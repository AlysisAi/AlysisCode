from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code.llm.metadata import build_provider_route_identity
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.protocols import (
    OPENAI_RESPONSES_PROTOCOL,
    resolve_reasoning_trace_capability,
)
from alysis_code.llm.types import LLMError
from alysis_code.mcp import token_store as token_store_mod
from alysis_code.mcp.errors import McpTokenStoreUnavailableError
from alysis_code.provider_auth import store as provider_store_mod
from alysis_code.provider_auth.base import (
    ProviderAuthError,
    ProviderLoginRequiredError,
    ProviderRequestAuthorization,
)
from alysis_code.provider_auth.openai_codex import (
    SESSION_EXPIRED_DETAIL,
    OpenAICodexSubscriptionAuth,
)
from alysis_code.provider_auth.store import (
    ProviderTokenRecord as StoredProviderTokenRecord,
)
from alysis_code.provider_auth.store import (
    load_provider_token,
    provider_token_store_path,
    save_provider_token,
    save_refreshed_provider_token,
)


def ProviderTokenRecord(**kwargs):
    # Fixtures model a validated SIWC registration. Signed-token validation is
    # separately exercised in test_subscription_plan_auth.py, not mocked there.
    return StoredProviderTokenRecord(
        client_id="oaiapp_fixture",
        subject=kwargs.get("account_id") or "fixture-subject",
        scopes=("chatgpt.tokens.use.direct", "resource.invoke"),
        **kwargs,
    )


@pytest.mark.parametrize(
    "live_efforts", [None, [{"effort": "low"}, {"effort": "max"}, {"effort": "ultra"}]]
)
def test_codex_filters_app_only_ultra_from_live_and_fallback_efforts(monkeypatch, live_efforts):
    monkeypatch.setattr(
        OpenAICodexSubscriptionAuth,
        "authorize_request",
        lambda *a, **k: ProviderRequestAuthorization(credential_scope="test-scope", headers={}),
    )
    model = {"slug": "gpt-6-sol"}
    if live_efforts is not None:
        model["supported_reasoning_levels"] = live_efforts
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [model]}))
    )
    efforts = [item.id for item in adapter.list_models()[0].reasoning_efforts]
    assert "ultra" not in efforts
    assert "max" in efforts and "low" in efforts


def test_codex_saved_ultra_setting_reports_actionable_error_before_inference():
    adapter = OpenAICodexSubscriptionAuth()
    with pytest.raises(ProviderAuthError, match="Choose Max"):
        adapter.adapt_responses_payload({"model": "gpt-6-sol", "reasoning": {"effort": "ultra"}})


@pytest.fixture(autouse=True)
def _isolate_provider_vault(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No focused provider-auth test may inspect the developer's real vault."""

    from alysis_code.provider_auth.openai_codex import _jwt_claims

    monkeypatch.setattr(
        OpenAICodexSubscriptionAuth,
        "_validate_id_token",
        lambda self, token, *_: _jwt_claims(token),
    )
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "isolated-config"))
    monkeypatch.setenv(
        token_store_mod.OAUTH_KEY_SOURCE_ENV,
        token_store_mod.KEY_SOURCE_FILESYSTEM,
    )


def _test_jwt(claims: dict[str, Any]) -> str:
    claims = {
        **claims,
        "sub": claims.get("chatgpt_account_id")
        or claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        or "fixture-subject",
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"test.{payload}.signature"


def test_subscription_store_uses_random_filesystem_key_without_tui_stderr_noise(
    tmp_path, monkeypatch, capsys, caplog
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))

    def _keyring_unavailable(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(token_store_mod, "_get_keyring_password", _keyring_unavailable)
    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _keyring_unavailable)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")
    record = ProviderTokenRecord(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=time.time() + 3600,
        account_id="account-123",
    )

    with caplog.at_level("WARNING", logger=token_store_mod.__name__):
        save_provider_token("openai-codex", record)
        loaded = [load_provider_token("openai-codex") for _ in range(3)]

    path = provider_token_store_path()
    envelope = json.loads(path.read_text(encoding="utf-8"))
    key_path = token_store_mod.filesystem_master_key_path(path)
    captured = capsys.readouterr()
    assert loaded == [record, record, record]
    assert envelope["version"] == token_store_mod.CURRENT_ENVELOPE_VERSION
    assert envelope["key_source"] == token_store_mod.KEY_SOURCE_FILESYSTEM
    assert len(key_path.read_bytes()) == 32
    assert "access-secret" not in path.read_text(encoding="utf-8")
    assert "refresh-secret" not in path.read_text(encoding="utf-8")
    assert "weak-derived-fallback" not in caplog.text
    assert captured.err == ""


