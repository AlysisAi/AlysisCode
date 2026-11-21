from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from alysis_code.agent_runtimes import (
    AgentRuntimeRegistry,
    AuthMethod,
    DuplicateRuntimeError,
    RuntimeAccountStatus,
    RuntimeCapabilities,
    RuntimeProbeStatus,
    RuntimeRegistryError,
    RuntimeTurnRequest,
    RuntimeTurnResult,
    UnknownRuntimeError,
)
from alysis_code.config import (
    AgentRuntimeSettings,
    AppConfig,
    ConfigError,
    config_path,
    load_config,
    save_config,
    set_config_value,
)


@dataclass(frozen=True)
class _FakeRuntime:
    runtime_id: str
    display_name: str = "Fake runtime"
    capabilities: RuntimeCapabilities = RuntimeCapabilities(streaming=True)
    auth_methods: tuple[AuthMethod, ...] = (AuthMethod(id="account", label="Provider account"),)

    def probe(self, settings: AgentRuntimeSettings) -> RuntimeProbeStatus:
        return RuntimeProbeStatus(available=True, executable=settings.executable)

    def account_status(self, settings: AgentRuntimeSettings) -> RuntimeAccountStatus:
        del settings
        return RuntimeAccountStatus(authenticated=True, auth_method_id="account")

    def login(
        self,
        settings: AgentRuntimeSettings,
        method_id: str,
    ) -> RuntimeAccountStatus:
        del settings
        return RuntimeAccountStatus(authenticated=True, auth_method_id=method_id)

    def logout(self, settings: AgentRuntimeSettings) -> RuntimeAccountStatus:
        del settings
        return RuntimeAccountStatus(authenticated=False)

    def run_turn(
        self,
        settings: AgentRuntimeSettings,
        request: RuntimeTurnRequest,
    ) -> RuntimeTurnResult:
        del settings
        return RuntimeTurnResult(
            runtime_id=self.runtime_id,
            command=("fake",),
            exit_code=0,
            final_message=request.prompt,
        )


@dataclass(frozen=True)
class _PluginRuntime(_FakeRuntime):
    adapter_id: str = "sample-cli"
    default_executable: str = "sample-agent"


def test_agent_runtime_config_defaults_to_native_execution() -> None:
    cfg = AppConfig()

    assert cfg.execution.backend == "native"
    assert cfg.execution.runtime is None
    assert cfg.agent_runtimes == {}


def test_agent_runtime_config_is_typed_and_validated() -> None:
    cfg = AppConfig(
        execution={"backend": "delegated", "runtime": "sample-agent"},
        agent_runtimes={
            "sample-agent": {
                "adapter": "sample_sdk",
                "executable": "sample-agent",
                "provider_managed_auth": True,
                "model": "gpt-5.5",
                "timeout_seconds": 120,
            }
        },
    )

    assert cfg.execution.backend == "delegated"
    assert cfg.execution.runtime == "sample-agent"
    assert cfg.agent_runtimes["sample-agent"] == AgentRuntimeSettings(
        adapter="sample_sdk",
        executable="sample-agent",
        provider_managed_auth=True,
        model="gpt-5.5",
        timeout_seconds=120,
    )

    with pytest.raises(ValidationError):
        AppConfig(execution={"backend": "unsupported"})
    with pytest.raises(ValidationError):
        AgentRuntimeSettings(adapter="sample_sdk", executable="sample-agent", timeout_seconds=0)
    with pytest.raises(ValidationError):
        AgentRuntimeSettings(
            adapter="sample_sdk",
            executable="sample-agent",
            timeout_seconds=float("inf"),
        )
    with pytest.raises(ValidationError, match="requires a selected agent runtime"):
        AppConfig(execution={"backend": "delegated"})
    with pytest.raises(ValidationError, match="cannot select a delegated agent runtime"):
        AppConfig(execution={"backend": "native", "runtime": "sample-agent"})
    with pytest.raises(ValidationError, match="has no agent_runtimes settings"):
        AppConfig(execution={"backend": "delegated", "runtime": "sample-agent"})


