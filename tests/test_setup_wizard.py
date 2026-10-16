from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from click import Abort
from rich.console import Console
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl import config_menu as config_menu_mod
from alysis_code.cli_impl import setup_wizard as setup_wizard_mod
from alysis_code.config import (
    AppConfig,
    config_path,
    credentials_path,
    load_config,
    load_persisted_api_key,
    load_persisted_profile_keys,
    save_config,
    save_persisted_api_key,
    save_persisted_profile_key,
)
from alysis_code.profile_presets import ProfilePreset, make_profile_from_preset
from alysis_code.profiles import ProfileSpec
from alysis_code.provider_auth import ProviderAccountStatus, ProviderModel
from alysis_code.sandbox_doctor import (
    BubblewrapInstallPlan,
    BubblewrapInstallResult,
    SandboxCheck,
    SandboxDiagnostic,
    SandboxImagePullResult,
    SandboxPullResult,
)

_ORIGINAL_VALIDATE_API_KEY = setup_wizard_mod._validate_api_key


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


class PromptScript:
    def __init__(self, answers: list[str | BaseException]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, text: str, *args: Any, **kwargs: Any) -> str:
        del args
        self.calls.append((text, kwargs))
        if not self.answers:
            raise AssertionError(f"Unexpected prompt: {text}")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class PickerScript:
    def __init__(self, answers: list[str | None]) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []
        self.connection_calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str | None:
        if kwargs.get("title") == "Connection Method":
            self.connection_calls.append(kwargs)
            if self.answers and str(self.answers[0] or "").startswith(
                setup_wizard_mod._RUNTIME_EXECUTION_PREFIX
            ):
                return setup_wizard_mod._SUBSCRIPTION_EXECUTION_VALUE
            return setup_wizard_mod._NATIVE_EXECUTION_VALUE
        if kwargs.get("title") == "AI Subscription":
            self.connection_calls.append(kwargs)
            if not self.answers:
                raise AssertionError(f"Unexpected picker: {kwargs}")
            return self.answers.pop(0)
        self.calls.append(kwargs)
        if not self.answers:
            raise AssertionError(f"Unexpected picker: {kwargs}")
        return self.answers.pop(0)


def _patch_console(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    monkeypatch.setattr(setup_wizard_mod, "_resolve_console", lambda: console)
    return output


def _config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)


def _sandbox_diagnostic(
    *,
    ready: bool,
    can_pull: bool = False,
    next_steps: tuple[str, ...] = ("Sandbox is ready.",),
) -> SandboxDiagnostic:
    return SandboxDiagnostic(
        ready=ready,
        status="ready" if ready else "not_ready",
        configured_mode="strict",
        configured_backend="auto",
        selected_backend="docker",
        docker_image="ghcr.io/alysisai/alysis-sandbox:dev",
        server_image="ghcr.io/alysisai/alysis-sandbox:server",
        checks=(
            SandboxCheck("Docker CLI", "ok", "/usr/bin/docker"),
            SandboxCheck("Docker daemon", "ok", "running"),
            SandboxCheck(
                "sandbox image",
                "ok" if ready else "missing",
                "ghcr.io/alysisai/alysis-sandbox:dev",
            ),
        ),
        next_steps=next_steps,
        can_pull=can_pull,
    )


@pytest.fixture(autouse=True)
def _setup_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        setup_wizard_mod,
        "diagnose_sandbox",
        lambda _cfg, *, include_smoke=False: _sandbox_diagnostic(ready=True),
    )
    monkeypatch.setattr(
        setup_wizard_mod,
        "pull_sandbox_images",
        Mock(side_effect=AssertionError("pull should not be called")),
    )
    monkeypatch.setattr(
        setup_wizard_mod,
        "_validate_api_key",
        lambda **_kwargs: setup_wizard_mod._ApiKeyValidationResult(status="validated"),
    )


def _run_basic_wizard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    provider: str = "openai",
    model: str = "gpt-5",
    api_key: str = "sk-test-1234",
    workspace: Path | None = None,
) -> tuple[bool, str, PickerScript, PromptScript]:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    selected_workspace = workspace or tmp_path
    picker = PickerScript([provider, model])
    prompt = PromptScript(["", api_key, os.fspath(selected_workspace)])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod.typer, "prompt", prompt)
    return setup_wizard_mod.run_setup_wizard(), output.getvalue(), picker, prompt


def test_setup_wizard_full_flow_persists_everything(monkeypatch, tmp_path: Path) -> None:
    result, output, picker, _prompt = _run_basic_wizard(monkeypatch, tmp_path)

    assert result is True
    cfg = load_config()
    assert load_persisted_api_key() is None
    assert load_persisted_profile_keys()["openai"] == "sk-test-1234"
    assert cfg.model == "gpt-5"
    assert cfg.extra_fields["active_profile"] == "openai"
    assert cfg.extra_fields["default_workspace_path"] == os.fspath(tmp_path.resolve())
    assert cfg.extra_fields["onboarded"] is True
    assert "router" not in cfg.extra_fields.get("role_models", {})
    assert cfg.execution.backend == "native"
    assert cfg.execution.runtime is None
    assert [row[1] for row in picker.connection_calls[0]["rows"]] == [
        "Use an API key",
        "Use an AI subscription",
    ]
    assert "OpenAI Codex" not in str(picker.connection_calls[0]["rows"])
    assert "Connection method selected: Use an API key" in output
    assert "Execution Backend" not in output
    assert "Alysis Code native" not in output


def test_subscription_picker_back_returns_to_connection_methods(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    selections = iter(
        [
            setup_wizard_mod._SUBSCRIPTION_EXECUTION_VALUE,
            None,
            setup_wizard_mod._NATIVE_EXECUTION_VALUE,
        ]
    )
    calls: list[dict[str, Any]] = []

    def picker(**kwargs: Any) -> str | None:
        calls.append(kwargs)
        return next(selections)

    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)

    result = setup_wizard_mod._prompt_execution(console)

    assert result.backend == "native"
    assert result.runtime is None
    assert [call["title"] for call in calls] == [
        "Connection Method",
        "AI Subscription",
        "Connection Method",
    ]
    assert "Connection method selected: Use an API key" in output.getvalue()


def test_setup_wizard_subscription_uses_native_profile_and_preserves_api_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    native_profile = ProfileSpec(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="native-model",
    )
    cfg = AppConfig(model="native-model")
    cfg.extra_fields = {
        "profiles": {"openai": native_profile.to_dict()},
        "active_profile": "openai",
    }
    save_config(cfg)
    save_persisted_profile_key("openai", "sk-native-stays")

    output = _patch_console(monkeypatch)
    picker = PickerScript(["runtime:openai-codex"])
    prompt = PromptScript(["", os.fspath(tmp_path), "n"])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod.typer, "prompt", prompt)
    _patch_subscription_adapter(
        monkeypatch,
        FakeSubscriptionAdapter(connected=False, detail="Not logged in."),
    )

    assert setup_wizard_mod.run_setup_wizard() is True

    saved = load_config()
    assert saved.execution.backend == "native"
    assert saved.execution.runtime is None
    assert saved.agent_runtimes == {}
    assert saved.model == ""
    assert saved.extra_fields["active_profile"] == "chatgpt-codex"
    assert saved.extra_fields["profiles"]["chatgpt-codex"]["auth_provider"] == "openai-codex"
    assert "openai" in saved.extra_fields["profiles"]
    assert load_persisted_profile_keys()["openai"] == "sk-native-stays"
    assert saved.extra_fields["default_workspace_path"] == os.fspath(tmp_path.resolve())
    assert saved.extra_fields["onboarded"] is False
    assert saved.extra_fields["subscription_reconnect_required"] is True
    assert saved.extra_fields["subscription_model_selection_required"] == "openai-codex"
    rendered = output.getvalue()
    assert [call["title"] for call in picker.connection_calls] == [
        "Connection Method",
        "AI Subscription",
    ]
    assert "OpenAI Codex" not in str(picker.connection_calls[0]["rows"])
    assert "ChatGPT subscription" in str(picker.connection_calls[1]["rows"])
    assert "Connection method selected: Use an AI subscription" in rendered
    assert "Connection:" in rendered
    assert "AI subscription" in rendered
    assert "Subscription:" in rendered
    assert "ChatGPT subscription" in rendered
    assert "immediate external side effect" in rendered
    assert "alysis auth login openai-codex" in rendered
    assert "not connected" in rendered
    assert "Execution Backend" not in rendered
    assert "Alysis Code native" not in rendered
    assert "(delegated)" not in rendered


