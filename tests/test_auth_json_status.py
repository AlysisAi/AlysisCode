"""Machine-readable auth status for a GUI that spawns the CLI.

The contract under test: exactly one JSON object on stdout, exit code 0 whenever
the command ran, and enough keyring detail for a supervising app to explain why
a spawned process sees different auth state than the user's terminal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.agent_runtimes.base import RuntimeAccountStatus
from alysis_code.cli import app
from alysis_code.cli_impl.commands import auth as auth_mod
from alysis_code.config import AppConfig
from alysis_code.mcp import token_store as token_store_mod
from alysis_code.provider_auth import (
    ProviderAccountStatus,
    ProviderAuthError,
)

_STATUS_KEYS = frozenset(
    {
        "connection",
        "authenticated",
        "account_label",
        "credential_scope",
        "method",
        "detail",
        "transport",
        "error",
    }
)
_NULL_BACKEND = "keyring.backends.null.Keyring"


def _sole_json_object(output: str) -> dict[str, object]:
    """Parse stdout and prove nothing else was written to it."""

    stripped = output.strip()
    assert stripped, "expected a JSON object on stdout"
    payload = json.loads(stripped)
    assert isinstance(payload, dict)
    return payload


class _StubAdapter:
    display_name = "ChatGPT Codex subscription"
    protocol = "openai_responses"

    def __init__(
        self,
        status: ProviderAccountStatus,
        credential_scope: str = "fixture-route-scope",
    ) -> None:
        self._status = replace(status, credential_scope=credential_scope)

    def account_status(self) -> ProviderAccountStatus:
        return self._status

    def route_credential_scope(self) -> str:
        raise AssertionError("JSON status must not reload the credential scope")


@pytest.fixture(autouse=True)
def _isolate_provider_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let auth CLI tests inspect the developer's real credential vault."""

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "isolated-config"))
    monkeypatch.setenv(
        token_store_mod.OAUTH_KEY_SOURCE_ENV,
        token_store_mod.KEY_SOURCE_FILESYSTEM,
    )


@pytest.fixture
def null_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the placeholder backend the project's macOS CI selects."""

    import keyring
    import keyring.backends.null

    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", _NULL_BACKEND)
    monkeypatch.setattr(keyring, "get_keyring", keyring.backends.null.Keyring)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")


def test_auth_status_json_reports_unavailable_keyring_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, null_keyring: None
) -> None:
    monkeypatch.setattr(cli_mod, "load_config", AppConfig)
    monkeypatch.setattr(
        auth_mod,
        "create_provider_auth",
        lambda _id: _StubAdapter(ProviderAccountStatus(connected=False, detail="session expired")),
    )

    result = CliRunner().invoke(app, ["auth", "status", "openai-codex", "--json"])

    assert result.exit_code == 0, result.output
    payload = _sole_json_object(result.stdout)
    assert _STATUS_KEYS <= set(payload)
    assert payload["authenticated"] is False
    assert payload["detail"] == "session expired"
    assert payload["connection"] == "openai-codex"
    assert payload["error"] is None
    assert payload["keyring_available"] is False
    assert payload["keyring_backend"] == _NULL_BACKEND
    assert payload["credential_fallback"] == token_store_mod.KEY_SOURCE_FILESYSTEM


def test_auth_status_json_reports_available_keyring_without_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code import auth_diagnostics

    monkeypatch.setattr(cli_mod, "load_config", AppConfig)
    monkeypatch.setattr(auth_diagnostics, "last_keyring_outcome", lambda: None)
    monkeypatch.setattr(
        auth_diagnostics,
        "keyring_availability",
        lambda **_kwargs: token_store_mod.KeyringOutcome(available=True, backend="test.Backend"),
    )
    monkeypatch.setattr(
        auth_mod,
        "create_provider_auth",
        lambda _id: _StubAdapter(
            ProviderAccountStatus(
                connected=True,
                account_label="developer@example.test",
                detail="Connected with ChatGPT.",
            )
        ),
    )

    result = CliRunner().invoke(app, ["auth", "status", "openai-codex", "--json"])

    assert result.exit_code == 0, result.output
    payload = _sole_json_object(result.stdout)
    assert payload["authenticated"] is True
    assert payload["account_label"] == "developer@example.test"
    assert payload["credential_scope"] == "fixture-route-scope"
    assert payload["transport"] == "native Alysis Code client (openai_responses)"
    assert payload["keyring_available"] is True
    assert payload["credential_fallback"] is None