def test_subscription_store_filesystem_policy_never_probes_or_writes_keyring(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv(
        token_store_mod.OAUTH_KEY_SOURCE_ENV,
        token_store_mod.KEY_SOURCE_FILESYSTEM,
    )
    keyring_calls: list[str] = []

    def _unexpected_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        keyring_calls.append("get")
        raise AssertionError("filesystem policy must not read keyring for a new store")

    def _unexpected_set(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        keyring_calls.append("set")
        raise AssertionError("filesystem policy must not write keyring for a new store")

    monkeypatch.setattr(token_store_mod, "_get_keyring_password", _unexpected_get)
    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _unexpected_set)
    record = ProviderTokenRecord(
        access_token="policy-access-secret",
        refresh_token="policy-refresh-secret",
        expires_at=time.time() + 3600,
    )

    save_provider_token("openai-codex", record)

    path = provider_token_store_path()
    raw = path.read_text(encoding="utf-8")
    envelope = json.loads(raw)
    assert keyring_calls == []
    assert load_provider_token("openai-codex") == record
    assert keyring_calls == []
    assert envelope["key_source"] == token_store_mod.KEY_SOURCE_FILESYSTEM
    assert len(token_store_mod.filesystem_master_key_path(path).read_bytes()) == 32
    assert "policy-access-secret" not in raw
    assert "policy-refresh-secret" not in raw


@pytest.mark.parametrize("policy_mode", ["explicit"])
def test_subscription_store_filesystem_policy_migrates_keyring_envelope_once(
    tmp_path, monkeypatch, policy_mode
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.delenv(token_store_mod.OAUTH_KEY_SOURCE_ENV, raising=False)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")
    keyring_values: dict[tuple[str, str], str] = {}
    keyring_calls: list[str] = []

    def _get(service: str, account: str) -> str | None:
        keyring_calls.append("get")
        return keyring_values.get((service, account))

    def _set(service: str, account: str, password: str) -> None:
        keyring_calls.append("set")
        keyring_values[(service, account)] = password

    monkeypatch.setattr(token_store_mod, "_get_keyring_password", _get)
    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _set)
    record = ProviderTokenRecord(
        access_token="migration-access-secret",
        refresh_token="migration-refresh-secret",
        expires_at=time.time() + 3600,
    )
    save_provider_token("openai-codex", record)
    path = provider_token_store_path()
    assert json.loads(path.read_text(encoding="utf-8"))["key_source"] == (
        token_store_mod.KEY_SOURCE_KEYRING
    )

    keyring_calls.clear()
    if policy_mode == "explicit":
        monkeypatch.setenv(
            token_store_mod.OAUTH_KEY_SOURCE_ENV,
            token_store_mod.KEY_SOURCE_FILESYSTEM,
        )
    else:
        monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Darwin")
    assert load_provider_token("openai-codex") == record
    assert keyring_calls == ["get"]
    migrated_raw = path.read_text(encoding="utf-8")
    assert json.loads(migrated_raw)["key_source"] == token_store_mod.KEY_SOURCE_FILESYSTEM
    assert "migration-access-secret" not in migrated_raw
    assert "migration-refresh-secret" not in migrated_raw

    keyring_calls.clear()
    assert load_provider_token("openai-codex") == record
    assert keyring_calls == []


@pytest.mark.parametrize(
    "invalid_policy",
    ["", "filesystem_random", "Filesystem-random", " filesystem-random"],
)
def test_subscription_store_rejects_non_exact_key_source_policy_without_keyring_access(
    tmp_path, monkeypatch, invalid_policy
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv(token_store_mod.OAUTH_KEY_SOURCE_ENV, invalid_policy)
    keyring_calls: list[str] = []

    def _unexpected_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        keyring_calls.append("get")
        return None

    def _unexpected_set(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        keyring_calls.append("set")

    monkeypatch.setattr(token_store_mod, "_get_keyring_password", _unexpected_get)
    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _unexpected_set)
    record = ProviderTokenRecord(
        access_token="invalid-policy-access-secret",
        refresh_token="invalid-policy-refresh-secret",
        expires_at=time.time() + 3600,
    )

    with pytest.raises(ProviderAuthError) as excinfo:
        save_provider_token("openai-codex", record)

    cause = excinfo.value.__cause__
    assert isinstance(cause, McpTokenStoreUnavailableError)
    assert token_store_mod.OAUTH_KEY_SOURCE_ENV in str(cause)
    assert token_store_mod.KEY_SOURCE_FILESYSTEM in str(cause)
    assert keyring_calls == []
    assert not provider_token_store_path().exists()


def test_subscription_store_unset_policy_preserves_default_keyring_semantics(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.delenv(token_store_mod.OAUTH_KEY_SOURCE_ENV, raising=False)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")
    keyring_values: dict[tuple[str, str], str] = {}
    keyring_calls: list[str] = []

    def _get(service: str, account: str) -> str | None:
        keyring_calls.append("get")
        return keyring_values.get((service, account))

    def _set(service: str, account: str, password: str) -> None:
        keyring_calls.append("set")
        keyring_values[(service, account)] = password

    monkeypatch.setattr(token_store_mod, "_get_keyring_password", _get)
    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _set)
    record = ProviderTokenRecord(
        access_token="default-access-secret",
        refresh_token="default-refresh-secret",
        expires_at=time.time() + 3600,
    )

    save_provider_token("openai-codex", record)

    path = provider_token_store_path()
    raw = path.read_text(encoding="utf-8")
    assert keyring_calls.count("set") == 1
    assert json.loads(raw)["key_source"] == token_store_mod.KEY_SOURCE_KEYRING
    assert load_provider_token("openai-codex") == record
    assert "default-access-secret" not in raw
    assert "default-refresh-secret" not in raw


def test_desktop_filesystem_policy_never_rotates_its_vault_to_keyring(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Darwin")
    monkeypatch.setenv(
        token_store_mod.OAUTH_KEY_SOURCE_ENV,
        token_store_mod.KEY_SOURCE_FILESYSTEM,
    )
    record = ProviderTokenRecord(
        access_token="shared-access-secret",
        refresh_token="shared-refresh-secret",
        expires_at=time.time() + 3600,
    )
    save_provider_token("openai-codex", record)
    path = provider_token_store_path()
    desktop_envelope = path.read_text(encoding="utf-8")
    keyring_calls: list[str] = []

    def _unexpected_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        keyring_calls.append("get")
        raise AssertionError("the desktop filesystem policy must not read the keyring")

    def _unexpected_set(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        keyring_calls.append("set")
        raise AssertionError("the desktop filesystem policy must not write the keyring")

    monkeypatch.setattr(token_store_mod, "_get_keyring_password", _unexpected_get)
    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _unexpected_set)
    # A later desktop-launched runtime process carries the same explicit policy.
    assert load_provider_token("openai-codex") == record
    assert path.read_text(encoding="utf-8") == desktop_envelope
    assert keyring_calls == []
    assert json.loads(desktop_envelope)["key_source"] == (token_store_mod.KEY_SOURCE_FILESYSTEM)

    cli_record = replace(record, expires_at=record.expires_at + 60)
    save_provider_token("openai-codex", cli_record)
    cli_envelope = path.read_text(encoding="utf-8")
    assert load_provider_token("openai-codex") == cli_record
    assert json.loads(cli_envelope)["key_source"] == token_store_mod.KEY_SOURCE_FILESYSTEM
    assert keyring_calls == []
    assert "shared-access-secret" not in cli_envelope
    assert "shared-refresh-secret" not in cli_envelope


def test_credential_scope_survives_refresh_metadata_changes_and_rotates_on_login(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv(
        token_store_mod.OAUTH_KEY_SOURCE_ENV,
        token_store_mod.KEY_SOURCE_FILESYSTEM,
    )
    original = ProviderTokenRecord(
        access_token="account-a-access-1",
        refresh_token="account-a-refresh-1",
        expires_at=time.time() + 3600,
        account_id="account-a",
        account_label="developer@example.test (free)",
    )
    save_provider_token("openai-codex", original)
    adapter = OpenAICodexSubscriptionAuth()
    original_scope = adapter.route_credential_scope()

    refreshed = replace(
        original,
        access_token="account-a-access-2",
        refresh_token="account-a-refresh-2",
        expires_at=time.time() + 7200,
        account_label="developer@example.test (pro)",
    )
    assert (
        save_refreshed_provider_token("openai-codex", expected=original, refreshed=refreshed)
        == refreshed
    )
    assert adapter.route_credential_scope() == original_scope

    replacement = ProviderTokenRecord(
        access_token="account-b-access",
        refresh_token="account-b-refresh",
        expires_at=time.time() + 3600,
        account_id="account-b",
        account_label="other@example.test",
    )
    save_provider_token("openai-codex", replacement)
    replacement_scope = adapter.route_credential_scope()

    assert replacement_scope != original_scope
    assert len(original_scope) == len(replacement_scope) == 64
    assert "access" not in original_scope
    assert "developer" not in original_scope


def test_legacy_credential_migration_persists_one_nonsecret_generation(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv(
        token_store_mod.OAUTH_KEY_SOURCE_ENV,
        token_store_mod.KEY_SOURCE_FILESYSTEM,
    )
    path = provider_token_store_path()
    token_store_mod.save_token_payload(
        path,
        {
            "openai-codex": {
                "access_token": "legacy-access-secret",
                "refresh_token": "legacy-refresh-secret",
                "expires_at": time.time() + 3600,
                "account_id": "legacy-account",
                "account_label": "legacy@example.test",
            }
        },
    )

    first = load_provider_token("openai-codex")
    second = load_provider_token("openai-codex")
    decrypted = token_store_mod.load_token_payload_result(path).payload["openai-codex"]
    scope = OpenAICodexSubscriptionAuth().route_credential_scope()

    assert first is not None and second is not None
    assert first.credential_generation == second.credential_generation
    assert decrypted["credential_generation"] == first.credential_generation
    assert scope
    assert "legacy-access-secret" not in scope
    assert "legacy@example.test" not in scope


def test_refresh_compare_and_swap_cannot_overwrite_cross_process_login(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    config_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(config_dir))
    monkeypatch.setenv(
        token_store_mod.OAUTH_KEY_SOURCE_ENV,
        token_store_mod.KEY_SOURCE_FILESYSTEM,
    )
    account_a = ProviderTokenRecord(
        access_token="account-a-access",
        refresh_token="account-a-refresh",
        expires_at=time.time() + 3600,
        account_id="account-a",
    )
    save_provider_token("openai-codex", account_a)
    stale_refresh_snapshot = load_provider_token("openai-codex")
    assert stale_refresh_snapshot == account_a

    env = os.environ.copy()
    src_dir = os.fspath(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (src_dir, env.get("PYTHONPATH", "")) if part
    )
    replacement_script = """
import time
from alysis_code.provider_auth.store import ProviderTokenRecord, save_provider_token
save_provider_token(
    "openai-codex",
    ProviderTokenRecord(
        access_token="account-b-access",
        refresh_token="account-b-refresh",
        expires_at=time.time() + 3600,
        account_id="account-b",
    ),
)
"""
    subprocess.run(
        [sys.executable, "-c", replacement_script],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    account_b = load_provider_token("openai-codex")
    assert account_b is not None
    assert account_b.account_id == "account-b"
    stale_refreshed_a = replace(
        stale_refresh_snapshot,
        access_token="account-a-refreshed-access",
        refresh_token="account-a-refreshed-refresh",
    )

    authoritative = save_refreshed_provider_token(
        "openai-codex",
        expected=stale_refresh_snapshot,
        refreshed=stale_refreshed_a,
    )

    assert authoritative == account_b
    assert authoritative.credential_generation != stale_refresh_snapshot.credential_generation
    assert load_provider_token("openai-codex") == account_b


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock-specific contention simulation")
def test_provider_store_lock_contention_fails_with_a_bounded_generic_error(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(provider_store_mod, "_STORE_LOCK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(provider_store_mod, "_STORE_LOCK_POLL_SECONDS", 0.001)

    def always_busy(_fd: int, _operation: int) -> None:
        raise BlockingIOError(provider_store_mod.errno.EAGAIN, "simulated lock contention")

    monkeypatch.setattr(provider_store_mod.fcntl, "flock", always_busy)

    started = time.monotonic()
    with pytest.raises(ProviderAuthError, match="credential store is busy"):
        load_provider_token("openai-codex")

    assert time.monotonic() - started < 0.5


def test_codex_subscription_payload_is_stateless_and_preserves_encrypted_reasoning() -> None:
    adapter = OpenAICodexSubscriptionAuth()

    payload = adapter.adapt_responses_payload(
        {
            "model": "gpt-test",
            "input": [
                {"role": "system", "content": "system prompt"},
                {"role": "developer", "content": [{"type": "input_text", "text": "dev"}]},
                {"role": "user", "content": "hello"},
                {
                    "id": "reasoning-server-id",
                    "status": "completed",
                    "type": "reasoning",
                    "encrypted_content": "opaque-state",
                    "summary": [],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "count": {"type": "integer", "minimum": 1, "default": 2},
                            "opaque": False,
                            "values": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                        },
                        "required": ["count"],
                    },
                    "strict": True,
                }
            ],
            "reasoning": {"effort": "high"},
            "include": ["web_search_call.action.sources"],
            "temperature": 0.2,
            "max_output_tokens": 123,
            "previous_response_id": "resp_old",
            "parallel_tool_calls": False,
            "stream": True,
        }
    )

    assert payload["instructions"] == "system prompt\n\ndev"
    assert payload["input"][0] == {"role": "user", "content": "hello"}
    assert payload["input"][1] == {
        "type": "reasoning",
        "encrypted_content": "opaque-state",
        "summary": [],
    }
    assert payload["store"] is False
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["tools"][0]["tools"][0]["strict"] is False
    assert payload["tools"][0]["tools"][0]["parameters"] == {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "opaque": {"type": "string"},
            "values": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["count"],
    }
    assert payload["include"] == [
        "web_search_call.action.sources",
        "reasoning.encrypted_content",
    ]
    assert payload["text"] == {"verbosity": "low"}
    assert "temperature" not in payload
    assert "max_output_tokens" not in payload
    assert "previous_response_id" not in payload
    assert payload["parallel_tool_calls"] is False


def test_codex_subscription_does_not_request_summary_without_trace() -> None:
    adapter = OpenAICodexSubscriptionAuth()

    payload = adapter.adapt_responses_payload(
        {
            "model": "gpt-test",
            "input": [{"role": "user", "content": "hello"}],
            "stream": True,
        }
    )

    assert "reasoning" not in payload


def test_codex_subscription_preserves_explicit_summary_request_and_effort() -> None:
    adapter = OpenAICodexSubscriptionAuth()

    payload = adapter.adapt_responses_payload(
        {
            "model": "gpt-test",
            "input": [{"role": "user", "content": "hello"}],
            "reasoning": {"effort": "high", "summary": "auto"},
            "stream": True,
        }
    )

    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}


def test_codex_subscription_headers_are_destination_allowlisted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ALYSIS_OPENAI_CODEX_COMPAT_VERSION", raising=False)
    record = ProviderTokenRecord(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=time.time() + 3600,
        account_id="account-123",
        account_label="developer@example.test",
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: record,
    )
    adapter = OpenAICodexSubscriptionAuth()

    headers = adapter.authorization_headers(
        "https://api.openai.com/v1/responses",
        session_id="session-1",
    )

    assert headers["Authorization"] == "Bearer access-secret"
    assert "ChatGPT-Account-Id" not in headers
    assert "originator" not in headers
    assert headers["User-Agent"] == "Alysis-Code"
    assert "session-id" not in headers
    with pytest.raises(ProviderAuthError, match="non-OpenAI subscription destination"):
        adapter.authorization_headers("https://example.com/responses")
    with pytest.raises(ProviderAuthError, match="non-OpenAI subscription destination"):
        adapter.authorization_headers(
            "https://api.openai.com/v1/responses?redirect=https://example.com"
        )


def test_account_status_returns_scope_from_its_single_validated_snapshot(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    record = ProviderTokenRecord(
        access_token="status-access-secret",
        refresh_token="status-refresh-secret",
        expires_at=time.time() + 3600,
        account_id="account-1",
    )
    loads: list[str] = []

    def load_once(provider_id: str) -> ProviderTokenRecord:
        loads.append(provider_id)
        return record

    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        load_once,
    )

    status = OpenAICodexSubscriptionAuth().account_status()

    assert loads == ["openai-codex"]
    assert status.connected is True
    assert status.verified is True
    assert status.credential_scope
    assert "status-access-secret" not in status.credential_scope


def test_request_authorization_is_immutable_and_bound_to_the_expected_scope(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    record = ProviderTokenRecord(
        access_token="bound-access-secret",
        refresh_token="bound-refresh-secret",
        expires_at=time.time() + 3600,
        account_id="account-1",
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: record,
    )
    adapter = OpenAICodexSubscriptionAuth()
    expected_scope = adapter.route_credential_scope()

    authorization = adapter.authorize_request(
        "https://api.openai.com/v1/responses",
        expected_credential_scope=expected_scope,
    )

    assert authorization.credential_scope == expected_scope
    assert authorization.headers["Authorization"] == "Bearer bound-access-secret"
    assert "bound-access-secret" not in repr(authorization)
    with pytest.raises(TypeError):
        authorization.headers["Authorization"] = "Bearer replacement"  # type: ignore[index]


def test_codex_subscription_refresh_rotates_tokens(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    stored = ProviderTokenRecord(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=time.time() - 1,
        account_id="account-1",
    )
    saved: list[ProviderTokenRecord] = []
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: saved[-1] if saved else stored,
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.save_refreshed_provider_token",
        lambda _provider_id, *, expected, refreshed: saved.append(refreshed) or refreshed,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://auth.openai.com/api/accounts/oauth/token"
        assert b"refresh_token=old-refresh" in request.content
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )

    adapter = OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler))
    headers = adapter.authorization_headers("https://api.openai.com/v1/responses")

    assert headers["Authorization"] == "Bearer new-access"
    assert saved[-1].refresh_token == "new-refresh"
    assert saved[-1].account_id == "account-1"
    assert saved[-1].credential_generation == stored.credential_generation


@pytest.mark.parametrize("status_code", [429, 500])
def test_codex_refresh_transient_failure_uses_still_valid_token(
    monkeypatch,
    status_code: int,
) -> None:  # type: ignore[no-untyped-def]
    stored = ProviderTokenRecord(
        access_token="still-valid",
        refresh_token="refresh",
        expires_at=time.time() + 120,
        account_id="account-1",
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: stored,
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status_code, json={"error": "temporarily_unavailable"})
        )
    )

    headers = adapter.authorization_headers("https://api.openai.com/v1/responses")

    assert headers["Authorization"] == "Bearer still-valid"


def test_codex_refresh_transient_failure_does_not_reuse_expired_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    stored = ProviderTokenRecord(
        access_token="expired",
        refresh_token="refresh",
        expires_at=time.time() - 1,
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: stored,
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500, json={}))
    )

    with pytest.raises(ProviderAuthError, match="temporarily unavailable"):
        adapter.authorization_headers("https://api.openai.com/v1/responses")