def test_subscription_setup_rerun_preserves_config_selected_model_and_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeSubscriptionAdapter(connected=True)
    _patch_subscription_adapter(monkeypatch, adapter)
    cfg = AppConfig(model="kept-model", llm_reasoning_effort="high")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
        auth_provider="openai-codex",
        default_model="kept-model",
        reasoning_effort="high",
        reasoning_trace_adapter="openai_responses_summary",
    )
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }

    rebuilt = setup_wizard_mod._direct_subscription_profile(cfg, "openai-codex")

    assert rebuilt.default_model == "kept-model"
    assert rebuilt.reasoning_effort == "high"
    assert rebuilt.reasoning_trace_adapter == "openai_responses_summary"


def test_subscription_setup_catalog_refresh_does_not_clear_pending_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    adapter = FakeSubscriptionAdapter(connected=True)
    _patch_subscription_adapter(monkeypatch, adapter)
    cfg = AppConfig(model="gpt-test", llm_reasoning_effort="high")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url=adapter.base_url,
        auth_provider="openai-codex",
        default_model="gpt-test",
        reasoning_effort="high",
    )
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
        "subscription_model_selection_required": "openai-codex",
    }

    setup_wizard_mod._sync_direct_subscription_model(
        cfg,
        "openai-codex",
        adapter=adapter,
    )

    assert cfg.extra_fields["subscription_model_selection_required"] == "openai-codex"
    assert cfg.extra_fields["onboarded"] is False


def test_setup_wizard_subscription_reuses_existing_provider_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    prompt = PromptScript(["", os.fspath(tmp_path)])
    monkeypatch.setattr(
        setup_wizard_mod,
        "_run_wizard_picker",
        PickerScript(["runtime:openai-codex"]),
    )
    monkeypatch.setattr(setup_wizard_mod.typer, "prompt", prompt)
    adapter = FakeSubscriptionAdapter(
        connected=True,
        detail="Logged in using ChatGPT",
    )
    _patch_subscription_adapter(monkeypatch, adapter)

    assert setup_wizard_mod.run_setup_wizard() is True

    assert adapter.login_calls == []
    assert len(prompt.calls) == 2
    rendered = output.getvalue()
    assert "Authentication:" in rendered
    assert "connected (Logged in using ChatGPT)" in rendered


def test_setup_wizard_subscription_can_connect_with_browser_login(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    prompt = PromptScript(["", os.fspath(tmp_path), ""])
    monkeypatch.setattr(
        setup_wizard_mod,
        "_run_wizard_picker",
        PickerScript(["runtime:openai-codex"]),
    )
    monkeypatch.setattr(setup_wizard_mod.typer, "prompt", prompt)
    adapter = FakeSubscriptionAdapter(
        connected=False,
        account_label="developer@example.test",
        detail="Not logged in.",
    )
    _patch_subscription_adapter(monkeypatch, adapter)

    assert setup_wizard_mod.run_setup_wizard() is True

    assert adapter.login_calls == ["browser"]
    rendered = output.getvalue()
    assert any(text == "Connect now? [Y/n]" for text, _kwargs in prompt.calls)
    assert "immediate external side effect" in rendered
    assert "connected as developer@example.test" in rendered


def test_setup_wizard_subscription_login_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    monkeypatch.setattr(
        setup_wizard_mod,
        "_run_wizard_picker",
        PickerScript(["runtime:openai-codex"]),
    )
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", os.fspath(tmp_path), ""]),
    )
    _patch_subscription_adapter(
        monkeypatch,
        FakeSubscriptionAdapter(
            connected=False,
            login_error=RuntimeError("browser login closed"),
        ),
    )

    assert setup_wizard_mod.run_setup_wizard() is True

    rendered = output.getvalue()
    assert "not connected (browser login closed)" in rendered
    assert "alysis auth login openai-codex" in rendered
    assert load_config().execution.backend == "native"
    assert load_config().execution.runtime is None


def test_subscription_login_interrupt_reports_that_settings_are_already_saved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    monkeypatch.setattr(
        setup_wizard_mod,
        "_run_wizard_picker",
        PickerScript(["runtime:openai-codex"]),
    )
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", os.fspath(tmp_path), KeyboardInterrupt()]),
    )
    _patch_subscription_adapter(
        monkeypatch,
        FakeSubscriptionAdapter(connected=False),
    )

    assert setup_wizard_mod.run_setup_wizard() is True

    rendered = output.getvalue()
    assert "model access settings are already saved" in rendered
    assert "provider sign-in was skipped" in rendered
    assert "alysis auth login openai-codex" in rendered
    assert load_config().execution.backend == "native"
    assert load_config().execution.runtime is None


def test_setup_wizard_has_no_router_step_and_profile_switch_drops_legacy_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # The router-model step is gone: setup never prompts for or writes a
    # router model. The legacy role_models.router key is still removed, but by
    # the profile-switch hygiene in profiles.set_active_profile (the migrated
    # "default" profile is swapped for the chosen provider during commit),
    # while other unexposed role keys survive.
    _config_env(tmp_path, monkeypatch)
    cfg = AppConfig()
    cfg.extra_fields = {"role_models": {"coding": "coding-model", "router": "old-router-model"}}
    save_config(cfg)

    result, output, picker, _prompt = _run_basic_wizard(monkeypatch, tmp_path)

    assert result is True
    saved_cfg = load_config()
    assert saved_cfg.extra_fields["role_models"] == {"coding": "coding-model"}
    assert "Router model:" not in output
    assert all(call.get("title") != "Router Model" for call in picker.calls)