def test_auth_status_json_omits_route_scope_for_an_unusable_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_mod, "load_config", AppConfig)
    monkeypatch.setattr(
        auth_mod,
        "create_provider_auth",
        lambda _id: _StubAdapter(
            ProviderAccountStatus(connected=True, verified=False),
            credential_scope="must-not-bind",
        ),
    )

    result = CliRunner().invoke(app, ["auth", "status", "openai-codex", "--json"])

    assert result.exit_code == 0, result.output
    payload = _sole_json_object(result.stdout)
    assert payload["authenticated"] is True
    assert payload["verified"] is False
    assert payload["credential_scope"] is None


def test_auth_status_never_reads_the_keyring_just_to_report_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read probe can prompt to unlock a desktop keyring; a status probe must not."""

    reads: list[tuple[str, str]] = []

    def _record_read(service: str, account: str) -> str | None:
        reads.append((service, account))
        return None

    monkeypatch.setattr(cli_mod, "load_config", AppConfig)
    monkeypatch.setattr(token_store_mod, "_get_keyring_password", _record_read)
    monkeypatch.setattr(
        auth_mod,
        "create_provider_auth",
        lambda _id: _StubAdapter(ProviderAccountStatus(connected=True)),
    )

    result = CliRunner().invoke(app, ["auth", "status", "openai-codex", "--json"])

    assert result.exit_code == 0, result.output
    assert _sole_json_object(result.stdout)["keyring_available"] is not None
    assert reads == []


def test_auth_status_prefers_a_degradation_actually_hit_over_the_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolvable backend that failed in practice is reported as unavailable."""

    monkeypatch.setattr(cli_mod, "load_config", AppConfig)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Windows")
    monkeypatch.setattr(
        auth_mod,
        "create_provider_auth",
        lambda _id: _StubAdapter(ProviderAccountStatus(connected=False)),
    )
    token_store_mod._record_keyring_outcome(
        token_store_mod.KeyringOutcome(
            available=False,
            reason="read-failed",
            error_type="NoKeyringError",
            backend="keyring.backends.SecretService.Keyring",
        )
    )

    result = CliRunner().invoke(app, ["auth", "status", "openai-codex", "--json"])

    assert result.exit_code == 0, result.output
    payload = _sole_json_object(result.stdout)
    assert payload["keyring_available"] is False
    assert payload["keyring_backend"] == "keyring.backends.SecretService.Keyring"
    assert payload["credential_fallback"] == token_store_mod.KEY_SOURCE_DPAPI