def test_codex_refresh_invalid_grant_requires_login(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    stored = ProviderTokenRecord(
        access_token="expired",
        refresh_token="refresh",
        expires_at=time.time() - 1,
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: stored,
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )

    with pytest.raises(ProviderLoginRequiredError, match="session expired"):
        adapter.authorization_headers("https://api.openai.com/v1/responses")


def test_codex_refresh_invalid_grant_uses_concurrent_same_generation_refresh(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    stored = ProviderTokenRecord(
        access_token="expired",
        refresh_token="rotated-refresh",
        expires_at=time.time() - 1,
        account_id="account-a",
    )
    concurrent = replace(
        stored,
        access_token="concurrent-access",
        refresh_token="concurrent-refresh",
        expires_at=time.time() + 3600,
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: stored,
    )
    expected_scope = adapter.route_credential_scope()
    snapshots = iter((stored, stored, concurrent))
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: next(snapshots),
    )

    headers = adapter.authorization_headers(
        "https://api.openai.com/v1/responses",
        expected_credential_scope=expected_scope,
    )

    assert headers["Authorization"] == "Bearer concurrent-access"


def test_codex_refresh_rejects_a_different_account_without_preserving_scope(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    stored = ProviderTokenRecord(
        access_token="expired-a",
        refresh_token="refresh-a",
        expires_at=time.time() - 1,
        account_id="account-a",
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: stored,
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.save_refreshed_provider_token",
        lambda *_args, **_kwargs: pytest.fail("cross-account refresh must not be saved"),
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "id_token": _test_jwt({"chatgpt_account_id": "account-b"}),
                    "refresh_token": "refresh-b",
                    "expires_in": 3600,
                },
            )
        )
    )

    with pytest.raises(ProviderLoginRequiredError, match="different account"):
        adapter.authorization_headers("https://api.openai.com/v1/responses")