def test_setup_wizard_no_backend_disable_writes_off_to_both_gates(
    monkeypatch, tmp_path: Path
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    monkeypatch.setattr(
        setup_wizard_mod,
        "diagnose_sandbox",
        lambda _cfg, *, include_smoke=False: _sandbox_diagnostic(ready=False, can_pull=False),
    )
    monkeypatch.setattr(setup_wizard_mod, "detect_bubblewrap_install_plan", lambda: None)
    picker = PickerScript(["openai", "gpt-5", "disable"])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer, "prompt", PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    cfg = load_config()
    assert cfg.extra_fields["shell_sandbox"]["mode"] == "off"
    assert cfg.extra_fields["verify_sandbox"]["mode"] == "off"
    assert "Sandbox disabled" in output.getvalue()


def test_setup_wizard_no_backend_install_bwrap_then_ready(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    diagnose_calls = {"count": 0}

    def fake_diagnose(_cfg, *, include_smoke=False):
        diagnose_calls["count"] += 1
        if diagnose_calls["count"] == 1:
            return _sandbox_diagnostic(ready=False, can_pull=False)
        return _sandbox_diagnostic(ready=True)

    plan = BubblewrapInstallPlan(
        manager="apt-get",
        command=("sudo", "apt-get", "install", "-y", "bubblewrap"),
        display="sudo apt-get install -y bubblewrap",
    )
    install_calls: list[BubblewrapInstallPlan | None] = []

    def fake_install(*, plan=None):
        install_calls.append(plan)
        return BubblewrapInstallResult(ok=True, command=plan.display, detail="ok")

    monkeypatch.setattr(setup_wizard_mod, "diagnose_sandbox", fake_diagnose)
    monkeypatch.setattr(setup_wizard_mod, "detect_bubblewrap_install_plan", lambda: plan)
    monkeypatch.setattr(setup_wizard_mod, "install_bubblewrap", fake_install)
    picker = PickerScript(["openai", "gpt-5", "install_bwrap"])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer, "prompt", PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    assert install_calls == [plan]
    assert diagnose_calls["count"] >= 2
    cfg = load_config()
    # Bubblewrap installed and re-check passed: the secure default is untouched.
    assert (
        "shell_sandbox" not in cfg.extra_fields
        or cfg.extra_fields.get("shell_sandbox", {}).get("mode") != "off"
    )


def test_setup_wizard_abort_on_api_key_writes_nothing(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai"]))
    monkeypatch.setattr(setup_wizard_mod.typer, "prompt", PromptScript(["", Abort(), "y"]))

    assert setup_wizard_mod.run_setup_wizard() is False
    assert "Setup cancelled. No changes saved." in output.getvalue()
    assert not config_path().exists()
    assert not credentials_path().exists()


def test_setup_wizard_abort_on_workspace_writes_nothing(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai", "gpt-5"]))
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "sk-test-1234", Abort(), "y"]),
    )

    assert setup_wizard_mod.run_setup_wizard() is False
    assert "Setup cancelled. No changes saved." in output.getvalue()
    assert not config_path().exists()
    assert not credentials_path().exists()


def test_setup_wizard_success_uses_one_config_save_and_one_profile_key_save(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai", "gpt-5"]))
    monkeypatch.setattr(
        setup_wizard_mod.typer, "prompt", PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    )
    save_calls: list[AppConfig] = []
    key_calls: list[tuple[str, str]] = []

    def fake_save_config(cfg: AppConfig) -> None:
        save_calls.append(cfg)

    def fake_save_profile_key(profile_name: str, api_key: str) -> None:
        key_calls.append((profile_name, api_key))

    monkeypatch.setattr(setup_wizard_mod, "save_config", fake_save_config)
    monkeypatch.setattr(setup_wizard_mod, "save_persisted_profile_key", fake_save_profile_key)

    assert setup_wizard_mod.run_setup_wizard() is True
    assert len(save_calls) == 1
    assert key_calls == [("openai", "sk-test-1234")]
    assert save_calls[0].model == "gpt-5"
    assert save_calls[0].extra_fields["default_workspace_path"] == os.fspath(tmp_path.resolve())


def test_setup_wizard_rollback_restores_config_when_key_save_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    old_cfg = AppConfig(model="existing")
    save_config(old_cfg)
    before = config_path().read_bytes()
    _patch_console(monkeypatch)
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai", "gpt-5"]))
    monkeypatch.setattr(
        setup_wizard_mod.typer, "prompt", PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    )

    def fail_key_save(_profile_name: str, _api_key: str) -> None:
        raise PermissionError("credential store locked")

    monkeypatch.setattr(setup_wizard_mod, "save_persisted_profile_key", fail_key_save)

    assert setup_wizard_mod.run_setup_wizard() is False
    assert config_path().read_bytes() == before
    assert load_config().model == "existing"
    assert not credentials_path().exists()


def test_setup_wizard_creates_anthropic_profile_with_chosen_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    result, _output, _picker, _prompt = _run_basic_wizard(
        monkeypatch,
        tmp_path,
        provider="anthropic",
        model="claude-sonnet-5",
        api_key="sk-ant-test",
    )

    assert result is True
    cfg = load_config()
    profile = cfg.extra_fields["profiles"]["anthropic"]
    assert cfg.extra_fields["active_profile"] == "anthropic"
    assert cfg.model == "claude-sonnet-5"
    assert profile["protocol"] == "anthropic_messages"
    assert profile["base_url"] == "https://api.anthropic.com/v1"
    assert profile["default_model"] == "claude-sonnet-5"
    assert profile["web_search_adapter"] == "anthropic_messages"
    assert profile["extra_headers"] == {}
    assert load_persisted_profile_keys()["anthropic"] == "sk-ant-test"


def test_provider_fallback_does_not_treat_api_key_like_input_as_openai(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    rows = [
        (preset.key, preset.label, setup_wizard_mod._preset_description(preset))
        for preset in setup_wizard_mod._setup_presets()
    ]
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["sk-this-is-a-key-not-a-provider", "2"]),
    )

    selected = setup_wizard_mod._run_wizard_picker_fallback(
        console=setup_wizard_mod._resolve_console(),
        title="Provider Profile",
        subtitle="Pick a provider.",
        rows=rows,
        current_value=rows[0][0],
        footer_hint="fallback",
        invalid_hint="Pick a provider first; you'll enter the key in the next step.",
    )

    assert selected == rows[1][0]
    assert selected != "openai"
    assert "Pick a provider first; you'll enter the key in the next step." in output.getvalue()


def test_setup_profile_picker_separates_compatibility_and_native_presets() -> None:
    presets = setup_wizard_mod._setup_presets()
    keys = [preset.key for preset in presets]
    advanced = setup_wizard_mod._advanced_setup_presets()
    advanced_keys = [preset.key for preset in advanced]

    # Native first-party providers lead the primary picker; every other hosted
    # provider follows in registration order. The account-gated hosted Alysis Code
    # preset stays behind the advanced picker while no campaign is active.
    assert keys[:3] == ["openai-responses", "anthropic", "gemini"]
    assert "alysis" not in keys
    assert "xiaomi-mimo" in keys
    assert "deepseek" in keys
    assert "openrouter" in keys
    assert all(preset.protocol != "gemini_interactions" for preset in presets)
    assert all(preset.protocol != "gemini_interactions" for preset in advanced)
    # Only compatibility duplicates, local endpoints, custom URLs, legacy
    # aliases, and the account-gated hosted preset are held back for the
    # advanced picker.
    assert "alysis" in advanced_keys
    assert "deepseek" not in advanced_keys
    assert "anthropic-compat" not in keys
    assert "gemini-compat" not in keys
    assert "ollama" not in keys
    assert "custom" not in keys
    assert "anthropic-compat" in advanced_keys
    assert "gemini-compat" in advanced_keys
    assert "ollama" in advanced_keys
    assert "custom" in advanced_keys
    assert all(
        "Compatibility protocol" in setup_wizard_mod._preset_description(preset)
        for preset in advanced
        if preset.key in {"anthropic-compat", "gemini-compat"}
    )
    assert all(
        "Native first-party protocol" in setup_wizard_mod._preset_description(preset)
        for preset in presets
        if preset.key in {"openai-responses", "anthropic", "gemini"}
    )


def test_setup_wizard_advanced_flow_can_choose_anthropic_compat(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(
        [
            setup_wizard_mod._ADVANCED_PROVIDER_PRESETS_VALUE,
            "anthropic-compat",
            "claude-sonnet-4-6",
        ]
    )
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "sk-ant-test", os.fspath(tmp_path)]),
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    profile = load_config().extra_fields["profiles"]["anthropic-compat"]
    assert profile["protocol"] == "openai_compat"
    assert profile["base_url"] == "https://api.anthropic.com/v1/"


def test_setup_wizard_surfaces_provider_diagnostic_warnings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    cfg = AppConfig(web_search_mode="external")
    save_config(cfg)
    output = _patch_console(monkeypatch)
    picker = PickerScript(["openai-responses", "gpt-5.5"])
    prompt = PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod.typer, "prompt", prompt)

    assert setup_wizard_mod.run_setup_wizard() is True

    rendered = output.getvalue()
    assert "Provider diagnostic:" in rendered
    assert "web_search_mode=external is incompatible" in rendered
    assert "web_search_adapter=openai_responses" in rendered


def test_model_picker_rows_include_preset_suggestions(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(["openai", "gpt-5.6-terra"])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer, "prompt", PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    model_rows = picker.calls[1]["rows"]
    assert model_rows[0] == (
        "gpt-6-astra",
        "gpt-6-astra",
        "default - GPT-6 flagship, reasoning always on, 1.05M",
    )
    assert (
        "gpt-5.6-terra",
        "gpt-5.6-terra",
        "advanced - balanced 5.6 tier at 1/5 the price, 1.05M",
    ) in model_rows
    assert model_rows[-1][1] == "Type a custom model name"


def test_deepseek_model_picker_uses_v4_models(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(["deepseek", "deepseek-flash"])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer, "prompt", PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    model_values = [value for value, _label, _description in picker.calls[1]["rows"]]
    assert "deepseek-v4-pro" in model_values
    assert "deepseek-flash" in model_values
    assert "deepseek-coder" not in model_values


def test_nvidia_setup_merges_live_third_party_models_and_persists_reasoning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(["nvidia", "deepseek-ai/deepseek-v4-pro", "max"])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "nvapi-test", os.fspath(tmp_path)]),
    )
    monkeypatch.setattr(
        setup_wizard_mod,
        "_discover_setup_provider_models",
        lambda *_args, **_kwargs: (
            (
                setup_wizard_mod.ProviderModelOption(
                    id="deepseek-ai/deepseek-v4-pro",
                    label="deepseek-ai/deepseek-v4-pro",
                ),
                setup_wizard_mod.ProviderModelOption(
                    id="meta/llama-3.3-70b-instruct",
                    label="meta/llama-3.3-70b-instruct",
                ),
            ),
            "",
        ),
    )

    assert setup_wizard_mod.run_setup_wizard() is True

    model_values = [value for value, _label, _description in picker.calls[1]["rows"]]
    assert "deepseek-ai/deepseek-v4-pro" in model_values
    assert "meta/llama-3.3-70b-instruct" in model_values
    live_meta_row = next(
        row for row in picker.calls[1]["rows"] if row[0] == "meta/llama-3.3-70b-instruct"
    )
    assert live_meta_row[1].endswith("(chat compatibility unverified)")
    assert "validated before setup completes" in live_meta_row[2]
    assert picker.calls[2]["title"] == "Reasoning Effort"
    assert [value for value, _label, _description in picker.calls[2]["rows"]] == [
        "off",
        "high",
        "max",
        "auto",
    ]
    cfg = load_config()
    assert cfg.model == "deepseek-ai/deepseek-v4-pro"
    assert cfg.llm_reasoning_effort == "max"
    assert cfg.llm_enable_thinking is True
    assert cfg.extra_fields["llm_thinking_label"] == "max"