def test_agent_runtime_config_round_trips_unknown_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "execution": {
                    "backend": "delegated",
                    "runtime": "sample-agent",
                    "future_execution_option": {"enabled": True},
                },
                "agent_runtimes": {
                    "sample-agent": {
                        "adapter": "sample_sdk",
                        "executable": "sample-agent",
                        "provider_managed_auth": True,
                        "model": None,
                        "timeout_seconds": 90,
                        "future_runtime_option": "preserve-me",
                    }
                },
                "future_top_level_option": {"value": 7},
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.execution.backend == "delegated"
    assert cfg.execution.runtime == "sample-agent"
    assert cfg.execution.model_extra == {
        "future_execution_option": {"enabled": True},
    }
    assert cfg.agent_runtimes["sample-agent"].model_extra == {
        "future_runtime_option": "preserve-me",
    }
    assert cfg.extra_fields["future_top_level_option"] == {"value": 7}

    save_config(cfg)
    persisted = json.loads(config_path().read_text(encoding="utf-8"))
    assert persisted["execution"]["future_execution_option"] == {"enabled": True}
    assert persisted["agent_runtimes"]["sample-agent"]["future_runtime_option"] == "preserve-me"
    assert persisted["future_top_level_option"] == {"value": 7}


def test_agent_runtime_config_rejects_secret_looking_extension_fields() -> None:
    with pytest.raises(ValidationError, match="Provider credentials must remain owned"):
        AgentRuntimeSettings(
            adapter="third-party",
            executable="third-party-agent",
            access_token="must-not-persist",
        )

    with pytest.raises(ValidationError, match="Provider credentials must remain owned"):
        AgentRuntimeSettings(
            adapter="third-party",
            executable="third-party-agent",
            auth={"refresh-token": "must-not-persist"},
        )

    for field in (
        "token",
        "id_token",
        "authorization",
        "oauth",
        "authorization_header",
        "secret_value",
        "bearer_token_value",
        "client_token_value",
        "cookie",
        "cookies",
        "session_cookie",
    ):
        with pytest.raises(ValidationError, match="Provider credentials must remain owned"):
            AgentRuntimeSettings(
                adapter="third-party",
                executable="third-party-agent",
                **{field: "must-not-persist"},
            )

    with pytest.raises(ValidationError, match="Provider credentials must remain owned"):
        AppConfig(execution={"access_token": "must-not-persist"})

    allowed = AgentRuntimeSettings(
        adapter="third-party",
        executable="third-party-agent",
        credential_store_backend="provider-owned",
    )
    assert allowed.model_extra == {"credential_store_backend": "provider-owned"}

    token_budget = AgentRuntimeSettings(
        adapter="third-party",
        executable="third-party-agent",
        max_tokens=4096,
    )
    assert token_budget.model_extra == {"max_tokens": 4096}


def test_runtime_config_set_uses_registered_defaults_and_stays_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code.agent_runtimes import builtins

    plugin = _PluginRuntime(runtime_id="sample-agent")
    monkeypatch.setattr(builtins, "_plugin_runtime_adapters", lambda: (plugin,))
    cfg = AppConfig()

    set_config_value(cfg, "agent_runtimes.sample-agent.timeout_seconds", "42")

    settings = cfg.agent_runtimes["sample-agent"]
    assert settings.adapter == "sample-cli"
    assert settings.executable == "sample-agent"
    assert settings.timeout_seconds == 42

    set_config_value(cfg, "execution.backend", "delegated")
    assert cfg.execution.runtime == "sample-agent"
    AppConfig.model_validate(cfg.model_dump())


def test_alysis_ships_no_delegated_runtime_of_its_own() -> None:
    from alysis_code.agent_runtimes.builtins import (
        create_builtin_runtime_registry,
        runtime_setup_options,
    )

    assert len(create_builtin_runtime_registry()) == 0
    assert runtime_setup_options() == ()


def test_save_config_revalidates_mutated_execution_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = AppConfig()
    cfg.execution.backend = "delegated"

    with pytest.raises(ConfigError, match="requires a selected agent runtime"):
        save_config(cfg)