def test_codex_account_status_reports_an_expired_session_as_data(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A status probe answers; only the model transport raises."""

    stored = ProviderTokenRecord(
        access_token="expired",
        refresh_token="refresh",
        expires_at=time.time() - 1,
        account_label="dev@example.test",
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: stored,
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )

    status = adapter.account_status()

    assert status.connected is False
    assert status.verified is True
    assert status.detail == SESSION_EXPIRED_DETAIL
    assert status.account_label == "dev@example.test"


def test_codex_account_status_survives_an_unreadable_credential_store(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A spawned process that cannot reach the keyring must still get an answer."""

    def _unreadable(_provider_id: str) -> ProviderTokenRecord:
        raise ProviderAuthError("Could not read the encrypted provider credential store.")

    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        _unreadable,
    )

    status = OpenAICodexSubscriptionAuth().account_status()

    assert status.connected is False
    assert status.verified is False
    assert status.detail == "Could not read the encrypted provider credential store."


def test_codex_account_status_survives_an_unexpected_store_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _explode(_provider_id: str) -> ProviderTokenRecord:
        raise MemoryError("simulated")

    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        _explode,
    )

    status = OpenAICodexSubscriptionAuth().account_status()

    assert status.connected is False
    assert status.verified is False
    assert "MemoryError" in str(status.detail)


