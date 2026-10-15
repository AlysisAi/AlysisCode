from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.agent_runtimes.base import RuntimeAccountStatus
from alysis_code.capabilities import build_capabilities_payload
from alysis_code.cli import app
from alysis_code.cli_impl.commands import auth as auth_mod
from alysis_code.config import AppConfig
from alysis_code.profiles import (
    ProfileSpec,
    add_profile,
    get_active_profile,
    set_active_profile,
)
from alysis_code.provider_auth import (
    ProviderAccountStatus,
    ProviderModel,
    ProviderReasoningEffort,
)


def test_unified_login_rows_offer_alysis_and_chatgpt_codex() -> None:
    rows = auth_mod.login_connection_rows()

    assert [row[0] for row in rows] == ["alysis", "openai-codex"]
    assert [row[1] for row in rows] == [
        "Alysis Code account",
        "ChatGPT subscription",
    ]


def test_unified_login_routes_subscription_choice_to_provider_flow(monkeypatch) -> None:
    connected: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(
        auth_mod,
        "login_provider_interactively",
        lambda *, runtime_id=None, device_code=False: connected.append((runtime_id, device_code)),
    )

    auth_mod.login_connection_interactively("openai-codex")

    assert connected == [("openai-codex", False)]


class _DirectAdapter:
    profile_name = "chatgpt-codex"
    protocol = "openai_responses"
    base_url = "https://chatgpt.com/backend-api/codex"
    display_name = "ChatGPT Codex subscription"
    access_token = "subscription-token-must-not-leak"

    def __init__(self) -> None:
        self.login_methods: list[str] = []

    def login(self, *, method: str, output_write):  # type: ignore[no-untyped-def]
        self.login_methods.append(method)
        return ProviderAccountStatus(
            connected=True,
            account_label="developer@example.test",
        )

    def logout(self) -> ProviderAccountStatus:
        return ProviderAccountStatus(connected=False)

    def account_status(self) -> ProviderAccountStatus:
        return ProviderAccountStatus(
            connected=True,
            account_label="developer@example.test",
            detail="Connected with ChatGPT.",
        )

    def list_models(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
        assert refresh is True
        return (
            ProviderModel(
                id="gpt-codex-test",
                label="GPT Codex Test",
                is_default=True,
                reasoning_efforts=(ProviderReasoningEffort("medium", "Medium"),),
                default_reasoning_effort="medium",
            ),
        )


class _DisconnectedAdapter(_DirectAdapter):
    def account_status(self) -> ProviderAccountStatus:
        return ProviderAccountStatus(
            connected=False,
            verified=False,
            account_label="developer@example.test",
            detail="Not connected.",
        )


def _api_active_with_subscription(adapter: _DirectAdapter) -> AppConfig:
    cfg = AppConfig()
    auth_mod._activate_direct_provider(cfg, "openai-codex", adapter)
    add_profile(
        cfg,
        ProfileSpec(
            name="openai-api",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            default_model="api-model",
        ),
    )
    set_active_profile(cfg, "openai-api")
    return cfg


def test_auth_login_activates_native_subscription_profile(monkeypatch) -> None:
    cfg = AppConfig()
    saved: list[AppConfig] = []
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        cli_mod, "save_config", lambda value: saved.append(value.model_copy(deep=True))
    )
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "login", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert "Connected" in result.output
    assert adapter.login_methods == ["browser"]
    assert cfg.execution.backend == "native"
    assert cfg.execution.runtime is None
    assert cfg.model == ""
    assert cfg.llm_reasoning_effort is None
    assert get_active_profile(cfg).auth_provider == "openai-codex"
    assert get_active_profile(cfg).reasoning_effort is None
    assert cfg.extra_fields["subscription_model_selection_required"] == "openai-codex"
    assert "Choose the subscription model and reasoning effort" in result.output
    assert saved and saved[0].model_dump() == cfg.model_dump()
    profile_payload = cfg.extra_fields["profiles"]["chatgpt-codex"]
    assert not any("token" in key.casefold() for key in profile_payload)


def test_auth_login_preserves_existing_config_selection(monkeypatch) -> None:
    cfg = AppConfig(model="gpt-codex-test", llm_reasoning_effort="medium")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="gpt-codex-test",
        reasoning_effort="medium",
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda _value: None)
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "login", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert get_active_profile(cfg).default_model == "gpt-codex-test"
    assert get_active_profile(cfg).reasoning_effort == "medium"
    assert "subscription_model_selection_required" not in cfg.extra_fields


def test_auth_login_does_not_confirm_a_migrated_selection(monkeypatch) -> None:
    cfg = AppConfig(model="gpt-codex-test", llm_reasoning_effort="medium")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="gpt-codex-test",
        reasoning_effort="medium",
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    cfg.extra_fields["subscription_model_selection_required"] = "openai-codex"
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda _value: None)
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "login", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert cfg.extra_fields["subscription_model_selection_required"] == "openai-codex"
    assert "Choose the subscription model and reasoning effort" in result.output


def test_auth_login_marks_unavailable_saved_selection_for_config_without_replacing_it(
    monkeypatch,
) -> None:
    cfg = AppConfig(model="old-account-model", llm_reasoning_effort="high")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="old-account-model",
        reasoning_effort="high",
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda _value: None)
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "login", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert get_active_profile(cfg).default_model == "old-account-model"
    assert get_active_profile(cfg).reasoning_effort == "high"
    assert cfg.extra_fields["subscription_model_selection_required"] == "openai-codex"


def test_provider_auth_login_supports_device_code(monkeypatch) -> None:
    cfg = AppConfig()
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda _value: None)
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(
        app,
        ["auth", "login", "openai-codex", "--device-code"],
    )

    assert result.exit_code == 0, result.output
    assert adapter.login_methods == ["device-code"]