def test_legacy_config_without_runtime_fields_stays_native(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps({"base_url": "https://api.openai.com/v1", "model": "gpt-5.5"}),
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.execution.backend == "native"
    assert cfg.execution.runtime is None
    assert cfg.agent_runtimes == {}


@pytest.mark.parametrize("onboarded", [False, True])
def test_discarded_prototype_subscription_config_moves_to_the_native_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    onboarded: bool,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "onboarded": onboarded,
                "connection": {"kind": "subscription", "provider": "chatgpt"},
                "subscription": {
                    "chatgpt": {
                        "codex_cli": {
                            "path": "/opt/bin/prototype-agent",
                            "model": "gpt-test",
                            "timeout_seconds": 45,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.execution.backend == "native"
    assert cfg.execution.runtime is None
    assert cfg.agent_runtimes == {}
    assert "connection" not in cfg.extra_fields
    assert "subscription" not in cfg.extra_fields
    if onboarded:
        # The same subscription and model continue through Alysis Code's own agent.
        assert cfg.extra_fields["active_profile"] == "chatgpt-codex"
        assert cfg.model == "gpt-test"
        assert cfg.extra_fields["subscription_reconnect_required"] is True

    save_config(cfg)
    persisted = json.loads(config_path().read_text(encoding="utf-8"))
    assert "connection" not in persisted
    assert "subscription" not in persisted
    assert persisted["execution"] == {"backend": "native", "runtime": None}
    assert persisted["agent_runtimes"] == {}


def test_retired_runtime_settings_are_dropped_even_when_not_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "onboarded": True,
                "preserve_delegated_runtime": True,
                "execution": {"backend": "delegated", "runtime": "sample-agent"},
                "agent_runtimes": {
                    "sample-agent": {"adapter": "sample-cli", "executable": "sample-agent"},
                    "old-subscription": {"adapter": "codex-cli", "executable": "/opt/custom"},
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config()

    # Another delegated runtime stays selected; only the retired adapter's settings go.
    assert cfg.execution.backend == "delegated"
    assert cfg.execution.runtime == "sample-agent"
    assert set(cfg.agent_runtimes) == {"sample-agent"}
    assert "preserve_delegated_runtime" not in cfg.extra_fields


def test_execution_and_runtime_settings_are_settable() -> None:
    cfg = AppConfig()

    set_config_value(cfg, "execution.runtime", "third-party-agent")
    set_config_value(cfg, "execution.backend", "delegated")
    set_config_value(cfg, "agent_runtimes.third-party-agent.adapter", "acp")
    set_config_value(cfg, "agent_runtimes.third-party-agent.executable", "third-party-agent")
    set_config_value(cfg, "agent_runtimes.third-party-agent.model", "provider-default")
    set_config_value(cfg, "agent_runtimes.third-party-agent.timeout_seconds", "90")

    assert cfg.execution.backend == "delegated"
    assert cfg.execution.runtime == "third-party-agent"
    assert cfg.agent_runtimes["third-party-agent"] == AgentRuntimeSettings(
        adapter="acp",
        executable="third-party-agent",
        provider_managed_auth=True,
        model="provider-default",
        timeout_seconds=90,
    )

    set_config_value(cfg, "execution.backend", "native")
    assert cfg.execution.backend == "native"
    assert cfg.execution.runtime is None
    assert "third-party-agent" in cfg.agent_runtimes


def test_runtime_status_models_are_immutable() -> None:
    auth = AuthMethod(id="account", label="Provider account")
    capabilities = RuntimeCapabilities(streaming=True)
    probe = RuntimeProbeStatus(available=True, version="1.2.3")
    account = RuntimeAccountStatus(authenticated=True, account_label="user@example.com")
    request = RuntimeTurnRequest(prompt="hello", cwd=Path("."))
    result = RuntimeTurnResult(runtime_id="fake", command=("fake",), exit_code=0)

    with pytest.raises(FrozenInstanceError):
        auth.label = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        capabilities.streaming = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        probe.available = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        account.authenticated = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        request.prompt = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 1  # type: ignore[misc]


def test_runtime_registry_validates_duplicate_and_unknown_ids() -> None:
    runtime = _FakeRuntime(runtime_id="sample-agent")
    registry = AgentRuntimeRegistry([runtime])
    settings = AgentRuntimeSettings(adapter="sample_sdk", executable="sample-agent")

    assert registry.get("sample-agent") is runtime
    assert registry.runtime_ids() == ("sample-agent",)
    assert registry.adapters() == (runtime,)
    assert "sample-agent" in registry
    assert runtime.login(settings, "account") == RuntimeAccountStatus(
        authenticated=True,
        auth_method_id="account",
    )
    assert runtime.logout(settings) == RuntimeAccountStatus(authenticated=False)

    with pytest.raises(DuplicateRuntimeError, match="already registered"):
        registry.register(_FakeRuntime(runtime_id="sample-agent"))
    with pytest.raises(UnknownRuntimeError, match="Available runtimes: sample-agent"):
        registry.get("other-agent")
    with pytest.raises(RuntimeRegistryError, match="non-empty"):
        registry.get("  ")