def test_subscription_model_catalog_uses_public_route_and_live_metadata(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ALYSIS_OPENAI_CODEX_COMPAT_VERSION", raising=False)
    record = ProviderTokenRecord(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=time.time() + 3600,
        account_id="account-123",
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: record,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert not request.url.query
        assert request.headers["User-Agent"] == "Alysis-Code"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "slug": "gpt-codex-live",
                        "display_name": "GPT Codex Live",
                        "description": "Account model",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 1,
                        "context_window": 272000,
                        "input_modalities": ["text", "image"],
                        "default_reasoning_level": "high",
                        "supported_reasoning_levels": [
                            {"effort": "low", "description": "Fast"},
                            {"effort": "high", "description": "Deep"},
                        ],
                    },
                    {
                        "slug": "gpt-chatgpt-only",
                        "display_name": "GPT ChatGPT Only",
                        "visibility": "list",
                        "supported_in_api": False,
                        "priority": 2,
                        "supported_reasoning_levels": [
                            {"effort": "medium", "description": "Balanced"},
                        ],
                    },
                ]
            },
        )

    models = OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler)).list_models(
        refresh=True
    )

    assert len(models) == 2
    assert models[0].id == "gpt-codex-live"
    assert models[0].is_default is True
    assert models[0].context_window_tokens == 272_000
    assert models[0].input_modalities == ("text", "image")
    assert [effort.id for effort in models[0].reasoning_efforts] == ["low", "high"]
    assert models[1].id == "gpt-chatgpt-only"
    assert [effort.id for effort in models[1].reasoning_efforts] == ["medium"]