def test_unknown_nvidia_model_uses_auto_without_exposing_guessed_efforts() -> None:
    preset = setup_wizard_mod._preset_by_key("nvidia")
    assert preset is not None
    profile_result = setup_wizard_mod._ProfileStepResult(
        profile=make_profile_from_preset(preset),
        label=preset.label,
        preset=preset,
    )

    assert setup_wizard_mod._setup_reasoning_labels(
        profile_result,
        model="new-vendor/model-released-today",
    ) == ("auto",)


def test_zai_coding_plan_setup_persists_only_documented_reasoning_levels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(["zai-coding-plan", "glm-5.3", "high"])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "zai-test", os.fspath(tmp_path)]),
    )

    assert setup_wizard_mod.run_setup_wizard() is True

    assert picker.calls[2]["title"] == "Reasoning Effort"
    assert [value for value, _label, _description in picker.calls[2]["rows"]] == [
        "low",
        "high",
        "max",
        "auto",
    ]
    cfg = load_config()
    assert cfg.model == "glm-5.3"
    assert cfg.llm_reasoning_effort == "high"
    assert cfg.llm_enable_thinking is True
    assert cfg.extra_fields["llm_thinking_label"] == "high"


def test_gemini_model_picker_uses_stable_models(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(["gemini", "gemini-3.8-flash"])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer, "prompt", PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    model_values = [value for value, _label, _description in picker.calls[1]["rows"]]
    assert model_values[0] == "gemini-3.8-flash"
    assert "gemini-3.7-flash" in model_values
    assert "gemini-3.5-flash-lite" in model_values
    assert "gemini-3.1-pro-preview" in model_values
    assert "gemini-2.5-pro" not in model_values
    assert "gemini-2.5-flash" not in model_values
    assert "gemini-3.1-preview" not in model_values
    assert "gemini-3-pro-preview" not in model_values
    assert "gemini-3.1-flash-lite-preview" not in model_values


def test_gemini_custom_stale_preview_alias_canonicalizes_before_validation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(["gemini", setup_wizard_mod._CUSTOM_MODEL_VALUE])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "sk-test-1234", "gemini-3.1-preview", os.fspath(tmp_path)]),
    )
    validated_models: list[str | None] = []

    def validate(*, profile, api_key, model=None, **_kwargs):
        del profile, api_key
        validated_models.append(model)
        return setup_wizard_mod._ApiKeyValidationResult(status="validated")

    monkeypatch.setattr(setup_wizard_mod, "_validate_api_key", validate)

    assert setup_wizard_mod.run_setup_wizard() is True
    assert validated_models == [None, "gemini-3.1-pro-preview"]
    assert load_config().model == "gemini-3.1-pro-preview"


def test_model_picker_without_suggestions_only_offers_custom() -> None:
    preset = ProfilePreset(
        key="empty",
        label="Empty",
        protocol="openai_compat",
        base_url="http://localhost:9999/v1",
        api_key_env=None,
        suggested_models=(),
    )
    profile = make_profile_from_preset(preset)
    rows = setup_wizard_mod._model_picker_rows(
        setup_wizard_mod._ProfileStepResult(profile=profile, label=preset.label, preset=preset)
    )

    assert rows == [
        (
            setup_wizard_mod._CUSTOM_MODEL_VALUE,
            "Type a custom model name",
            "Use any model supported by this provider",
        )
    ]
    assert setup_wizard_mod._FALLBACK_VALIDATION_MODEL not in {row[0] for row in rows}


def _validate_with_transport_response(
    response: httpx.Response,
    *,
    base_url: str = "https://api.example.test/v1",
) -> tuple[setup_wizard_mod._ApiKeyValidationResult, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return response

    result = _ORIGINAL_VALIDATE_API_KEY(
        profile=ProfileSpec(
            name="custom",
            base_url=base_url,
            extra_headers={"x-provider": "example"},
            default_model="example-model",
        ),
        api_key="sk-test",
        model="example-model",
        transport=httpx.MockTransport(handler),
    )
    return result, captured


def test_api_key_validation_posts_chat_completion_ping() -> None:
    result, captured = _validate_with_transport_response(
        httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})
    )

    assert result.status == "validated"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.example.test/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer sk-test"
    assert captured["headers"]["x-provider"] == "example"
    assert captured["body"] == {
        "model": "example-model",
        "messages": [{"role": "user", "content": "ping"}],
        "temperature": 0.0,
    }


def test_api_key_validation_normalizes_trailing_slash_base_url() -> None:
    result, captured = _validate_with_transport_response(
        httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]}),
        base_url="https://api.example.test/v1/",
    )

    assert result.status == "validated"
    assert captured["url"] == "https://api.example.test/v1/chat/completions"


def test_api_key_validation_401_is_failed() -> None:
    result, _captured = _validate_with_transport_response(httpx.Response(401, text="bad key"))

    assert result.status == "failed"
    assert result.message == "Provider rejected the API key (HTTP 401)."


def test_api_key_validation_400_model_not_found_is_distinct() -> None:
    result, _captured = _validate_with_transport_response(
        httpx.Response(400, json={"error": {"message": "model not_found"}})
    )

    assert result.status == "model_not_found"
    assert "Model 'example-model' not found" in result.message


def test_api_key_validation_400_max_tokens_unsupported_is_not_model_not_found() -> None:
    result, _captured = _validate_with_transport_response(
        httpx.Response(
            400,
            json={
                "error": {
                    "message": (
                        "Unsupported parameter: 'max_tokens' is not supported with this model. "
                        "Use 'max_completion_tokens' instead."
                    ),
                    "type": "invalid_request_error",
                    "param": "max_tokens",
                    "code": "unsupported_parameter",
                }
            },
        )
    )

    assert result.status == "inconclusive"
    assert "max_tokens" in result.message
    assert "not found" not in result.message