def test_auth_models_json_matches_desktop_contract_without_secret_values(monkeypatch) -> None:
    adapter = _DirectAdapter()
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "models", "openai-codex", "--json"])

    assert result.exit_code == 0, result.output
    assert build_capabilities_payload()["features"]["auth.models-json"] is True
    assert json.loads(result.stdout) == [
        {
            "id": "gpt-codex-test",
            "label": "GPT Codex Test",
            "description": "",
            "is_default": True,
            "default_reasoning_effort": "medium",
            "reasoning_efforts": [{"id": "medium", "label": "Medium", "description": ""}],
            "input_modalities": ["text"],
            "context_window_tokens": None,
            "max_output_tokens": None,
        }
    ]
    assert adapter.access_token not in result.stdout
    assert "developer@example.test" not in result.stdout


def test_auth_select_model_atomically_switches_provider_and_model(monkeypatch) -> None:
    adapter = _DirectAdapter()
    cfg = _api_active_with_subscription(adapter)
    saved: list[AppConfig] = []
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        cli_mod,
        "save_config",
        lambda value: saved.append(value.model_copy(deep=True)),
    )
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(
        app,
        [
            "auth",
            "select-model",
            "openai-codex",
            "gpt-codex-test",
            "--reasoning-effort",
            "medium",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert build_capabilities_payload()["features"]["auth.select-model"] is True
    assert json.loads(result.stdout) == {
        "id": "openai-codex",
        "profile_name": "chatgpt-codex",
        "model": "gpt-codex-test",
        "reasoning_effort": "medium",
    }
    assert len(saved) == 1
    persisted = saved[0]
    assert persisted.extra_fields["active_profile"] == "chatgpt-codex"
    assert persisted.model == "gpt-codex-test"
    assert persisted.llm_reasoning_effort == "medium"
    assert get_active_profile(persisted).default_model == "gpt-codex-test"
    assert get_active_profile(persisted).reasoning_effort == "medium"
    assert persisted.extra_fields["onboarded"] is True
    assert "subscription_model_selection_required" not in persisted.extra_fields
    assert adapter.access_token not in result.stdout
    assert "developer@example.test" not in result.stdout


@pytest.mark.parametrize(
    ("adapter", "model", "effort", "expected_error"),
    [
        (
            _DirectAdapter(),
            "missing-model",
            "auto",
            "selected model is not available",
        ),
        (
            _DirectAdapter(),
            "gpt-codex-test",
            "unsupported",
            "reasoning effort is not supported",
        ),
        (
            _DisconnectedAdapter(),
            "gpt-codex-test",
            "medium",
            "Not connected",
        ),
    ],
)
def test_auth_select_model_rejects_invalid_inputs_without_mutation(
    monkeypatch,
    adapter: _DirectAdapter,
    model: str,
    effort: str,
    expected_error: str,
) -> None:
    cfg = _api_active_with_subscription(_DirectAdapter())
    original = cfg.model_copy(deep=True)
    saved: list[AppConfig] = []
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda value: saved.append(value))
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(
        app,
        [
            "auth",
            "select-model",
            "openai-codex",
            model,
            "--reasoning-effort",
            effort,
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert expected_error in json.loads(result.stdout)["error"]
    assert saved == []
    assert cfg.model_dump() == original.model_dump()
    assert adapter.access_token not in result.stdout


def test_auth_provider_json_commands_reject_unknown_connection() -> None:
    runner = CliRunner()

    models = runner.invoke(app, ["auth", "models", "unknown-provider", "--json"])
    selection = runner.invoke(
        app,
        ["auth", "select-model", "unknown-provider", "model", "--json"],
    )

    assert models.exit_code == 2
    assert selection.exit_code == 2
    assert json.loads(models.stdout)["ok"] is False
    assert json.loads(selection.stdout)["ok"] is False


def test_top_level_connect_command_is_removed() -> None:
    result = CliRunner().invoke(app, ["connect", "openai-codex"])

    assert result.exit_code == 2
    assert "No such command 'connect'" in result.output


def test_auth_status_reports_native_subscription_account(monkeypatch) -> None:
    cfg = AppConfig()
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "status", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert "ChatGPT Codex subscription" in result.output
    assert "native Alysis Code client (openai_responses)" in result.output
    assert "Authenticated: yes" in result.output
    assert "developer@example.test" in result.output


def test_auth_logout_leaves_selection_but_removes_provider_credentials(monkeypatch) -> None:
    cfg = AppConfig(
        execution={"backend": "delegated", "runtime": "openai-codex"},
        agent_runtimes={
            "openai-codex": {"adapter": "codex-cli", "executable": "codex"},
        },
    )
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        auth_mod,
        "logout_runtime",
        lambda *_args, **_kwargs: RuntimeAccountStatus(authenticated=False),
    )

    result = CliRunner().invoke(app, ["auth", "logout"])

    assert result.exit_code == 0, result.output
    assert "Disconnected" in result.output
    assert cfg.execution.backend == "delegated"
    assert cfg.execution.runtime == "openai-codex"


def test_auth_logout_does_not_claim_success_when_provider_could_not_run(monkeypatch) -> None:
    cfg = AppConfig(
        execution={"backend": "delegated", "runtime": "openai-codex"},
        agent_runtimes={
            "openai-codex": {"adapter": "codex-cli", "executable": "missing-codex"},
        },
    )
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        auth_mod,
        "logout_runtime",
        lambda *_args, **_kwargs: RuntimeAccountStatus(
            authenticated=False,
            verified=False,
            detail="Codex CLI is missing.",
        ),
    )

    result = CliRunner().invoke(app, ["auth", "logout"])

    assert result.exit_code == 1
    assert "Logout failed" in result.output
    assert "Disconnected" not in result.output