def test_codex_model_catalog_cache_is_scoped_to_the_live_credential_generation(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    account_a = ProviderTokenRecord(
        access_token="account-a-access",
        refresh_token="account-a-refresh",
        expires_at=time.time() + 3600,
    )
    account_b = ProviderTokenRecord(
        access_token="account-b-access",
        refresh_token="account-b-refresh",
        expires_at=time.time() + 3600,
    )
    current = account_a
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: current,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        account = "a" if request.headers["Authorization"] == "Bearer account-a-access" else "b"
        return httpx.Response(
            200,
            json={"models": [{"slug": f"model-{account}", "visibility": "list"}]},
        )

    adapter = OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler))
    assert adapter.list_models()[0].id == "model-a"
    current = account_b
    assert adapter.list_models()[0].id == "model-b"

    assert len(requests) == 2


def test_responses_request_detects_cross_instance_account_replacement_before_http(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv(
        token_store_mod.OAUTH_KEY_SOURCE_ENV,
        token_store_mod.KEY_SOURCE_FILESYSTEM,
    )
    account_a = ProviderTokenRecord(
        access_token="account-a-access",
        refresh_token="account-a-refresh",
        expires_at=time.time() + 3600,
        account_id="account-a",
    )
    save_provider_token("openai-codex", account_a)
    selected_adapter = OpenAICodexSubscriptionAuth()
    selected_scope = selected_adapter.route_credential_scope()
    provider_requests: list[httpx.Request] = []

    def unexpected_http(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        return httpx.Response(500)

    client = OpenAIResponsesClient(
        base_url=selected_adapter.base_url,
        api_key="",
        model="gpt-test",
        provider_auth=selected_adapter,
        transport=httpx.MockTransport(unexpected_http),
    )
    assert client.route_identity.credential_scope == selected_scope
    replacement = ProviderTokenRecord(
        access_token="account-b-access",
        refresh_token="account-b-refresh",
        expires_at=time.time() + 3600,
        account_id="account-b",
    )
    save_provider_token("openai-codex", replacement)
    replacement_adapter = OpenAICodexSubscriptionAuth()
    assert replacement_adapter.route_credential_scope() != selected_scope

    with pytest.raises(LLMError, match="account changed"):
        client.chat(messages=[{"role": "user", "content": "do not send"}])

    assert provider_requests == []


def test_responses_401_retry_rechecks_scope_before_sending_replacement_headers(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv(
        token_store_mod.OAUTH_KEY_SOURCE_ENV,
        token_store_mod.KEY_SOURCE_FILESYSTEM,
    )
    account_a = ProviderTokenRecord(
        access_token="account-a-access",
        refresh_token="account-a-refresh",
        expires_at=time.time() + 3600,
        account_id="account-a",
    )
    save_provider_token("openai-codex", account_a)
    adapter = OpenAICodexSubscriptionAuth()
    selected_scope = adapter.route_credential_scope()
    route_identity = build_provider_route_identity(
        protocol="openai_responses",
        base_url=adapter.base_url,
        provider_key="openai",
        model="gpt-test",
        profile_name="chatgpt-codex",
        auth_provider="openai-codex",
        credential_scope=selected_scope,
    )
    provider_requests: list[httpx.Request] = []
    refresh_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://auth.openai.com/api/accounts/oauth/token":
            refresh_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": "account-b-refreshed-access",
                    "refresh_token": "account-b-refreshed-refresh",
                    "expires_in": 3600,
                },
            )
        provider_requests.append(request)
        assert request.headers["Authorization"] == "Bearer account-a-access"
        save_provider_token(
            "openai-codex",
            ProviderTokenRecord(
                access_token="account-b-access",
                refresh_token="account-b-refresh",
                expires_at=time.time() + 3600,
                account_id="account-b",
            ),
        )
        return httpx.Response(401, json={"error": {"message": "expired"}})

    transport = httpx.MockTransport(handler)
    adapter = OpenAICodexSubscriptionAuth(transport=transport)
    client = OpenAIResponsesClient(
        base_url=adapter.base_url,
        api_key="",
        model="gpt-test",
        provider_auth=adapter,
        route_identity=route_identity,
        transport=transport,
    )

    with pytest.raises(LLMError, match="account changed"):
        client.chat(messages=[{"role": "user", "content": "first request only"}])

    assert len(provider_requests) == 1
    assert len(refresh_requests) == 1


class _FakeCodexAuth:
    provider_id = "fake-codex"
    display_name = "Fake"
    description = "Fake"
    base_url = "https://api.openai.com/v1"
    protocol = "openai_responses"
    supports_previous_response_id = False
    supports_temperature = False

    def __init__(self) -> None:
        self.force_refresh_values: list[bool] = []
        self.session_ids: list[str | None] = []
        self._dialect = OpenAICodexSubscriptionAuth()

    def authorization_headers(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        session_id: str | None = None,
    ) -> Mapping[str, str]:
        self.force_refresh_values.append(force_refresh)
        self.session_ids.append(session_id)
        return {
            "Authorization": f"Bearer {'new' if force_refresh else 'old'}",
            "ChatGPT-Account-Id": "account-1",
            "originator": "alysis",
        }

    def adapt_responses_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._dialect.adapt_responses_payload(payload)


class _FakeStreamingCodexAuth(_FakeCodexAuth):
    requires_streaming = True


def _attach_subscription_reasoning_capability(
    client: OpenAIResponsesClient,
) -> OpenAIResponsesClient:
    client.reasoning_trace_capability = resolve_reasoning_trace_capability(
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        model_supports_reasoning=True,
    )
    return client


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("adapter", ["auto", "none"])
@pytest.mark.parametrize("carrier", ["delta", "item_done", "completed"])
def test_sol_subscription_factory_requests_and_displays_summary(
    monkeypatch, stream, adapter, carrier
):
    from alysis_code.cli_impl.tui.surface import TuiSurface
    from alysis_code.cli_impl.tui.transcript import TuiTranscript
    from alysis_code.config import AppConfig
    from alysis_code.llm.factory import make_llm_client
    from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile

    auth = _FakeStreamingCodexAuth()
    monkeypatch.setattr("alysis_code.llm.factory.create_provider_auth", lambda *a, **k: auth)
    monkeypatch.setattr("alysis_code.provider_auth.create_provider_auth", lambda *a, **k: auth)
    cfg = AppConfig(model="gpt-6.1-sol")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="chatgpt-codex",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url=auth.base_url,
            auth_provider="openai-codex",
            default_model="gpt-6.1-sol",
            reasoning_effort="high",
            reasoning_trace_adapter=adapter,
        ),
    )
    set_active_profile(cfg, "chatgpt-codex")
    captured = {}
    summary = "Checking the changed files before reviewing their behavior."

    def handler(request):
        captured.update(json.loads(request.content))
        item = {
            "type": "reasoning",
            "id": "rs_sol",
            "summary": [{"type": "summary_text", "text": summary}],
        }
        events = []
        if carrier == "delta":
            events.append(
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": "rs_sol",
                    "output_index": 0,
                    "summary_index": 0,
                    "delta": summary,
                }
            )
        if carrier == "item_done":
            event = {"type": "response.output_item.done", "output_index": 0, "item": item}
            events.extend([event, event])
        events.append(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_sol",
                    "model": "gpt-6.1-sol",
                    "status": "completed",
                    "output": [item],
                    "output_text": "Done.",
                },
            }
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
        )

    client = make_llm_client(
        cfg=cfg, api_key="", model="gpt-6.1-sol", transport=httpx.MockTransport(handler)
    )
    assert client.provider_key == "openai"
    assert client.reasoning_trace_capability.has_safe_summary is (adapter == "auto")
    transcript = TuiTranscript()
    surface = TuiSurface(transcript)
    surface.set_trace_level("full")
    client.chat(
        messages=[{"role": "user", "content": "Review the changes."}],
        stream=stream,
        on_reasoning_delta=surface.on_reasoning_token,
    )
    assert captured["reasoning"] == {
        "effort": "high",
        **({"summary": "auto"} if adapter == "auto" else {}),
    }
    assert [text for role, text in transcript.entries if role == "reasoning"] == (
        [summary] if adapter == "auto" else []
    )