def test_auth_status_json_exit_code_does_not_encode_authentication_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The JSON carries auth state; a nonzero exit means the command failed."""

    monkeypatch.setattr(cli_mod, "load_config", AppConfig)
    monkeypatch.setattr(
        auth_mod,
        "create_provider_auth",
        lambda _id: _StubAdapter(ProviderAccountStatus(connected=False, detail="not connected")),
    )
    runner = CliRunner()

    machine = runner.invoke(app, ["auth", "status", "openai-codex", "--json"])
    human = runner.invoke(app, ["auth", "status", "openai-codex"])

    assert machine.exit_code == 0, machine.output
    assert _sole_json_object(machine.stdout)["authenticated"] is False
    # The human surface keeps its historical shell-friendly exit code.
    assert human.exit_code == 1
    assert "Authenticated: no" in human.output


def test_auth_status_json_turns_a_provider_error_into_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_mod, "load_config", AppConfig)

    def _broken(_id: str) -> object:
        raise ProviderAuthError("Could not read the encrypted provider credential store.")

    monkeypatch.setattr(auth_mod, "create_provider_auth", _broken)
    runner = CliRunner()

    machine = runner.invoke(app, ["auth", "status", "openai-codex", "--json"])
    human = runner.invoke(app, ["auth", "status", "openai-codex"])

    assert machine.exit_code == 0, machine.output
    payload = _sole_json_object(machine.stdout)
    assert payload["authenticated"] is False
    assert payload["error"] == "Could not read the encrypted provider credential store."
    # Unchanged for humans: a provider error is still a runtime error exit.
    assert human.exit_code == 2


def test_auth_status_json_reports_a_delegated_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = AppConfig(
        execution={"backend": "delegated", "runtime": "openai-codex"},
        agent_runtimes={"openai-codex": {"adapter": "codex-cli", "executable": "codex"}},
    )
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)

    class _Snapshot:
        option = type(
            "_Option",
            (),
            {"id": "openai-codex", "label": "Codex CLI", "adapter": "codex-cli"},
        )()
        settings = type("_Settings", (), {"executable": "codex"})()
        probe = type(
            "_Probe",
            (),
            {"available": True, "executable": "/usr/bin/codex", "version": "1.2.3"},
        )()
        account = RuntimeAccountStatus(
            authenticated=True,
            auth_method_id="browser",
            account_label="dev@example.test",
            detail="Signed in.",
        )

    monkeypatch.setattr(auth_mod, "runtime_connection_snapshot", lambda *_a, **_k: _Snapshot())

    result = CliRunner().invoke(app, ["auth", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = _sole_json_object(result.stdout)
    assert payload["authenticated"] is True
    assert payload["method"] == "browser"
    assert payload["transport"] == "delegated runtime (codex-cli)"
    assert payload["installed"] is True
    assert payload["version"] == "1.2.3"


def test_auth_list_json_emits_one_object_with_every_connection(
    monkeypatch: pytest.MonkeyPatch, null_keyring: None
) -> None:
    monkeypatch.setattr(cli_mod, "load_config", AppConfig)
    monkeypatch.setattr(
        auth_mod,
        "create_provider_auth",
        lambda _id: _StubAdapter(
            ProviderAccountStatus(connected=True, account_label="dev@example.test")
        ),
    )

    result = CliRunner().invoke(app, ["auth", "list", "--json"])

    assert result.exit_code == 0, result.output
    payload = _sole_json_object(result.stdout)
    assert payload["keyring_available"] is False
    assert payload["error"] is None
    connections = payload["connections"]
    assert isinstance(connections, list) and connections
    for entry in connections:
        assert _STATUS_KEYS <= set(entry)
    assert {entry["connection"] for entry in connections} == {"openai-codex"}


def test_auth_list_json_reports_a_failing_connection_without_failing_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_mod, "load_config", AppConfig)

    def _broken(_id: str) -> object:
        raise ProviderAuthError("vault unreadable")

    monkeypatch.setattr(auth_mod, "create_provider_auth", _broken)

    result = CliRunner().invoke(app, ["auth", "list", "--json"])

    assert result.exit_code == 0, result.output
    entry = _sole_json_object(result.stdout)["connections"][0]
    assert entry["authenticated"] is False
    assert entry["error"] == "vault unreadable"


def test_auth_status_human_output_stays_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "load_config", AppConfig)
    monkeypatch.setattr(
        auth_mod,
        "create_provider_auth",
        lambda _id: _StubAdapter(
            ProviderAccountStatus(connected=True, account_label="dev@example.test")
        ),
    )

    result = CliRunner().invoke(app, ["auth", "status", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert "Authenticated: yes" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_auth_status_human_output_names_the_keyring_fallback(
    monkeypatch: pytest.MonkeyPatch, null_keyring: None
) -> None:
    """Degrade loudly: an interactive user is told too, not just a supervising app."""

    monkeypatch.setattr(cli_mod, "load_config", AppConfig)
    monkeypatch.setattr(
        auth_mod,
        "create_provider_auth",
        lambda _id: _StubAdapter(
            ProviderAccountStatus(connected=True, account_label="dev@example.test")
        ),
    )

    result = CliRunner().invoke(app, ["auth", "status", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert "OS keyring unavailable" in result.output
    assert token_store_mod.KEY_SOURCE_FILESYSTEM in result.output


def test_whoami_json_reports_the_alysis_account(monkeypatch: pytest.MonkeyPatch) -> None:
    from alysis_code import account_login

    cfg = AppConfig()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        account_login,
        "login_status",
        lambda _cfg: account_login.LoginStatus(
            logged_in=True,
            profile_name="alysis",
            base_url="https://proxy.example.test/v1",
            active=True,
            key_preview="abcd1234…",
        ),
    )
    monkeypatch.setattr(account_login, "fetch_trial_status", lambda _cfg: None)

    result = CliRunner().invoke(app, ["whoami", "--json"])

    assert result.exit_code == 0, result.output
    payload = _sole_json_object(result.stdout)
    assert _STATUS_KEYS <= set(payload)
    assert payload["connection"] == "alysis"
    assert payload["authenticated"] is True
    assert payload["method"] == "access-key"
    assert payload["transport"] == "https://proxy.example.test/v1"
    assert payload["profile_active"] is True
    # Local state is authoritative; plan/credits live on the account page.
    # Derived from alysis_cloud rather than hardcoded, so moving the product
    # site is one constant and does not require editing this assertion.
    from alysis_code.alysis_cloud import site_host

    assert f"{site_host()}/account" in str(payload["detail"])


def test_whoami_json_reports_a_disconnected_account_with_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code import account_login

    monkeypatch.setattr(cli_mod, "load_config", AppConfig)
    monkeypatch.setattr(
        account_login,
        "login_status",
        lambda _cfg: account_login.LoginStatus(
            logged_in=False,
            profile_name="alysis",
            base_url="https://proxy.example.test/v1",
            active=False,
            key_preview=None,
        ),
    )

    result = CliRunner().invoke(app, ["whoami", "--json"])

    assert result.exit_code == 0, result.output
    payload = _sole_json_object(result.stdout)
    assert payload["authenticated"] is False
    assert payload["account_label"] is None
    assert payload["method"] is None


def test_doctor_auth_json_reports_env_keyring_and_store_health(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, null_keyring: None
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))

    result = CliRunner().invoke(app, ["doctor", "auth", "--json"])

    assert result.exit_code == 0, result.output
    payload = _sole_json_object(result.stdout)
    assert set(payload) == {"context", "environment", "keyring", "credential_stores"}
    assert payload["context"]["kind"] in {"interactive", "spawned"}
    assert payload["keyring"]["available"] is False
    assert payload["keyring"]["backend"] == _NULL_BACKEND
    assert payload["keyring"]["env_override"] == _NULL_BACKEND
    assert payload["keyring"]["reason"] == "unusable-backend"
    environment = payload["environment"]
    assert environment["config_dir_override"] == os.fspath(tmp_path / "cfg")
    assert environment["home"]
    assert isinstance(environment["path"]["entry_count"], int)
    stores = {store["name"]: store for store in payload["credential_stores"]}
    assert set(stores) == {"provider_auth", "mcp_oauth"}
    for store in stores.values():
        assert store["exists"] is False
        assert store["readable"] is True
        assert store["planned_key_source"] == token_store_mod.KEY_SOURCE_FILESYSTEM


def test_doctor_auth_json_reports_an_unreadable_credential_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    (cfg_dir / "provider_auth_tokens.json").write_text("{not json", encoding="utf-8")

    result = CliRunner().invoke(app, ["doctor", "auth", "--json"])

    assert result.exit_code == 0, result.output
    stores = {
        store["name"]: store for store in _sole_json_object(result.stdout)["credential_stores"]
    }
    provider_store = stores["provider_auth"]
    assert provider_store["exists"] is True
    assert provider_store["readable"] is False
    assert "McpTokenStoreCorruptError" in str(provider_store["error"])


def test_doctor_auth_human_output_stays_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))

    result = CliRunner().invoke(app, ["doctor", "auth"])

    assert result.exit_code == 0, result.output
    assert "alysis doctor auth" in result.output
    assert "keyring_available" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_unknown_doctor_target_still_fails_with_usage_exit_code() -> None:
    result = CliRunner().invoke(app, ["doctor", "nonsense"])

    assert result.exit_code == 2
    assert "Unknown doctor target" in result.output


@pytest.mark.parametrize("args", [["doctor", "--json"], ["doctor", "providers", "--json"]])
def test_doctor_rejects_json_for_check_groups_that_have_no_json_form(
    args: list[str],
) -> None:
    """Handing a table to a caller that asked for JSON is worse than refusing."""

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 2
    assert "--json is only supported" in result.output


def test_auth_status_json_in_a_spawned_process_with_the_ci_keyring_backend(
    tmp_path: Path,
) -> None:
    """End to end in the configuration the project's macOS CI actually sets.

    In-process tests patch ``keyring.get_keyring``; only a real subprocess proves
    ``PYTHON_KEYRING_BACKEND`` reaches the probe, which is the whole point of the
    spawned-process contract.
    """

    env = dict(os.environ)
    env["PYTHON_KEYRING_BACKEND"] = _NULL_BACKEND
    env["ALYSIS_CONFIG_DIR"] = os.fspath(tmp_path / "cfg")
    env["PYTHONPATH"] = os.fspath(Path(__file__).resolve().parents[1] / "src")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alysis_code.cli",
            "auth",
            "status",
            "openai-codex",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    payload = _sole_json_object(completed.stdout)
    assert payload["keyring_available"] is False
    assert payload["keyring_backend"] == _NULL_BACKEND
    assert payload["authenticated"] is False
    assert payload["connection"] == "openai-codex"