def test_api_key_validation_retries_when_gemini_rejects_temperature() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append(body)
        if "temperature" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "temperature cannot be set for this model.",
                        "type": "invalid_request_error",
                        "param": "temperature",
                        "code": "unsupported_parameter",
                    }
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})

    result = _ORIGINAL_VALIDATE_API_KEY(
        profile=ProfileSpec(
            name="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            default_model="gemini-2.5-flash",
        ),
        api_key="sk-test",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "validated"
    assert len(captured) == 3
    assert captured[0]["temperature"] == 0.0
    assert captured[1]["temperature"] == 1.0
    assert "temperature" not in captured[2]


def test_gemini_api_key_validation_uses_fast_validation_model_before_model_choice() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})

    result = _ORIGINAL_VALIDATE_API_KEY(
        profile=ProfileSpec(
            name="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            default_model="gemini-2.5-flash",
        ),
        api_key="sk-test",
        suggested_models=(
            "gemini-3.7-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
        ),
        validation_model="gemini-2.5-flash",
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "validated"
    assert result.message == (
        "Validated API key using 'gemini-2.5-flash'. The selected model will be verified next."
    )
    assert "gemini-2.5-flash" in result.message
    assert "fallback" not in result.message.casefold()
    assert captured["body"]["model"] == "gemini-2.5-flash"
    assert captured["body"]["reasoning_effort"] == "low"


def test_gemini_selected_model_validation_uses_low_reasoning_effort() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})

    result = _ORIGINAL_VALIDATE_API_KEY(
        profile=ProfileSpec(
            name="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            default_model="gemini-2.5-flash",
        ),
        api_key="sk-test",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "validated"
    assert result.message == ""
    assert captured["body"]["model"] == "gemini-2.5-flash"
    assert captured["body"]["reasoning_effort"] == "low"


def test_zai_coding_plan_validation_uses_low_reasoning_effort() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})

    result = _ORIGINAL_VALIDATE_API_KEY(
        profile=ProfileSpec(
            name="zai-coding-plan",
            base_url="https://api.z.ai/api/coding/paas/v4",
            default_model="glm-5.3",
        ),
        api_key="zai-test",
        model="glm-5.3",
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "validated"
    assert captured["body"]["model"] == "glm-5.3"
    assert captured["body"]["reasoning_effort"] == "low"


def test_gemini_validation_uses_extended_timeout() -> None:
    gemini_profile = ProfileSpec(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    custom_profile = ProfileSpec(name="custom", base_url="https://api.example.test/v1")

    assert (
        setup_wizard_mod._validation_timeout_s(gemini_profile)
        > setup_wizard_mod._VALIDATION_TIMEOUT_S
    )
    assert (
        setup_wizard_mod._validation_timeout_s(custom_profile)
        == setup_wizard_mod._VALIDATION_TIMEOUT_S
    )


def test_api_key_validation_404_is_endpoint_inconclusive_not_failed() -> None:
    result, _captured = _validate_with_transport_response(httpx.Response(404, text="not found"))

    assert result.status == "inconclusive"
    assert "Provider endpoint not found at https://api.example.test/v1" in result.message


def test_api_key_validation_429_is_rate_limited_inconclusive() -> None:
    result, _captured = _validate_with_transport_response(httpx.Response(429, text="slow down"))

    assert result.status == "inconclusive"
    assert "rate-limited" in result.message


def test_api_key_validation_timeout_is_inconclusive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    result = _ORIGINAL_VALIDATE_API_KEY(
        profile=ProfileSpec(
            name="custom",
            base_url="https://api.example.test/v1",
            default_model="example-model",
        ),
        api_key="sk-test",
        model="example-model",
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "inconclusive"
    assert "validation request timed out" in result.message


@pytest.mark.parametrize(
    ("validation_status", "validation_message", "expected"),
    [
        ("validated", "", "stored, validated"),
        (
            "inconclusive",
            "Provider endpoint not found at https://api.example.test/v1.",
            "stored, validation inconclusive",
        ),
        ("failed", "Provider rejected the API key (HTTP 401).", "stored, but provider rejected it"),
        ("skipped", "", "stored, validation skipped"),
        ("model_not_found", "Model 'bad-model' not found.", "stored, model validation failed"),
    ],
)
def test_setup_summary_renders_api_key_validation_status(
    monkeypatch: pytest.MonkeyPatch,
    validation_status: setup_wizard_mod._ValidationStatus,
    validation_message: str,
    expected: str,
) -> None:
    output = _patch_console(monkeypatch)

    setup_wizard_mod._print_setup_complete(
        console=setup_wizard_mod._resolve_console(),
        profile_result=setup_wizard_mod._ProfileStepResult(
            profile=ProfileSpec(name="custom", base_url="https://api.example.test/v1"),
            label="Custom",
            preset=None,
        ),
        api_key_result=setup_wizard_mod._ApiKeyStepResult(
            api_key="sk-test",
            validation_status=validation_status,
            validation_message=validation_message,
        ),
        model_result=setup_wizard_mod._ModelStepResult(model="example-model"),
        workspace_result=setup_wizard_mod._WorkspaceStepResult(workspace="C:\\repo"),
        sandbox_result=setup_wizard_mod._SandboxStepResult(ready=True, status="docker"),
    )

    assert expected in output.getvalue()


def test_model_picker_reprompts_on_model_not_found(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    picker = PickerScript(["openai", "gpt-5.5", "gpt-5.4-mini"])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer, "prompt", PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    )
    validated_models: list[str | None] = []

    def validate(*, profile, api_key, model=None, **_kwargs):
        del profile, api_key
        validated_models.append(model)
        if model == "gpt-5.5":
            return setup_wizard_mod._ApiKeyValidationResult(
                status="model_not_found",
                message="Model 'gpt-5.5' not found at this provider. Pick a different model in the next step.",
            )
        return setup_wizard_mod._ApiKeyValidationResult(status="validated")

    monkeypatch.setattr(setup_wizard_mod, "_validate_api_key", validate)

    assert setup_wizard_mod.run_setup_wizard() is True
    assert validated_models == [None, "gpt-5.5", "gpt-5.4-mini"]
    assert load_config().model == "gpt-5.4-mini"
    assert "Model 'gpt-5.5' not found" in output.getvalue()


def test_custom_model_not_found_can_be_used_after_explicit_warning(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    picker = PickerScript(["openai", setup_wizard_mod._CUSTOM_MODEL_VALUE])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "sk-test-1234", "provider-private-model", "y", os.fspath(tmp_path)]),
    )

    def validate(*, profile, api_key, model=None, **_kwargs):
        del profile, api_key
        if model == "provider-private-model":
            return setup_wizard_mod._ApiKeyValidationResult(
                status="model_not_found",
                message=(
                    "Model 'provider-private-model' not found at this provider. "
                    "Pick a different model in the next step."
                ),
            )
        return setup_wizard_mod._ApiKeyValidationResult(status="validated")

    monkeypatch.setattr(setup_wizard_mod, "_validate_api_key", validate)

    assert setup_wizard_mod.run_setup_wizard() is True
    cfg = load_config()
    assert cfg.model == "provider-private-model"
    rendered = output.getvalue()
    assert "Model 'provider-private-model' not found" in rendered
    assert "stored, validation inconclusive" in rendered


def test_api_key_validation_failure_reprompts_then_accepts_last_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai", "gpt-5"]))
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "bad-1", "y", "bad-2", "y", "bad-3", os.fspath(tmp_path)]),
    )
    validations: list[str] = []

    def fail_validation(*, profile, api_key, **_kwargs):
        del profile
        validations.append(api_key)
        return setup_wizard_mod._ApiKeyValidationResult(
            status="failed",
            message="Provider rejected the key (HTTP 401).",
        )

    monkeypatch.setattr(setup_wizard_mod, "_validate_api_key", fail_validation)

    assert setup_wizard_mod.run_setup_wizard() is True
    assert validations == ["bad-1", "bad-2", "bad-3"]
    assert load_persisted_profile_keys()["openai"] == "bad-3"