def test_subscription_non_public_stream_requests_and_surfaces_reasoning_summary() -> None:
    auth = _FakeStreamingCodexAuth()
    captured: dict[str, Any] = {}
    summaries: list[str] = []
    reasoning_item = {
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "Checked the constraints."}],
    }

    def sse(event_type: str, data: dict[str, Any]) -> bytes:
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        events = [
            (
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": "rs_subscription",
                    "output_index": 0,
                    "summary_index": 0,
                    "delta": "Checked the constraints.",
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_subscription_summary",
                        "model": "gpt-test",
                        "status": "completed",
                        "output_text": "Done.",
                        "output": [reasoning_item],
                    },
                },
            ),
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"".join(sse(event_type, data) for event_type, data in events),
        )

    client = _attach_subscription_reasoning_capability(
        OpenAIResponsesClient(
            base_url="https://api.openai.com/v1",
            api_key="",
            model="gpt-test",
            reasoning_effort="high",
            provider_auth=auth,
            transport=httpx.MockTransport(handler),
        )
    )

    response = client.chat(
        messages=[{"role": "user", "content": "inspect"}],
        stream=False,
        on_reasoning_delta=summaries.append,
    )

    assert captured["stream"] is True
    assert captured["reasoning"] == {"effort": "high", "summary": "auto"}
    assert response.content == "Done."
    assert [item.text for item in response.reasoning] == ["Checked the constraints."]
    assert summaries == ["Checked the constraints."]


def test_responses_client_retries_one_401_with_refreshed_subscription_auth() -> None:
    auth = _FakeCodexAuth()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "gpt-test",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="",
        model="gpt-test",
        prompt_cache_key="session-1",
        reasoning_effort="medium",
        provider_auth=auth,
        session_id="agent-session-1",
        transport=httpx.MockTransport(handler),
    )
    result = client.chat(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hi"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert result.content == "hello"
    assert auth.force_refresh_values == [False, True]
    assert auth.session_ids == ["agent-session-1", "agent-session-1"]
    assert requests[0].headers["authorization"] == "Bearer old"
    assert requests[1].headers["authorization"] == "Bearer new"
    sent = json.loads(requests[1].content)
    assert sent["instructions"] == "system"
    assert sent["store"] is False
    assert sent["reasoning"] == {"effort": "medium"}
    assert sent["tools"][0]["tools"][0]["strict"] is False
    assert "previous_response_id" not in sent
    assert "temperature" not in sent
