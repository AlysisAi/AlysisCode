"""Tests for the full-screen TUI setup wizard.

The :class:`SetupFlow` is driven synchronously (no terminal) for the bulk of the
coverage; a couple of headless ``run_setup_tui`` smokes exercise the prompt_toolkit
application through a pipe input + dummy output with ``inline_busy`` so a
pre-loaded key sequence walks the whole flow deterministically.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import alysis_code.account_login as account_login
import alysis_code.sandbox_doctor as sandbox_doctor
from alysis_code.cli_impl.tui import setup_flow as flow_mod
from alysis_code.cli_impl.tui.setup_app import run_setup_tui
from alysis_code.cli_impl.tui.setup_flow import SetupFlow
from alysis_code.config import (
    AppConfig,
    ConfigError,
    load_config,
    load_persisted_profile_keys,
    save_config,
)
from alysis_code.provider_auth import ProviderAccountStatus, ProviderModel

# --------------------------------------------------------------------------- helpers


def _config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    for var in (
        "ALYSIS_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "ZAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _fake_diag(*, ready: bool, can_pull: bool = False, backend: str = "docker") -> SimpleNamespace:
    return SimpleNamespace(
        ready=ready,
        status="ready" if ready else "not_ready",
        selected_backend=backend if ready else None,
        docker_image="img:dev",
        can_pull=can_pull,
    )


def _patch_validate(
    monkeypatch: pytest.MonkeyPatch, status: str = "validated", message: str = ""
) -> None:
    monkeypatch.setattr(
        flow_mod._wiz,
        "_validate_api_key",
        lambda **_k: flow_mod._wiz._ApiKeyValidationResult(status=status, message=message),
    )


def _drive_busy(flow: SetupFlow) -> None:
    """Run busy steps until the flow lands on an interactive screen."""
    guard = 0
    while flow.current_mode() == "busy":
        flow.run_busy()
        guard += 1
        assert guard < 20, "busy chain did not converge"


def _start_native(flow: SetupFlow) -> None:
    """Advance past the welcome screen onto the merged provider picker.

    The caller then chooses an API-key provider row (native execution is
    implied by the row, not a separate step).
    """
    flow.advance_message()
    assert flow.stage == "connect_provider"


def _start_subscription(flow: SetupFlow, runtime_id: str = "openai-codex") -> None:
    flow.advance_message()
    assert flow.stage == "connect_provider"
    flow.choose(f"{flow_mod._wiz._RUNTIME_EXECUTION_PREFIX}{runtime_id}")
    assert flow.stage == "workspace"


class FakeSubscriptionAdapter:
    provider_id = "openai-codex"
    display_name = "ChatGPT subscription"
    description = "test"
    profile_name = "chatgpt-codex"
    auth_hint = "test"
    base_url = "https://api.openai.com/v1"
    protocol = "openai_responses"
    supports_previous_response_id = False

    def __init__(
        self,
        *,
        connected: bool,
        account_label: str | None = None,
        detail: str | None = None,
        login_error: Exception | None = None,
    ) -> None:
        self.connected = connected
        self.account_label = account_label
        self.detail = detail
        self.login_error = login_error
        self.login_calls: list[str] = []

    def account_status(self) -> ProviderAccountStatus:
        return ProviderAccountStatus(
            connected=self.connected,
            account_label=self.account_label,
            detail=self.detail,
        )

    def login(self, method: str = "browser", **_kwargs: Any) -> ProviderAccountStatus:
        self.login_calls.append(method)
        if self.login_error is not None:
            raise self.login_error
        self.connected = True
        return self.account_status()

    def list_models(self, *, refresh: bool = False) -> tuple[ProviderModel, ...]:
        assert refresh is True
        return (ProviderModel(id="gpt-test", label="GPT Test", is_default=True),)


def _patch_subscription_adapter(
    monkeypatch: pytest.MonkeyPatch,
    adapter: FakeSubscriptionAdapter,
) -> None:
    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda _provider_id: adapter,
    )


# --------------------------------------------------------------------------- flow: happy path


def test_flow_full_happy_path_persists(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)
    monkeypatch.setattr(
        sandbox_doctor, "diagnose_sandbox", lambda _cfg, **_k: _fake_diag(ready=True)
    )

    flow = SetupFlow()
    assert flow.screen().stage == "welcome"
    _start_native(flow)
    flow.choose("openai")  # compat OpenAI (requires a key)
    assert flow.stage == "api_key"
    flow.submit_input("sk-test-123")
    _drive_busy(flow)  # validate key -> model
    assert flow.stage == "model"
    flow.choose("gpt-5.5")
    _drive_busy(flow)  # validate default model -> workspace
    assert flow.stage == "workspace"
    flow.submit_input(os.fspath(tmp_path))
    _drive_busy(flow)  # commit -> diagnose -> complete
    assert flow.stage == "complete"

    cfg = load_config()
    assert cfg.model == "gpt-5.5"
    # No router step remains, so setup never writes role_models.
    assert "role_models" not in cfg.extra_fields
    assert load_persisted_profile_keys().get("openai") == "sk-test-123"
    # Summary reflects a validated key + ready sandbox.
    summary = " ".join(text for text, _tone in flow._summary_lines())
    assert "validated" in summary
    assert "ready" in summary

    # Enter on the complete screen finishes with success.
    flow.advance_message()
    assert flow.stage == "done"
    assert flow.success is True


def test_flow_nvidia_selects_live_deepseek_then_verified_reasoning(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)
    monkeypatch.setattr(
        flow_mod._wiz,
        "_discover_setup_provider_models",
        lambda *_args, **_kwargs: (
            (
                flow_mod._wiz.ProviderModelOption(
                    id="deepseek-ai/deepseek-v4-pro",
                    label="deepseek-ai/deepseek-v4-pro",
                ),
                flow_mod._wiz.ProviderModelOption(
                    id="meta/llama-3.3-70b-instruct",
                    label="meta/llama-3.3-70b-instruct",
                ),
            ),
            "",
        ),
    )

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("nvidia")
    flow.submit_input("nvapi-test")
    _drive_busy(flow)

    assert flow.stage == "model"
    assert "deepseek-ai/deepseek-v4-pro" in {row.value for row in flow.screen().rows}
    assert "meta/llama-3.3-70b-instruct" in {row.value for row in flow.screen().rows}

    flow.choose("deepseek-ai/deepseek-v4-pro")
    _drive_busy(flow)
    assert flow.stage == "model_thinking"
    assert [row.value for row in flow.screen().rows] == ["off", "high", "max", "auto"]

    flow.choose("max")
    assert flow.stage == "workspace"
    assert flow.model_result is not None
    assert flow.model_result.reasoning_label == "max"


def test_flow_zai_coding_plan_exposes_reasoning_floor(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("zai-coding-plan")
    flow.submit_input("zai-test")
    _drive_busy(flow)

    assert flow.stage == "model"
    assert "glm-5.3" in {row.value for row in flow.screen().rows}

    flow.choose("glm-5.3")
    _drive_busy(flow)
    assert flow.stage == "model_thinking"
    assert [row.value for row in flow.screen().rows] == ["low", "high", "max", "auto"]

    flow.choose("max")
    assert flow.stage == "workspace"
    assert flow.model_result is not None
    assert flow.model_result.reasoning_label == "max"


def test_flow_optional_key_provider_skips_validation(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sandbox_doctor, "diagnose_sandbox", lambda _cfg, **_k: _fake_diag(ready=True)
    )

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("ollama")  # local: api_key_env is None
    assert flow.stage == "api_key"
    flow.submit_input("")  # empty key is allowed for this provider
    assert flow.stage == "model"
    assert flow.api_key_result is not None and flow.api_key_result.validation_status == "skipped"


def test_flow_subscription_skips_api_key_provider_steps(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_subscription_adapter(
        monkeypatch,
        FakeSubscriptionAdapter(connected=False, detail="Not logged in."),
    )

    flow = SetupFlow()
    flow.advance_message()
    assert flow.stage == "connect_provider"
    picker_screen = flow.screen()
    assert picker_screen.title == "Connect a Provider"
    runtime_value = f"{flow_mod._wiz._RUNTIME_EXECUTION_PREFIX}openai-codex"
    picker_rows = {row.value: row for row in picker_screen.rows}
    # Subscription sign-ins and API-key providers share one merged list.
    assert runtime_value in picker_rows
    assert picker_rows[runtime_value].label == "ChatGPT subscription"
    assert "openai-responses" in picker_rows

    flow.back()
    assert flow.stage == "welcome"
    flow.advance_message()
    assert flow.stage == "connect_provider"

    flow.choose(runtime_value)
    assert flow.stage == "workspace"
    assert flow.screen().progress.startswith("Step 3 of 3")
    flow.back()
    assert flow.stage == "connect_provider"
    flow.choose(runtime_value)
    assert flow.stage == "workspace"
    assert flow.profile_result is None
    assert flow.api_key_result is None
    assert flow.model_result is None

    flow.submit_input(os.fspath(tmp_path))
    _drive_busy(flow)
    assert flow.stage == "runtime_login_confirm"
    connect_screen = flow.screen()
    assert connect_screen.confirm_default is True
    assert "immediate external side effect" in " ".join(text for text, _ in connect_screen.lines)
    flow.request_cancel()
    cancel_copy = " ".join(text for text, _tone in flow.screen().lines)
    assert "settings are already saved" in cancel_copy
    assert "No changes will be saved" not in cancel_copy
    flow.confirm(False)
    assert flow.stage == "runtime_login_confirm"
    flow.confirm(False)
    assert flow.stage == "complete"
    assert flow.sandbox_result is None

    cfg = load_config()
    assert cfg.execution.backend == "native"
    assert cfg.execution.runtime is None
    assert cfg.agent_runtimes == {}
    assert cfg.extra_fields["profiles"]["chatgpt-codex"]["auth_provider"] == "openai-codex"
    summary = " ".join(text for text, _tone in flow._summary_lines())
    assert "Connection  AI subscription" in summary
    assert "Provider    ChatGPT subscription" in summary
    assert "Sign-in     not connected" in summary
    assert "(delegated)" not in summary
    assert "delegated run" not in summary.lower()
    assert "alysis auth login openai-codex" in summary


def test_flow_subscription_reuses_existing_provider_auth(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    adapter = FakeSubscriptionAdapter(
        connected=True,
        detail="Logged in using ChatGPT",
    )
    _patch_subscription_adapter(monkeypatch, adapter)
    monkeypatch.setattr(
        sandbox_doctor,
        "diagnose_sandbox",
        lambda _cfg, **_kwargs: _fake_diag(ready=True),
    )

    flow = SetupFlow()
    _start_subscription(flow)
    flow.submit_input(os.fspath(tmp_path))
    _drive_busy(flow)

    assert flow.stage == "complete"
    assert flow.runtime_auth_connected is True
    assert adapter.login_calls == []
    summary = " ".join(text for text, _tone in flow._summary_lines())
    assert "Sign-in     connected (Logged in using ChatGPT)" in summary


def test_flow_subscription_browser_login_runs_in_terminal(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    adapter = FakeSubscriptionAdapter(
        connected=False,
        account_label="developer@example.test",
    )
    _patch_subscription_adapter(monkeypatch, adapter)
    monkeypatch.setattr(
        sandbox_doctor,
        "diagnose_sandbox",
        lambda _cfg, **_kwargs: _fake_diag(ready=True),
    )

    flow = SetupFlow()
    _start_subscription(flow)
    flow.submit_input(os.fspath(tmp_path))
    _drive_busy(flow)
    assert flow.stage == "runtime_login_confirm"
    flow.confirm(True)
    assert flow.stage == "runtime_logging_in"
    assert flow.busy_kind() == "terminal"
    assert flow.screen().busy_kind == "terminal"
    _drive_busy(flow)

    assert flow.stage == "complete"
    assert adapter.login_calls == ["browser"]
    assert flow.runtime_auth_connected is True
    assert "connected as developer@example.test" in flow.runtime_auth_summary


def test_flow_subscription_login_failure_is_non_fatal(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_subscription_adapter(
        monkeypatch,
        FakeSubscriptionAdapter(
            connected=False,
            login_error=RuntimeError("browser login closed"),
        ),
    )

    flow = SetupFlow()
    _start_subscription(flow)
    flow.submit_input(os.fspath(tmp_path))
    _drive_busy(flow)
    flow.confirm(True)
    _drive_busy(flow)

    assert flow.stage == "complete"
    assert flow.runtime_auth_connected is False
    assert "browser login closed" in flow.runtime_auth_summary
    assert "alysis auth login openai-codex" in " ".join(
        text for text, _tone in flow._summary_lines()
    )
    assert flow.success is None


def test_provider_screen_surfaces_hosted_providers_with_advanced_branch():
    # The merged "Connect a Provider" picker surfaces subscription sign-ins
    # (including the Alysis Code account itself) and every hosted provider
    # (DeepSeek, OpenRouter, …) side by side, so users aren't limited to the
    # big-three brands. Only local, compatibility, custom, and legacy presets
    # stay behind the "Advanced" branch.
    # Asserts the *displayed rows*, not just handler behaviour.
    flow = SetupFlow()
    _start_native(flow)

    rows = flow.screen().rows
    values = [r.value for r in rows]
    # The Alysis Code account is always row 1, then subscription runtimes, then
    # the hosted API-key providers.
    assert values[0] == "alysis"
    assert values[1].startswith(flow_mod._wiz._RUNTIME_EXECUTION_PREFIX)
    assert values.index("alysis") < values.index("openai-responses")
    labels = [r.label for r in rows]
    # No row over-promises: nothing claims a trial or a recommendation.
    assert not any("free trial" in label for label in labels)
    assert sum("recommended" in label.lower() for label in labels) == 0
    assert values[-1] == flow_mod._wiz._ADVANCED_PROVIDER_PRESETS_VALUE
    assert "xiaomi-mimo" in values  # BYOK Xiaomi MiMo is a regular hosted row
    assert "deepseek" in values  # hosted providers sit on the primary screen…
    assert "openrouter" in values
    assert "ollama" not in values  # …local endpoints stay behind the advanced branch

    # The description column carries the auth method per row.
    by_value = {r.value: r for r in rows}
    assert by_value["openai-responses"].description == "API key"
    assert by_value["xiaomi-mimo"].description == "API key"

    # The advanced branch holds the local / compatibility / custom / legacy
    # presets plus the account-gated hosted Alysis Code entry.
    flow.choose(flow_mod._wiz._ADVANCED_PROVIDER_PRESETS_VALUE)
    assert flow.stage == "provider_advanced"
    advanced_values = [r.value for r in flow.screen().rows]
    assert "alysis" in advanced_values
    assert "deepseek" not in advanced_values  # promoted to the primary screen
    assert "ollama" in advanced_values
    assert "custom" in advanced_values
    assert flow.screen().progress.startswith("Step 2 of 6")
    assert "Provider (advanced)" in flow.screen().progress

    flow.back()
    assert flow.stage == "connect_provider"

    # Choosing a hosted provider directly (no advanced hop) proceeds to the key step.
    flow.choose("deepseek")
    assert flow.stage == "api_key"
    assert flow.profile_result is not None
    assert flow.profile_result.preset is not None
    assert flow.profile_result.preset.key == "deepseek"


def test_provider_advanced_back_returns_to_provider():
    flow = SetupFlow()
    _start_native(flow)
    flow.choose(flow_mod._wiz._ADVANCED_PROVIDER_PRESETS_VALUE)
    assert flow.stage == "provider_advanced"
    flow.back()
    assert flow.stage == "connect_provider"


# --------------------------------------------------------------------------- flow: validation


def test_flow_key_validation_failure_retries_then_continues(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch, status="failed", message="bad key")

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("openai")
    for _ in range(flow_mod._MAX_KEY_ATTEMPTS - 1):
        flow.submit_input("sk-bad")
        flow.run_busy()
        assert flow.stage == "api_key"  # bounced back to retry
        assert flow.status_tone == "err"
    # Final attempt continues with the last key rather than blocking forever.
    flow.submit_input("sk-bad")
    flow.run_busy()
    assert flow.stage == "model"
    assert flow.api_key_result.validation_status == "failed"


def test_flow_custom_model_not_found_confirm(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)

    # Key validates, but the chosen model is reported missing.
    calls = {"n": 0}

    def _validate(**kwargs: Any):
        calls["n"] += 1
        if kwargs.get("model"):
            return flow_mod._wiz._ApiKeyValidationResult(
                status="model_not_found", message="no such model"
            )
        return flow_mod._wiz._ApiKeyValidationResult(status="validated")

    monkeypatch.setattr(flow_mod._wiz, "_validate_api_key", _validate)

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("openai")
    flow.submit_input("sk-test")
    _drive_busy(flow)  # -> model
    flow.choose(flow_mod._wiz._CUSTOM_MODEL_VALUE)
    assert flow.stage == "custom_model"
    flow.submit_input("totally-made-up")
    _drive_busy(flow)
    assert flow.stage == "model_not_found_confirm"
    flow.confirm(True)  # use it anyway
    assert flow.stage == "workspace"
    assert flow.api_key_result.validation_status == "inconclusive"
    assert "totally-made-up" in flow.api_key_result.validation_message


def test_flow_has_no_router_step_and_profile_switch_drops_legacy_key(monkeypatch, tmp_path):
    # The router-model step is gone; the flow moves model -> workspace and the
    # summary renders no Router line. The legacy role_models.router key is
    # still removed — by the profile-switch hygiene in set_active_profile when
    # commit swaps the migrated "default" profile for the chosen provider —
    # while other unexposed role keys survive.
    _config_env(tmp_path, monkeypatch)
    cfg = AppConfig()
    cfg.extra_fields = {"role_models": {"coding": "coding-model", "router": "old-router-model"}}
    save_config(cfg)
    monkeypatch.setattr(
        sandbox_doctor,
        "diagnose_sandbox",
        lambda _cfg, **_kwargs: _fake_diag(ready=True),
    )

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("ollama")
    flow.submit_input("")
    flow.choose("llama3.3")
    _drive_busy(flow)

    assert flow.stage == "workspace"
    flow.back()
    assert flow.stage == "model"
    flow.choose("llama3.3")
    _drive_busy(flow)
    assert flow.stage == "workspace"

    flow.request_cancel()
    assert flow.stage == "cancel_confirm"
    flow.confirm(False)
    assert flow.stage == "workspace"

    flow.submit_input(os.fspath(tmp_path))
    _drive_busy(flow)

    assert flow.stage == "complete"
    role_models = load_config().extra_fields["role_models"]
    assert role_models == {"coding": "coding-model"}
    summary_lines = [text for text, _tone in flow._summary_lines()]
    assert not any(text.startswith("Router") for text in summary_lines)


def test_flow_workspace_invalid_path_stays(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("ollama")
    flow.submit_input("")  # skip key
    flow.choose("llama3.3")
    _drive_busy(flow)
    assert flow.stage == "workspace"
    target_file = tmp_path / "afile.txt"
    target_file.write_text("x", encoding="utf-8")
    flow.submit_input(os.fspath(target_file))  # not a directory
    assert flow.stage == "workspace"
    assert flow.status_tone == "err"


def test_flow_workspace_missing_folder_confirms_and_creates(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)

    flow = SetupFlow()
    flow.stage = "workspace"
    missing = tmp_path / "new-project"
    flow.submit_input(os.fspath(missing))

    assert flow.stage == "workspace_create_confirm"
    screen = flow.screen()
    assert screen.progress.startswith("Step 5 of 6")
    assert "create_if_missing" not in screen.subtitle
    assert any(os.fspath(missing.resolve()) in text for text, _tone in screen.lines)

    flow.confirm(True)

    assert missing.is_dir()
    assert flow.stage == "committing"
    assert flow.workspace_result is not None
    assert flow.workspace_result.workspace == os.fspath(missing.resolve())


def test_flow_progress_counts_sandbox_as_setup_step():
    flow = SetupFlow()
    assert flow._progress("model").startswith("Step 4 of 6")
    flow.stage = "workspace"
    assert flow.screen().progress.startswith("Step 5 of 6")
    flow.stage = "sandbox_choice"
    assert flow.screen().progress.startswith("Step 6 of 6")
    assert "Sandbox" in flow.screen().progress


# --------------------------------------------------------------------------- flow: custom profile


def test_flow_custom_profile_builds_openai_compat(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("custom")
    assert flow.stage == "custom_name"
    flow.submit_input("myco")
    assert flow.stage == "custom_url"
    flow.submit_input("https://api.example.com/v1")
    assert flow.stage == "custom_headers"
    flow.submit_input("x-api-key=abc, x-org=acme")  # header-authenticated endpoint
    assert flow.stage == "api_key"
    profile = flow.profile_result.profile
    assert profile.name == "myco"
    assert profile.protocol == "openai_compat"
    assert profile.base_url == "https://api.example.com/v1"
    assert profile.extra_headers == {"x-api-key": "abc", "x-org": "acme"}
    # Back from api_key returns through the headers step to the URL step.
    flow.back()
    assert flow.stage == "custom_headers"
    flow.back()
    assert flow.stage == "custom_url"


def test_flow_custom_profile_malformed_headers_reprompt(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    flow = SetupFlow()
    _start_native(flow)
    flow.choose("custom")
    flow.submit_input("")  # name defaults to "custom"
    flow.submit_input("https://api.example.com/v1")
    assert flow.stage == "custom_headers"
    flow.submit_input("not-a-header")  # missing '=' -> re-prompt
    assert flow.stage == "custom_headers"
    assert flow.status_tone == "err"
    flow.submit_input("")  # empty -> no headers, proceed
    assert flow.stage == "api_key"
    assert flow.profile_result.profile.extra_headers == {}


# --------------------------------------------------------------------------- flow: sandbox branches


def _to_sandbox(flow: SetupFlow, tmp_path: Path) -> None:
    _start_native(flow)
    flow.choose("ollama")
    flow.submit_input("")
    flow.choose("llama3.3")
    _drive_busy(flow)
    assert flow.stage == "workspace"
    flow.submit_input(os.fspath(tmp_path))
    _drive_busy(flow)  # commit -> diagnose (-> next sandbox screen)


def test_flow_sandbox_pull_confirm_yes(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)
    seq = [_fake_diag(ready=False, can_pull=True), _fake_diag(ready=True)]
    monkeypatch.setattr(sandbox_doctor, "diagnose_sandbox", lambda _cfg, **_k: seq.pop(0))
    monkeypatch.setattr(
        sandbox_doctor,
        "pull_sandbox_images",
        lambda **_k: SimpleNamespace(ok=True, error=None, results=[]),
    )

    _to_sandbox(flow := SetupFlow(), tmp_path)
    assert flow.stage == "sandbox_pull_confirm"
    flow.confirm(True)
    _drive_busy(flow)  # pull -> recheck -> complete
    assert flow.stage == "complete"
    assert flow.sandbox_result.ready is True


def test_flow_sandbox_no_backend_disable(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)
    monkeypatch.setattr(
        sandbox_doctor,
        "diagnose_sandbox",
        lambda _cfg, **_k: _fake_diag(ready=False, can_pull=False),
    )
    monkeypatch.setattr(sandbox_doctor, "detect_bubblewrap_install_plan", lambda: None)

    _to_sandbox(flow := SetupFlow(), tmp_path)
    assert flow.stage == "sandbox_choice"
    # With no install plan, only disable / later are offered.
    values = [r.value for r in flow.screen().rows]
    assert values == ["disable", "later"]
    flow.choose("disable")
    _drive_busy(flow)
    assert flow.stage == "complete"
    assert flow.sandbox_result.status == "disabled"
    # Both sandbox keys were written off (strict invariant) — config reloads clean.
    cfg = load_config()
    assert cfg is not None


# --------------------------------------------------------------------------- flow: hosted Alysis Code login


def test_flow_hosted_alysis_offers_login(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch, status="skipped")
    monkeypatch.setattr(
        sandbox_doctor, "diagnose_sandbox", lambda _cfg, **_k: _fake_diag(ready=True)
    )
    monkeypatch.setattr(
        account_login, "login", lambda _cfg, **_k: SimpleNamespace(email="me@example.com")
    )

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("alysis")  # hosted account: skips API key AND model steps
    assert flow.stage == "workspace"
    assert flow.api_key_result is not None
    assert flow.api_key_result.validation_status == "skipped"
    assert flow.model_result is not None
    assert flow.model_result.model == "gpt-6-luna"
    flow.submit_input(os.fspath(tmp_path))
    _drive_busy(flow)  # commit -> diagnose -> login_confirm
    assert flow.stage == "login_confirm"
    assert flow.screen().subtitle == "Your provider is the Alysis Code endpoint."
    flow.confirm(True)
    _drive_busy(flow)  # logging_in -> complete
    assert flow.stage == "complete"
    assert "me@example.com" in flow.login_summary


# --------------------------------------------------------------------------- flow: cancel / back


def test_flow_cancel_at_welcome(monkeypatch):
    flow = SetupFlow()
    flow.request_cancel()
    assert flow.stage == "cancel_confirm"
    flow.confirm(False)  # keep going
    assert flow.stage == "welcome"
    flow.request_cancel()
    flow.confirm(True)  # cancel for real
    assert flow.stage == "done"
    assert flow.success is False


def test_flow_back_navigation(monkeypatch):
    _patch_validate(monkeypatch)
    flow = SetupFlow()
    _start_native(flow)
    flow.choose("openai")
    assert flow.stage == "api_key"
    flow.back()
    assert flow.stage == "connect_provider"
    flow.back()
    assert flow.stage == "welcome"


# --------------------------------------------------------------------------- headless application


def _headless(keys: str, **kwargs: Any) -> bool:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return run_setup_tui(
            owl_color=False, input=pipe, output=DummyOutput(), inline_busy=True, **kwargs
        )


def test_headless_cancel_returns_false(tmp_path, monkeypatch):
    _config_env(tmp_path, monkeypatch)
    # welcome Enter -> provider picker; Ctrl+C exits immediately (no confirm to get stuck on).
    assert _headless("\r\x03") is False


def test_headless_ctrl_c_exits_from_input_step(tmp_path, monkeypatch):
    _config_env(tmp_path, monkeypatch)
    # welcome -> picker(down to row 1 = openai-responses, API key) -> api_key input;
    # Ctrl+C must still exit.
    assert _headless("\r\x1b[B\r\x03") is False


def test_setup_input_plain_selection_backspace_cuts_selected_span(monkeypatch):
    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.selection import SelectionType

    from alysis_code.cli_impl.tui import setup_app as setup_app_module
    from alysis_code.cli_impl.tui.setup_flow import Screen

    submitted = []

    class InputFlow:
        stage = "field"
        success = False

        def set_report(self, callback):
            self.report = callback

        def current_mode(self):
            return "input"

        def screen(self):
            return Screen(
                stage="field",
                mode="input",
                title="Field",
                input_label="Value",
                progress="test",
            )

        def submit_input(self, text):
            submitted.append(text)
            self.success = True
            self.stage = "done"

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)

        def select_input_text():
            buffer = application.layout.current_buffer
            buffer.text = "abcd"
            buffer.cursor_position = 4
            buffer.start_selection(selection_type=SelectionType.CHARACTERS)
            buffer.cursor_position = 2

        application.pre_run_callables.append(select_input_text)
        return application

    monkeypatch.setattr(setup_app_module, "SetupFlow", InputFlow)
    monkeypatch.setattr(setup_app_module, "Application", capture_application)

    assert _headless("\x7f\r") is True
    assert submitted == ["ab"]


def test_headless_full_path_saves(tmp_path, monkeypatch):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)
    monkeypatch.setattr(
        sandbox_doctor, "diagnose_sandbox", lambda _cfg, **_k: _fake_diag(ready=True)
    )

    # welcome -> picker(row 2 openai-responses — row 0 is the Alysis Code
    # account, row 1 the ChatGPT runtime; key required) -> type key ->
    # model(idx0) -> workspace(default home, Enter) -> complete -> done.
    keys = "\r" + "\x1b[B" * 2 + "\r" + "sk-xyz" + "\r" + "\r" + "\r" + "\r"
    assert _headless(keys) is True
    cfg = load_config()
    assert cfg.model  # a default model was persisted


# --------------------------------------------------------------------------- regression: more paths


def test_flow_preset_model_not_found_bounces_to_picker(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)

    def _validate(**kwargs: Any):
        if kwargs.get("model"):
            return flow_mod._wiz._ApiKeyValidationResult(status="model_not_found", message="gone")
        return flow_mod._wiz._ApiKeyValidationResult(status="validated")

    monkeypatch.setattr(flow_mod._wiz, "_validate_api_key", _validate)

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("openai")
    flow.submit_input("sk-test")
    _drive_busy(flow)
    flow.choose("gpt-5.5")  # a PRESET model (custom=False) -> else branch
    _drive_busy(flow)
    assert flow.stage == "model"  # bounced back to the picker, not the confirm
    assert flow.status_tone == "err"
    assert flow.api_key_result.validation_status == "model_not_found"
    # The summary still renders sanely after a preset miss.
    summary = " ".join(t for t, _tone in flow._summary_lines())
    assert "model validation failed" in summary
    assert flow._api_key_tone() == "err"


def test_flow_fatal_commit_error(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)
    monkeypatch.setattr(
        sandbox_doctor, "diagnose_sandbox", lambda _cfg, **_k: _fake_diag(ready=True)
    )

    def _boom(**_k: Any):
        raise ConfigError("disk full")

    monkeypatch.setattr(flow_mod._wiz, "_commit_setup", _boom)

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("ollama")
    flow.submit_input("")
    flow.choose("llama3.3")
    _drive_busy(flow)
    assert flow.stage == "workspace"
    flow.submit_input(os.fspath(tmp_path))
    _drive_busy(flow)  # committing -> fatal (NOT complete)
    assert flow.stage == "fatal"
    assert "disk full" in flow.fatal_error
    flow.advance_message()
    assert flow.stage == "done"
    assert flow.success is False  # a save failure must not claim success


def test_flow_hosted_alysis_login_failure_is_non_fatal(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch, status="skipped")
    monkeypatch.setattr(
        sandbox_doctor, "diagnose_sandbox", lambda _cfg, **_k: _fake_diag(ready=True)
    )

    def _login_boom(_cfg: Any, **_k: Any):
        raise RuntimeError("network down")

    monkeypatch.setattr(account_login, "login", _login_boom)

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("alysis")  # skips API key + model, lands on workspace
    assert flow.stage == "workspace"
    flow.submit_input(os.fspath(tmp_path))
    _drive_busy(flow)  # -> login_confirm
    assert flow.stage == "login_confirm"
    flow.confirm(True)
    _drive_busy(flow)  # logging_in raises internally -> complete, NOT fatal
    assert flow.stage == "complete"
    assert flow.login_ok is False
    assert "not connected" in flow.login_summary and "network down" in flow.login_summary
    acct_tone = next(tone for text, tone in flow._summary_lines() if text.startswith("Account"))
    assert acct_tone == "warn"  # a failed login is not shown in the green success tone
    assert flow.success is None  # setup is not marked failed


def test_flow_sandbox_diagnose_raises_is_contained(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)

    def _raise(*_a: Any, **_k: Any):
        raise RuntimeError("docker daemon down")

    monkeypatch.setattr(sandbox_doctor, "diagnose_sandbox", _raise)

    _to_sandbox(flow := SetupFlow(), tmp_path)  # commit -> diagnose (raises, contained)
    assert flow.stage == "complete"
    assert flow.sandbox_result.status == "check failed"
    assert flow.status_tone == "warn"


def test_flow_sandbox_pull_raises_is_contained(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)
    monkeypatch.setattr(
        sandbox_doctor,
        "diagnose_sandbox",
        lambda _cfg, **_k: _fake_diag(ready=False, can_pull=True),
    )

    def _raise(**_k: Any):
        raise RuntimeError("registry unreachable")

    monkeypatch.setattr(sandbox_doctor, "pull_sandbox_images", _raise)

    _to_sandbox(flow := SetupFlow(), tmp_path)
    assert flow.stage == "sandbox_pull_confirm"
    flow.confirm(True)
    _drive_busy(flow)  # pull raises internally -> contained, not fatal
    assert flow.stage == "complete"
    assert flow.sandbox_result.status == "pull failed"
    assert flow.status_tone == "warn"


def test_flow_sandbox_install_branch(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)
    seq = [_fake_diag(ready=False, can_pull=False), _fake_diag(ready=True)]
    monkeypatch.setattr(sandbox_doctor, "diagnose_sandbox", lambda _cfg, **_k: seq.pop(0))
    monkeypatch.setattr(
        sandbox_doctor,
        "detect_bubblewrap_install_plan",
        lambda: SimpleNamespace(display="apt-get install -y bubblewrap"),
    )
    monkeypatch.setattr(
        sandbox_doctor, "install_bubblewrap", lambda **_k: SimpleNamespace(ok=True, detail="")
    )

    _to_sandbox(flow := SetupFlow(), tmp_path)
    assert flow.stage == "sandbox_choice"
    assert [r.value for r in flow.screen().rows] == ["install_bwrap", "disable", "later"]
    flow.choose("install_bwrap")
    assert flow.stage == "installing_sandbox"
    assert flow.busy_kind() == "terminal"  # runs via run_in_terminal, not a worker
    _drive_busy(flow)  # install -> recheck(ready) -> complete
    assert flow.stage == "complete"
    assert flow.sandbox_result.ready is True


def test_flow_sandbox_install_failure_maps_to_not_ready(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)
    monkeypatch.setattr(
        sandbox_doctor,
        "diagnose_sandbox",
        lambda _cfg, **_k: _fake_diag(ready=False, can_pull=False),
    )
    monkeypatch.setattr(
        sandbox_doctor, "detect_bubblewrap_install_plan", lambda: SimpleNamespace(display="apt ...")
    )
    monkeypatch.setattr(
        sandbox_doctor,
        "install_bubblewrap",
        lambda **_k: SimpleNamespace(ok=False, detail="no package manager"),
    )

    _to_sandbox(flow := SetupFlow(), tmp_path)
    flow.choose("install_bwrap")
    _drive_busy(flow)
    assert flow.stage == "complete"
    assert flow.sandbox_result.status == "not ready"
    assert flow.status_tone == "warn"


def test_flow_api_key_keep_current_on_return(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    _patch_validate(monkeypatch)

    flow = SetupFlow()
    _start_native(flow)
    flow.choose("openai")  # key-required provider
    flow.submit_input("sk-keep")
    _drive_busy(flow)  # -> model
    flow.back()  # model -> api_key, with the validated key still held
    assert flow.stage == "api_key"
    attempts_before = flow._key_attempts
    flow.submit_input("")  # empty submit keeps the current key (no re-paste, no penalty)
    assert flow.stage == "model"
    assert flow._key_attempts == attempts_before
    assert flow.api_key_result.api_key == "sk-keep"


def test_flow_empty_required_key_does_not_consume_retry_budget(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    flow = SetupFlow()
    _start_native(flow)
    flow.choose("openai")  # key required, none entered yet
    flow.submit_input("")
    assert flow.stage == "api_key"
    flow.submit_input("")
    assert flow.stage == "api_key"
    assert flow._key_attempts == 0  # empty submits must not erode the failure budget


# --------------------------------------------------------------------------- wiring regression


def test_wiring_invokes_setup_tui_when_enabled_and_interactive(monkeypatch):
    """Regression for the import bug that left the setup TUI dead on arrival."""
    from alysis_code.cli_impl import tui as tui_pkg
    from alysis_code.cli_impl.commands import startup
    from alysis_code.cli_impl.tui import setup_app as setup_app_mod

    calls = {"n": 0}

    def _fake_run(**_k: Any) -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr(tui_pkg, "is_tui_enabled", lambda: True)
    monkeypatch.setattr(startup, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(setup_app_mod, "run_setup_tui", _fake_run)

    assert startup._try_setup_tui() is True
    assert calls["n"] == 1  # run_setup_tui was actually reached


def test_wiring_skips_setup_tui_when_non_interactive(monkeypatch):
    from alysis_code.cli_impl import tui as tui_pkg
    from alysis_code.cli_impl.commands import startup
    from alysis_code.cli_impl.tui import setup_app as setup_app_mod

    def _should_not_run(**_k: Any) -> bool:
        raise AssertionError("run_setup_tui must not run on a non-interactive terminal")

    monkeypatch.setattr(tui_pkg, "is_tui_enabled", lambda: True)
    monkeypatch.setattr(startup, "_is_non_interactive_terminal", lambda: True)
    monkeypatch.setattr(setup_app_mod, "run_setup_tui", _should_not_run)

    assert startup._try_setup_tui() is None  # falls back to the classic wizard


def test_wiring_setup_command_runs_tui_without_flag(monkeypatch):
    """`alysis setup` shows the interactive screens even when ALYSIS_TUI is off."""
    from alysis_code.cli_impl import tui as tui_pkg
    from alysis_code.cli_impl.commands import startup
    from alysis_code.cli_impl.tui import setup_app as setup_app_mod

    calls = {"n": 0}

    def _fake_run(**_k: Any) -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr(tui_pkg, "is_tui_enabled", lambda: False)  # flag OFF
    monkeypatch.setattr(startup, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(setup_app_mod, "run_setup_tui", _fake_run)

    # First-run path stays gated (flag off -> classic).
    assert startup._try_setup_tui() is None
    assert calls["n"] == 0
    # The explicit setup command opts out of the gate.
    assert startup._try_setup_tui(require_flag=False) is True
    assert calls["n"] == 1


def test_wiring_announces_fallback_reason(monkeypatch, capsys):
    from alysis_code.cli_impl import tui as tui_pkg
    from alysis_code.cli_impl.commands import startup

    monkeypatch.setattr(tui_pkg, "is_tui_enabled", lambda: True)
    monkeypatch.setattr(startup, "_is_non_interactive_terminal", lambda: True)

    assert startup._try_setup_tui(require_flag=False, announce_fallback=True) is None
    out = capsys.readouterr().out
    assert "classic setup wizard" in out and "not interactive" in out