def test_api_key_validation_network_failure_continues(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai", "gpt-5"]))
    monkeypatch.setattr(
        setup_wizard_mod.typer, "prompt", PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    )
    monkeypatch.setattr(
        setup_wizard_mod,
        "_validate_api_key",
        lambda **_kwargs: setup_wizard_mod._ApiKeyValidationResult(
            status="inconclusive",
            message="Could not reach https://api.openai.com/v1: timed out",
        ),
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    assert "Could not reach https://api.openai.com/v1" in output.getvalue()
    assert load_persisted_profile_keys()["openai"] == "sk-test-1234"


def test_workspace_default_prefers_projects(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    projects = home / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr(setup_wizard_mod.Path, "home", classmethod(lambda cls: home))

    assert setup_wizard_mod._suggest_workspace_default() == projects


def test_workspace_default_falls_back_to_home_and_not_cwd(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    downloads = tmp_path / "Downloads"
    home.mkdir()
    downloads.mkdir()
    monkeypatch.chdir(downloads)
    monkeypatch.setattr(setup_wizard_mod.Path, "home", classmethod(lambda cls: home))

    assert setup_wizard_mod._suggest_workspace_default() == home
    assert setup_wizard_mod._suggest_workspace_default() != downloads


def test_setup_wizard_invalid_workspace_reprompts(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai", "gpt-5"]))
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "sk-test-1234", "/does/not/exist", os.fspath(tmp_path)]),
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    assert load_config().extra_fields["default_workspace_path"] == os.fspath(tmp_path.resolve())


def test_setup_wizard_skips_sandbox_pull_when_ready(monkeypatch, tmp_path: Path) -> None:
    pull = Mock(side_effect=AssertionError("pull should not be called"))
    monkeypatch.setattr(setup_wizard_mod, "pull_sandbox_images", pull)

    result, output, _picker, _prompt = _run_basic_wizard(monkeypatch, tmp_path)

    assert result is True
    pull.assert_not_called()
    assert "Sandbox ready" in output


def test_setup_wizard_offers_pull_when_image_missing(monkeypatch, tmp_path: Path) -> None:
    diagnose_calls: list[bool] = []

    def fake_diagnose(_cfg: AppConfig, *, include_smoke: bool = False) -> SandboxDiagnostic:
        diagnose_calls.append(include_smoke)
        if include_smoke:
            return _sandbox_diagnostic(ready=True)
        return _sandbox_diagnostic(
            ready=False,
            can_pull=True,
            next_steps=("Run `alysis sandbox pull`.",),
        )

    pull = Mock(return_value=SandboxPullResult(ok=True, results=()))
    monkeypatch.setattr(setup_wizard_mod, "diagnose_sandbox", fake_diagnose)
    monkeypatch.setattr(setup_wizard_mod, "pull_sandbox_images", pull)
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai", "gpt-5"]))
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "sk-test-1234", os.fspath(tmp_path), "y"]),
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    pull.assert_called_once_with(timeout_s=900)
    assert diagnose_calls == [False, True]


def test_setup_wizard_pull_failure_logs_raw_output_without_printing_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        setup_wizard_mod,
        "diagnose_sandbox",
        lambda _cfg, *, include_smoke=False: _sandbox_diagnostic(
            ready=False,
            can_pull=True,
            next_steps=("Run `alysis sandbox pull`.",),
        ),
    )
    monkeypatch.setattr(
        setup_wizard_mod,
        "pull_sandbox_images",
        Mock(
            return_value=SandboxPullResult(
                ok=False,
                error="docker pull failed",
                results=(
                    SandboxImagePullResult(
                        image="ghcr.io/alysisai/alysis-sandbox:dev",
                        ok=False,
                        output="RAW SUBPROCESS OUTPUT",
                    ),
                ),
            )
        ),
    )
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai", "gpt-5"]))
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "sk-test-1234", os.fspath(tmp_path), "y"]),
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    rendered = output.getvalue()
    assert "RAW SUBPROCESS OUTPUT" not in rendered
    assert "Raw pull output saved to:" in rendered
    logs = list((tmp_path / "data" / "logs").glob("sandbox_pull_*.log"))
    assert len(logs) == 1
    assert "RAW SUBPROCESS OUTPUT" in logs[0].read_text(encoding="utf-8")


def test_setup_wizard_unexpected_sandbox_exception_does_not_print_traceback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def boom(_cfg: AppConfig, *, include_smoke: bool = False) -> SandboxDiagnostic:
        raise RuntimeError("bang")

    monkeypatch.setattr(setup_wizard_mod, "diagnose_sandbox", boom)

    result, output, _picker, _prompt = _run_basic_wizard(monkeypatch, tmp_path)

    assert result is True
    assert "Sandbox check failed:" in output
    assert "Traceback" not in output


def test_setup_wizard_ctrl_c_after_model_back_can_cancel_without_saving(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai", None]))
    monkeypatch.setattr(
        setup_wizard_mod.typer,
        "prompt",
        PromptScript(["", "sk-test-1234", KeyboardInterrupt(), "y"]),
    )

    assert setup_wizard_mod.run_setup_wizard() is False
    assert "Setup cancelled. No changes saved." in output.getvalue()
    assert not config_path().exists()
    assert not credentials_path().exists()


def test_setup_wizard_escape_at_welcome_confirm_cancel_writes_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    monkeypatch.setattr(
        setup_wizard_mod,
        "_esc_aware_text_input",
        PromptScript([setup_wizard_mod._GoBack(), "y"]),
    )

    assert setup_wizard_mod.run_setup_wizard() is False
    assert "Setup cancelled. No changes saved." in output.getvalue()
    assert not config_path().exists()
    assert not credentials_path().exists()


def test_setup_wizard_escape_at_welcome_decline_reshows_welcome(
    monkeypatch, tmp_path: Path
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    picker = PickerScript(["openai", "gpt-5"])
    text_input = PromptScript(
        [setup_wizard_mod._GoBack(), "n", "", "sk-test-1234", os.fspath(tmp_path)]
    )
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod, "_esc_aware_text_input", text_input)

    assert setup_wizard_mod.run_setup_wizard() is True
    assert output.getvalue().count("Welcome to Alysis Code") == 2


def test_setup_wizard_escape_at_profile_returns_to_execution(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    picker = PickerScript([None, "openai", "gpt-5"])
    text_input = PromptScript(["", "", "sk-test-1234", os.fspath(tmp_path)])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod, "_esc_aware_text_input", text_input)

    assert setup_wizard_mod.run_setup_wizard() is True
    assert output.getvalue().count("Welcome to Alysis Code") == 1
    assert [call["title"] for call in picker.calls] == [
        "Provider Profile",
        "Provider Profile",
        "Default Model",
    ]


def test_setup_wizard_escape_at_api_key_returns_to_profile_with_preselection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(["openai", "openai", "gpt-5"])
    text_input = PromptScript(["", setup_wizard_mod._GoBack(), "sk-test-1234", os.fspath(tmp_path)])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod, "_esc_aware_text_input", text_input)

    assert setup_wizard_mod.run_setup_wizard() is True
    assert picker.calls[1]["title"] == "Provider Profile"
    assert picker.calls[1]["current_value"] == "openai"


def test_setup_wizard_escape_at_model_returns_to_api_key_with_previous_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(["openai", None, "gpt-5"])
    text_input = PromptScript(["", "sk-test-1234", "", os.fspath(tmp_path)])
    validations: list[str] = []

    def validate(*, profile, api_key, **_kwargs):
        del profile
        validations.append(api_key)
        return setup_wizard_mod._ApiKeyValidationResult(status="validated")

    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod, "_esc_aware_text_input", text_input)
    monkeypatch.setattr(setup_wizard_mod, "_validate_api_key", validate)

    assert setup_wizard_mod.run_setup_wizard() is True
    assert validations == ["sk-test-1234", "sk-test-1234"]
    assert any("Enter to keep current" in call[0] for call in text_input.calls)


def test_setup_wizard_escape_at_workspace_returns_to_model_with_preselection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(
        [
            "openai",
            "gpt-5.6-terra",
            "gpt-5.6-terra",
        ]
    )
    text_input = PromptScript(["", "sk-test-1234", setup_wizard_mod._GoBack(), os.fspath(tmp_path)])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod, "_esc_aware_text_input", text_input)

    assert setup_wizard_mod.run_setup_wizard() is True
    assert picker.calls[2]["title"] == "Default Model"
    assert picker.calls[2]["current_value"] == "gpt-5.6-terra"


def test_setup_wizard_profile_change_invalidates_api_key_and_model_results(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(
        [
            "openai",
            "gpt-5.6-terra",
            None,
            "anthropic",
            "claude-sonnet-5",
        ]
    )
    text_input = PromptScript(
        [
            "",
            "sk-openai",
            setup_wizard_mod._GoBack(),
            setup_wizard_mod._GoBack(),
            "sk-ant",
            os.fspath(tmp_path),
        ]
    )
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod, "_esc_aware_text_input", text_input)

    assert setup_wizard_mod.run_setup_wizard() is True
    cfg = load_config()
    assert cfg.extra_fields["active_profile"] == "anthropic"
    assert load_persisted_profile_keys()["anthropic"] == "sk-ant"
    api_prompts = [call[0] for call in text_input.calls if call[0].startswith("Paste your API key")]
    assert api_prompts[-1] == "Paste your API key"
    assert picker.calls[-1]["title"] == "Default Model"
    assert any(row[0] == "claude-sonnet-5" for row in picker.calls[-1]["rows"])


def test_setup_wizard_ctrl_c_confirmation_decline_stays_on_same_step(
    monkeypatch, tmp_path: Path
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(["openai", "gpt-5"])
    text_input = PromptScript(["", KeyboardInterrupt(), "n", "sk-test-1234", os.fspath(tmp_path)])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod, "_esc_aware_text_input", text_input)

    assert setup_wizard_mod.run_setup_wizard() is True
    api_prompts = [call[0] for call in text_input.calls if call[0].startswith("Paste your API key")]
    assert api_prompts == ["Paste your API key", "Paste your API key"]


def test_setup_wizard_ctrl_c_confirmation_accept_cancels_without_writes(
    monkeypatch, tmp_path: Path
) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    picker = PickerScript(["openai"])
    text_input = PromptScript(["", KeyboardInterrupt(), "y"])
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod, "_esc_aware_text_input", text_input)

    assert setup_wizard_mod.run_setup_wizard() is False
    assert "Setup cancelled. No changes saved." in output.getvalue()
    assert not config_path().exists()
    assert not credentials_path().exists()


def test_setup_wizard_non_tty_note_is_printed(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    monkeypatch.setattr(
        setup_wizard_mod, "_escape_capture_unavailable_reason", lambda: "non-interactive terminal"
    )
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", PickerScript(["openai", "gpt-5"]))
    monkeypatch.setattr(
        setup_wizard_mod.typer, "prompt", PromptScript(["", "sk-test-1234", os.fspath(tmp_path)])
    )

    assert setup_wizard_mod.run_setup_wizard() is True
    assert "does not support Esc/back navigation (non-interactive terminal)" in output.getvalue()


def test_setup_wizard_back_and_forth_then_cancel_writes_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    _config_env(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    picker = PickerScript(["openai", "gpt-5", None])
    text_input = PromptScript(
        [
            "",
            "sk-test-1234",
            setup_wizard_mod._GoBack(),
            KeyboardInterrupt(),
            "y",
        ]
    )
    monkeypatch.setattr(setup_wizard_mod, "_run_wizard_picker", picker)
    monkeypatch.setattr(setup_wizard_mod, "_esc_aware_text_input", text_input)

    assert setup_wizard_mod.run_setup_wizard() is False
    assert not config_path().exists()
    assert not credentials_path().exists()


def test_main_callback_triggers_wizard_on_first_run(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    calls: list[str] = []

    def fake_wizard() -> bool:
        calls.append("wizard")
        save_config(AppConfig(model="gpt-5"))
        save_persisted_api_key("sk-test-1234")
        return True

    def fake_chat() -> None:
        calls.append("chat")

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(setup_wizard_mod, "run_setup_wizard", fake_wizard)
    monkeypatch.setattr(cli_mod, "_run_default_chat_action", fake_chat)

    result = CliRunner().invoke(
        alysis_app, [], env={"ALYSIS_CONFIG_DIR": os.environ["ALYSIS_CONFIG_DIR"]}
    )

    assert result.exit_code == 0
    assert calls == ["wizard", "chat"]


def test_main_callback_skips_wizard_for_onboarded_partial_config(
    monkeypatch, tmp_path: Path
) -> None:
    # An already-onboarded user (a completed setup persisted ``default_workspace_path``)
    # whose model is unset is nudged through the config menu, not re-sent through the
    # full first-run wizard. The onboarded marker — not model/key presence — is what
    # gates the wizard.
    _config_env(tmp_path, monkeypatch)
    cfg = AppConfig(model="")
    cfg.extra_fields = {"default_workspace_path": os.fspath(tmp_path)}
    save_config(cfg)
    save_persisted_api_key("sk-test-1234")
    calls: list[tuple[str, str | None]] = []

    def fake_wizard() -> bool:
        calls.append(("wizard", None))
        return True

    def fake_menu(*, cfg: AppConfig | None = None, auto_focus: str | None = None):
        del cfg
        calls.append(("menu", auto_focus))

    def fake_chat() -> None:
        calls.append(("chat", None))

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(setup_wizard_mod, "run_setup_wizard", fake_wizard)
    monkeypatch.setattr(config_menu_mod, "run_config_menu", fake_menu)
    monkeypatch.setattr(cli_mod, "_run_default_chat_action", fake_chat)

    result = CliRunner().invoke(
        alysis_app, [], env={"ALYSIS_CONFIG_DIR": os.environ["ALYSIS_CONFIG_DIR"]}
    )

    assert result.exit_code == 0
    assert calls == [("menu", "model"), ("chat", None)]


def test_main_callback_migrates_delegated_subscription_and_opens_chat_shell(
    monkeypatch, tmp_path: Path
) -> None:
    _config_env(tmp_path, monkeypatch)
    cfg = AppConfig(
        execution={"backend": "delegated", "runtime": "openai-codex"},
        agent_runtimes={
            "openai-codex": {
                "adapter": "codex-cli",
                "executable": "codex",
                "model": "gpt-5.4",
                "reasoning_effort": "high",
            },
        },
    )
    cfg.extra_fields = {"onboarded": True, "default_workspace_path": os.fspath(tmp_path)}
    save_config(cfg)
    calls: list[str] = []

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(
        config_menu_mod,
        "run_config_menu",
        lambda **_kwargs: calls.append("menu"),
    )
    monkeypatch.setattr(cli_mod, "_run_default_chat_action", lambda: calls.append("chat"))

    result = CliRunner().invoke(
        alysis_app, [], env={"ALYSIS_CONFIG_DIR": os.environ["ALYSIS_CONFIG_DIR"]}
    )

    assert result.exit_code == 0
    assert calls == ["chat"]
    migrated = load_config()
    assert migrated.execution.backend == "native"
    assert migrated.execution.runtime is None
    assert migrated.extra_fields["active_profile"] == "chatgpt-codex"
    assert migrated.extra_fields["subscription_model_selection_required"] == "openai-codex"


def test_main_callback_runs_wizard_when_config_lacks_onboarding(
    monkeypatch, tmp_path: Path
) -> None:
    # A config that already carries a model + key but was never completed through
    # setup (no onboarded marker, no default_workspace_path) must still route a
    # first launch into the setup wizard instead of dropping the user straight into
    # chat — which is what lands them on the guarded-workspace picker.
    _config_env(tmp_path, monkeypatch)
    save_config(AppConfig(model="gpt-5"))
    save_persisted_api_key("sk-test-1234")
    calls: list[str] = []

    def fake_wizard() -> bool:
        calls.append("wizard")
        return True

    def fake_chat() -> None:
        calls.append("chat")

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(setup_wizard_mod, "run_setup_wizard", fake_wizard)
    monkeypatch.setattr(cli_mod, "_run_default_chat_action", fake_chat)

    result = CliRunner().invoke(
        alysis_app, [], env={"ALYSIS_CONFIG_DIR": os.environ["ALYSIS_CONFIG_DIR"]}
    )

    assert result.exit_code == 0
    assert calls == ["wizard", "chat"]


def test_explicit_chat_with_no_model_starts_first_run_setup(monkeypatch, tmp_path: Path) -> None:
    _config_env(tmp_path, monkeypatch)
    calls: list[str] = []

    def fake_setup() -> bool:
        calls.append("setup")
        return False

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(cli_mod, "_should_run_first_run_setup_wizard", lambda: True)
    monkeypatch.setattr(cli_mod, "_maybe_run_first_run_setup_wizard", fake_setup)

    result = CliRunner().invoke(
        alysis_app, ["chat"], env={"ALYSIS_CONFIG_DIR": os.environ["ALYSIS_CONFIG_DIR"]}
    )

    assert result.exit_code == 0
    assert calls == ["setup"]
    assert "Starting first-run setup" in result.output
    assert "Model is not set" not in result.output


def test_explicit_chat_with_no_model_noninteractive_prints_onboarding_guidance(
    monkeypatch, tmp_path: Path
) -> None:
    _config_env(tmp_path, monkeypatch)

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: True)

    result = CliRunner().invoke(
        alysis_app, ["chat"], env={"ALYSIS_CONFIG_DIR": os.environ["ALYSIS_CONFIG_DIR"]}
    )

    assert result.exit_code == 2
    assert "No model is configured yet." in result.output
    assert "alysis setup" in result.output
    assert "alysis login" in result.output
    assert "Config error" not in result.output


def test_is_onboarded_marker_and_back_compat(monkeypatch, tmp_path: Path) -> None:
    from alysis_code.cli_impl.commands import startup as startup_mod

    _config_env(tmp_path, monkeypatch)
    # No config at all → not onboarded.
    assert startup_mod._is_onboarded() is False
    # A model alone (never completed setup) → still not onboarded (the repro case).
    save_config(AppConfig(model="gpt-5"))
    assert startup_mod._is_onboarded() is False
    # Explicit marker → onboarded.
    cfg = AppConfig(model="gpt-5")
    cfg.extra_fields = {"onboarded": True}
    save_config(cfg)
    assert startup_mod._is_onboarded() is True
    # Back-compat: a completed setup recorded default_workspace_path → onboarded.
    cfg = AppConfig(model="gpt-5")
    cfg.extra_fields = {"default_workspace_path": os.fspath(tmp_path)}
    save_config(cfg)
    assert startup_mod._is_onboarded() is True


def test_setup_typer_command_runs_wizard(monkeypatch) -> None:
    called: list[bool] = []

    def fake_wizard() -> bool:
        called.append(True)
        return True

    monkeypatch.setattr(setup_wizard_mod, "run_setup_wizard", fake_wizard)
    monkeypatch.setattr(
        cli_mod,
        "load_config",
        lambda: AppConfig(
            execution={"backend": "delegated", "runtime": "openai-codex"},
            agent_runtimes={
                "openai-codex": {"adapter": "codex-cli", "executable": "codex"},
            },
        ),
    )

    result = CliRunner().invoke(alysis_app, ["setup"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert called == [True]


def test_setup_command_launches_chat_after_interactive_tui(monkeypatch) -> None:
    chat_calls: list[bool] = []
    monkeypatch.setattr(cli_mod, "_try_setup_tui", lambda **_k: True)  # TUI saved
    monkeypatch.setattr(cli_mod, "load_config", lambda: AppConfig())
    monkeypatch.setattr(cli_mod, "_provider_auth_ready_for_chat", lambda: True)
    monkeypatch.setattr(cli_mod, "_maybe_run_startup_config_menu", lambda: None)
    monkeypatch.setattr(cli_mod, "_run_chat_after_setup", lambda: chat_calls.append(True))

    result = CliRunner().invoke(alysis_app, ["setup"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert chat_calls == [True]  # flows straight into chat


def test_setup_command_delegated_tui_finishes_without_launching_native_chat(monkeypatch) -> None:
    chat_calls: list[bool] = []
    delegated = AppConfig(
        execution={"backend": "delegated", "runtime": "openai-codex"},
        agent_runtimes={
            "openai-codex": {"adapter": "codex-cli", "executable": "codex"},
        },
    )
    monkeypatch.setattr(cli_mod, "_try_setup_tui", lambda **_k: True)
    monkeypatch.setattr(cli_mod, "load_config", lambda: delegated)
    monkeypatch.setattr(cli_mod, "_run_chat_after_setup", lambda: chat_calls.append(True))

    result = CliRunner().invoke(alysis_app, ["setup"])

    assert result.exit_code == 0
    assert chat_calls == []


def test_setup_command_tui_cancel_does_not_launch_chat(monkeypatch) -> None:
    chat_calls: list[bool] = []
    monkeypatch.setattr(cli_mod, "_try_setup_tui", lambda **_k: False)  # cancelled
    monkeypatch.setattr(cli_mod, "_run_chat_after_setup", lambda: chat_calls.append(True))

    result = CliRunner().invoke(alysis_app, ["setup"])

    assert result.exit_code == 0
    assert chat_calls == []


def test_run_chat_after_setup_uses_configured_workspace(monkeypatch, tmp_path) -> None:
    """After setup, chat launches in the saved workspace with broad consent so the
    guard does not re-ask about the folder the user just chose."""
    from alysis_code.cli_impl.commands import startup as startup_mod

    ws = tmp_path / "myproject"
    ws.mkdir()
    monkeypatch.setattr(
        startup_mod,
        "_configured_default_workspace",
        lambda: ws,
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        startup_mod,
        "_run_default_chat_action",
        lambda **kwargs: captured.update(kwargs),
    )

    startup_mod._run_chat_after_setup()

    assert captured["path"] == ws
    assert captured["allow_broad_workspace"] is True


def test_run_default_chat_action_forwards_posture_flags(monkeypatch) -> None:
    """A relaunch (e.g. /config → switch project) must preserve the session's
    execution-posture flags instead of silently resetting approval policy/mode/logging."""
    from alysis_code.cli_impl.commands import startup as startup_mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli_mod, "chat", lambda **kw: captured.update(kw))

    startup_mod._run_default_chat_action(
        path=Path("/tmp/proj"),
        allow_broad_workspace=True,
        mode="auto",
        model="m1",
        yes=True,
        no_log=True,
        verify_cmd=["pytest"],
    )

    assert captured["allow_broad_workspace"] is True
    assert captured["mode"] == "auto"
    assert captured["model"] == "m1"
    assert captured["yes"] is True
    assert captured["no_log"] is True
    assert captured["verify_cmd"] == ["pytest"]


def test_run_default_chat_action_defaults_unchanged(monkeypatch) -> None:
    """Plain launches (no forwarded flags) still get the default posture."""
    from alysis_code.cli_impl.commands import startup as startup_mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli_mod, "chat", lambda **kw: captured.update(kw))

    startup_mod._run_default_chat_action()

    assert captured["yes"] is False
    assert captured["mode"] is None
    assert captured["no_log"] is False
    assert captured["verify_cmd"] is None
    assert captured["diagnostic_log"] is None


def test_run_default_run_action_passes_concrete_typer_defaults(monkeypatch) -> None:
    """Direct Python dispatch must never leak Typer OptionInfo objects into run()."""
    from alysis_code.cli_impl.commands import startup as startup_mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli_mod, "run", lambda **kw: captured.update(kw))

    startup_mod._run_default_run_action("inspect")

    assert captured["instruction"] == "inspect"
    assert captured["benchmark"] is False
    assert captured["deadline_seconds"] is None
    assert captured["require_deadline"] is False
    assert captured["diagnostic_log"] is None


def _alysis_login_preset() -> ProfilePreset:
    from alysis_code.alysis_cloud import PROFILE_KEY

    # The account preset is exposed separately from the API-key providers.
    assert all(p.key != PROFILE_KEY for p in setup_wizard_mod.provider_selection_presets())
    return next(
        p for p in setup_wizard_mod.advanced_provider_selection_presets() if p.key == PROFILE_KEY
    )


def _profile_step_for(preset: ProfilePreset) -> Any:
    profile = make_profile_from_preset(preset)
    return setup_wizard_mod._ProfileStepResult(profile=profile, label=preset.label, preset=preset)


def test_setup_offers_login_for_alysis_preset(monkeypatch) -> None:
    import alysis_code.account_login as account_login_mod

    console = Console(file=io.StringIO())
    monkeypatch.setattr(setup_wizard_mod, "_prompt_yes_no", lambda *a, **k: True)
    calls: list[bool] = []

    def fake_login(cfg: Any, *, output_write: Any = None) -> Any:
        calls.append(True)
        return Mock(email="user@example.com")

    monkeypatch.setattr(account_login_mod, "login", fake_login)

    setup_wizard_mod._maybe_offer_alysis_login(
        console, profile_result=_profile_step_for(_alysis_login_preset()), cfg=Mock()
    )

    assert calls == [True]


def test_setup_skips_login_offer_for_other_presets(monkeypatch) -> None:
    import alysis_code.account_login as account_login_mod

    console = Console(file=io.StringIO())
    other = next(
        p
        for p in setup_wizard_mod.provider_selection_presets()
        if p.key != _alysis_login_preset().key
    )
    calls: list[bool] = []
    monkeypatch.setattr(setup_wizard_mod, "_prompt_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(account_login_mod, "login", lambda *a, **k: calls.append(True))

    setup_wizard_mod._maybe_offer_alysis_login(
        console, profile_result=_profile_step_for(other), cfg=Mock()
    )

    assert calls == []  # never prompts or logs in for other providers


def test_setup_login_offer_declined_does_not_log_in(monkeypatch) -> None:
    import alysis_code.account_login as account_login_mod

    console = Console(file=io.StringIO())
    calls: list[bool] = []
    monkeypatch.setattr(setup_wizard_mod, "_prompt_yes_no", lambda *a, **k: False)
    monkeypatch.setattr(account_login_mod, "login", lambda *a, **k: calls.append(True))

    setup_wizard_mod._maybe_offer_alysis_login(
        console, profile_result=_profile_step_for(_alysis_login_preset()), cfg=Mock()
    )

    assert calls == []
