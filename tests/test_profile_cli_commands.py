from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.capabilities import build_capabilities_payload
from alysis_code.cli import app as alysis_app
from alysis_code.config import (
    AppConfig,
    load_config,
    load_persisted_profile_keys,
    save_config,
)
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_API_KEY": "",
        "OPENAI_API_KEY": "",
    }


def _seed_profiles(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = AppConfig(model="gpt-test")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, ProfileSpec(name="openai", base_url="https://api.openai.com/v1"))
    add_profile(cfg, ProfileSpec(name="anthropic", base_url="https://api.anthropic.com/v1"))
    set_active_profile(cfg, "openai")
    save_config(cfg)


def _seed_subscription_profile(tmp_path: Path, monkeypatch) -> ProfileSpec:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        # The adapter's current endpoint: a profile saved with a former one is moved to it
        # on load (test_subscription_endpoint_migration.py).
        base_url=OpenAICodexSubscriptionAuth.base_url,
        auth_provider="openai-codex",
        default_model="gpt-5.4",
        reasoning_effort="high",
    )
    cfg = AppConfig(model=profile.default_model)
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    save_config(cfg)
    return profile


def test_profile_list_shows_profiles_with_active_marker(monkeypatch, tmp_path: Path) -> None:
    _seed_profiles(tmp_path, monkeypatch)

    result = CliRunner().invoke(alysis_app, ["profile", "list"], env=_env(tmp_path))

    assert result.exit_code == 0
    assert "openai" in result.output
    assert "✓" in result.output


def test_profile_list_json_is_machine_readable_without_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_profiles(tmp_path, monkeypatch)
    secret = "sk-profile-contract-must-not-leak"
    env = _env(tmp_path)
    env["OPENAI_API_KEY"] = secret

    result = CliRunner().invoke(alysis_app, ["profile", "list", "--json"], env=env)

    assert result.exit_code == 0, result.output
    assert build_capabilities_payload()["features"]["profile.list-json"] is True
    payload = json.loads(result.stdout)
    openai = next(item for item in payload if item["name"] == "openai")
    assert set(openai) == {
        "name",
        "active",
        "protocol",
        "base_url",
        "api_key",
        "default_model",
        "web_search",
    }
    assert openai["active"] is True
    assert openai["api_key"] == "env (OPENAI_API_KEY)"
    assert secret not in result.stdout


def test_profile_select_model_atomically_activates_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-profile-selection-route-contract"
    monkeypatch.setenv("PROFILE_SELECTION_API_KEY", secret)
    cfg = AppConfig(
        model="subscription-model",
        execution={"backend": "delegated", "runtime": "openai-codex"},
        agent_runtimes={
            "openai-codex": {"adapter": "codex-cli", "executable": "codex"},
        },
    )
    cfg.extra_fields = {
        "profiles": {},
        "active_profile": "",
        "subscription_model_selection_required": "openai-codex",
        "subscription_reconnect_required": True,
        "onboarded": False,
    }
    add_profile(
        cfg,
        ProfileSpec(
            name="openai-api",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="PROFILE_SELECTION_API_KEY",
            default_model="old-api-model",
        ),
    )
    add_profile(
        cfg,
        ProfileSpec(
            name="chatgpt-codex",
            protocol="openai_responses",
            base_url="https://chatgpt.com/backend-api/codex",
            auth_provider="openai-codex",
            default_model="subscription-model",
            reasoning_effort="high",
        ),
    )
    set_active_profile(cfg, "chatgpt-codex")
    saved: list[AppConfig] = []
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        cli_mod,
        "save_config",
        lambda value: saved.append(value.model_copy(deep=True)),
    )

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "select-model", "openai-api", "gpt-5.6-terra", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "profile_name": "openai-api",
        "model": "gpt-5.6-terra",
        "auth_provider": None,
    }
    assert len(saved) == 1
    persisted = saved[0]
    assert persisted.extra_fields["active_profile"] == "openai-api"
    assert persisted.extra_fields["profiles"]["openai-api"]["default_model"] == "gpt-5.6-terra"
    assert persisted.model == "gpt-5.6-terra"
    assert persisted.base_url == "https://api.openai.com/v1"
    assert persisted.execution.backend == "native"
    assert persisted.execution.runtime is None
    assert persisted.extra_fields["onboarded"] is True
    assert persisted.extra_fields["subscription_model_selection_required"] == "openai-codex"
    assert persisted.extra_fields["subscription_reconnect_required"] is True
    assert secret not in result.stdout


def test_profile_select_model_rejects_hosted_profile_without_key_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    cfg = AppConfig(
        model="subscription-model",
        execution={"backend": "delegated", "runtime": "openai-codex"},
        agent_runtimes={
            "openai-codex": {"adapter": "codex-cli", "executable": "codex"},
        },
    )
    cfg.extra_fields = {
        "profiles": {},
        "active_profile": "",
        "subscription_model_selection_required": "openai-codex",
        "subscription_reconnect_required": True,
        "onboarded": False,
    }
    add_profile(
        cfg,
        ProfileSpec(
            name="hosted-api",
            base_url="https://api.example.test/v1",
            default_model="old-model",
        ),
    )
    original = cfg.model_copy(deep=True)
    saved: list[AppConfig] = []
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda value: saved.append(value))

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "select-model", "hosted-api", "new-model", "--json"],
    )

    assert result.exit_code == 2
    assert "API key is missing for hosted profile" in json.loads(result.stdout)["error"]
    assert saved == []
    assert cfg.model_dump() == original.model_dump()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:11434/v1",
        "http://127.42.0.9:9137/v1",
        "http://[::1]:8123/v1",
        "http://[::ffff:127.42.0.9]:8123/v1",
        "https://model.localhost.:9443/v1",
    ],
)
def test_profile_select_model_allows_keyless_local_profile_and_resets_subscription_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    endpoint: str,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    cfg = AppConfig(
        model="subscription-model",
        execution={"backend": "delegated", "runtime": "openai-codex"},
        agent_runtimes={
            "openai-codex": {"adapter": "codex-cli", "executable": "codex"},
        },
    )
    cfg.extra_fields = {
        "profiles": {},
        "active_profile": "",
        "subscription_model_selection_required": "openai-codex",
        "subscription_reconnect_required": True,
        "onboarded": False,
    }
    add_profile(
        cfg,
        ProfileSpec(
            name="ollama",
            base_url=endpoint,
            default_model="old-local-model",
        ),
    )
    saved: list[AppConfig] = []
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        cli_mod,
        "save_config",
        lambda value: saved.append(value.model_copy(deep=True)),
    )

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "select-model", "ollama", "llama3.3", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert len(saved) == 1
    persisted = saved[0]
    assert persisted.extra_fields["active_profile"] == "ollama"
    assert persisted.model == "llama3.3"
    assert persisted.execution.backend == "native"
    assert persisted.execution.runtime is None
    assert persisted.extra_fields["onboarded"] is True
    assert persisted.extra_fields["subscription_model_selection_required"] == "openai-codex"
    assert persisted.extra_fields["subscription_reconnect_required"] is True


@pytest.mark.parametrize(
    ("profile_name", "model", "error_text"),
    [
        ("missing", "gpt-test", "Profile not found"),
        (
            "chatgpt-codex",
            "gpt-test",
            "Subscription models must be selected with `alysis auth select-model`.",
        ),
        ("openai", "   ", "Profile name and model are required"),
    ],
)
def test_profile_select_model_rejects_invalid_inputs_without_persisting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile_name: str,
    model: str,
    error_text: str,
) -> None:
    _seed_profiles(tmp_path, monkeypatch)
    cfg = load_config()
    add_profile(
        cfg,
        ProfileSpec(
            name="chatgpt-codex",
            protocol="openai_responses",
            base_url="https://chatgpt.com/backend-api/codex",
            auth_provider="openai-codex",
            default_model="subscription-model",
            reasoning_effort="high",
        ),
    )
    original = cfg.model_copy(deep=True)
    saved: list[AppConfig] = []
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda value: saved.append(value))

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "select-model", profile_name, model, "--json"],
    )

    assert result.exit_code == 2
    assert error_text in json.loads(result.stdout)["error"]
    assert saved == []
    assert cfg.model_dump() == original.model_dump()


def test_profile_use_switches_active(monkeypatch, tmp_path: Path) -> None:
    _seed_profiles(tmp_path, monkeypatch)

    result = CliRunner().invoke(alysis_app, ["profile", "use", "anthropic"], env=_env(tmp_path))

    assert result.exit_code == 0
    assert load_config().extra_fields["active_profile"] == "anthropic"


def test_profile_use_unknown_errors(tmp_path: Path) -> None:
    result = CliRunner().invoke(alysis_app, ["profile", "use", "missing"], env=_env(tmp_path))

    assert result.exit_code == 2
    assert "Profile not found" in result.output


def test_profile_add_creates_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    result = CliRunner().invoke(
        alysis_app,
        ["profile", "add", "custom", "--base-url", "https://example.com/v1"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "custom" in load_config().extra_fields["profiles"]


def test_profile_add_accepts_safe_reasoning_trace_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    result = CliRunner().invoke(
        alysis_app,
        [
            "profile",
            "add",
            "custom",
            "--base-url",
            "https://example.com/v1",
            "--reasoning-trace-adapter",
            "openrouter_reasoning",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    profile = load_config().extra_fields["profiles"]["custom"]
    assert profile["reasoning_trace_adapter"] == "openrouter_reasoning"


def test_profile_add_cannot_overwrite_subscription_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _seed_subscription_profile(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        alysis_app,
        [
            "profile",
            "add",
            original.name,
            "--base-url",
            "https://example.com/v1",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "managed by the 'openai-codex' subscription connection" in result.output
    assert load_config().extra_fields["profiles"][original.name] == original.to_dict()


def test_profile_remove_with_yes_skips_confirm(monkeypatch, tmp_path: Path) -> None:
    _seed_profiles(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "remove", "anthropic", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "anthropic" not in load_config().extra_fields["profiles"]


def test_profile_preset_clones_into_named_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    result = CliRunner().invoke(
        alysis_app,
        ["profile", "preset", "anthropic", "--as", "claude"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    profile = load_config().extra_fields["profiles"]["claude"]
    assert profile["protocol"] == "anthropic_messages"
    assert profile["base_url"] == "https://api.anthropic.com/v1"
    assert profile["default_model"] == "claude-sonnet-5"
    assert profile["extra_headers"] == {}


def test_profile_preset_yes_cannot_overwrite_subscription_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _seed_subscription_profile(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        alysis_app,
        [
            "profile",
            "preset",
            "openai-responses",
            "--as",
            original.name,
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "cannot be overwritten through generic profile commands" in " ".join(
        result.output.split()
    )
    assert load_config().extra_fields["profiles"][original.name] == original.to_dict()


def test_profile_set_key_persists_per_profile(monkeypatch, tmp_path: Path) -> None:
    _seed_profiles(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "set-key", "anthropic", "--key", "sk-ant-test"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert load_persisted_profile_keys()["anthropic"] == "sk-ant-test"


def test_profile_presets_lists_known_presets(tmp_path: Path) -> None:
    result = CliRunner().invoke(alysis_app, ["profile", "presets"], env=_env(tmp_path))

    assert result.exit_code == 0
    assert "anthropic" in result.output
    assert "compatibility" in result.output
    assert "native" in result.output
    assert "openai_responses" in result.output
    assert "openrouter" in result.output
    assert "qwen-us" in result.output


def test_profile_presets_json_matches_desktop_contract_without_secrets(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        alysis_app,
        ["profile", "presets", "--json"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert build_capabilities_payload()["features"]["profile.presets-json"] is True
    payload = json.loads(result.stdout)
    openai = next(item for item in payload if item["key"] == "openai-responses")
    assert set(openai) == {
        "key",
        "label",
        "protocol",
        "kind",
        "base_url",
        "api_key_env",
        "requires_api_key",
        "models",
        "default_model",
        "connection_description",
        "setup_warning",
    }
    assert openai["protocol"] == "openai_responses"
    assert openai["kind"] == "native"
    assert openai["requires_api_key"] is True
    assert isinstance(openai["connection_description"], str)
    assert openai["setup_warning"] is None or isinstance(openai["setup_warning"], str)
    assert openai["default_model"] == openai["models"][0]["id"]
    assert all(
        set(model)
        == {
            "id",
            "label",
            "description",
            "protocol",
            "context_window_tokens",
            "max_output_tokens",
            "supports_vision",
            "supports_reasoning",
            "input_modalities",
            "reasoning_efforts",
            "default_reasoning_effort",
        }
        for model in openai["models"]
    )
    assert "sk-" not in result.stdout


def test_qwen_us_preset_uses_dashscope_virginia_endpoint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "preset", "qwen-us"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    profile = load_config().extra_fields["profiles"]["qwen-us"]
    assert profile["base_url"] == "https://dashscope-us.aliyuncs.com/compatible-mode/v1"
    assert profile["default_model"] == "qwen3.7-plus"


def test_profile_convert_to_native_updates_protocol_without_exposing_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _seed_profiles(tmp_path, monkeypatch)
    cfg = load_config()
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1",
            default_model="claude-sonnet-4-6",
            web_search_adapter="anthropic_messages",
            web_search_model="legacy-search-model",
        ),
    )
    save_config(cfg)
    set_key = CliRunner().invoke(
        alysis_app,
        ["profile", "set-key", "anthropic", "--key", "sk-ant-test"],
        env=_env(tmp_path),
    )
    assert set_key.exit_code == 0

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "convert", "anthropic", "--to", "native", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "anthropic_messages" in result.output
    assert "web_search_model" in result.output
    assert "legacy-search-model" in result.output
    assert "sk-ant-test" not in result.output
    profile = load_config().extra_fields["profiles"]["anthropic"]
    assert profile["protocol"] == "anthropic_messages"
    assert profile["base_url"] == "https://api.anthropic.com/v1"
    assert profile["web_search_adapter"] == "anthropic_messages"
    assert profile["web_search_model"] == ""
    assert load_persisted_profile_keys()["anthropic"] == "sk-ant-test"


def test_profile_convert_cannot_convert_subscription_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _seed_subscription_profile(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        alysis_app,
        [
            "profile",
            "convert",
            original.name,
            "--to",
            "compatibility",
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "cannot be converted through generic profile commands" in " ".join(result.output.split())
    assert load_config().extra_fields["profiles"][original.name] == original.to_dict()


def test_profile_convert_to_compatibility_updates_gemini_native_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = AppConfig(model="gemini-3-flash-preview")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="gemini-native",
            protocol="gemini_generate_content",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-3-flash-preview",
            web_search_adapter="gemini_grounding",
        ),
    )
    set_active_profile(cfg, "gemini-native")
    save_config(cfg)

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "convert", "--to", "compatibility", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    profile = load_config().extra_fields["profiles"]["gemini-native"]
    assert profile["protocol"] == "openai_compat"
    assert profile["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert profile["default_model"] == "gemini-3-flash-preview"
    assert profile["web_search_adapter"] == "gemini_grounding"


def _record_key_checks(monkeypatch: pytest.MonkeyPatch, status: str, message: str) -> list[dict]:
    from alysis_code.cli_impl import setup_wizard

    calls: list[dict] = []

    def fake_validate(**kwargs):
        calls.append(kwargs)
        return setup_wizard._ApiKeyValidationResult(status=status, message=message)

    monkeypatch.setattr(setup_wizard, "_validate_api_key", fake_validate)
    return calls


def test_profile_check_rejects_a_key_the_provider_refuses_and_saves_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_profiles(tmp_path, monkeypatch)
    before = (tmp_path / "config.json").read_bytes()
    calls = _record_key_checks(monkeypatch, "failed", "Provider rejected the API key (HTTP 401).")

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "check", "openai-responses", "--api-key-stdin", "--json"],
        input="sk-wrong-key\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "message": "Provider rejected the API key (HTTP 401).",
        "ok": False,
        "profile": "openai-responses",
        "status": "failed",
    }
    assert calls[0]["api_key"] == "sk-wrong-key"
    assert calls[0]["profile"].name == "openai-responses"
    # The setup wizard's request: the preset's small validation model.
    assert calls[0]["validation_model"] == "gpt-5.6-luna"
    assert (tmp_path / "config.json").read_bytes() == before
    assert "openai-responses" not in load_persisted_profile_keys()


def test_profile_check_accepts_a_working_key_against_the_base_url_given(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_profiles(tmp_path, monkeypatch)
    calls = _record_key_checks(monkeypatch, "validated", "")

    result = CliRunner().invoke(
        alysis_app,
        [
            "profile",
            "check",
            "openai-responses",
            "--api-key-stdin",
            "--base-url",
            "https://gateway.example.test/v1",
            "--json",
        ],
        input="sk-good-key\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["ok"] is True
    assert json.loads(result.output)["status"] == "validated"
    assert calls[0]["profile"].base_url == "https://gateway.example.test/v1"


def test_profile_check_uses_the_saved_key_of_a_saved_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_profiles(tmp_path, monkeypatch)
    calls = _record_key_checks(
        monkeypatch, "inconclusive", "Provider returned HTTP 503 during validation."
    )
    env = _env(tmp_path)
    env["OPENAI_API_KEY"] = "sk-from-environment"

    result = CliRunner().invoke(alysis_app, ["profile", "check", "openai", "--json"], env=env)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True and payload["status"] == "inconclusive"
    assert calls[0]["profile"].name == "openai"
    assert calls[0]["api_key"] == "sk-from-environment"


def test_profile_check_refuses_an_unknown_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_profiles(tmp_path, monkeypatch)
    calls = _record_key_checks(monkeypatch, "validated", "")

    result = CliRunner().invoke(
        alysis_app, ["profile", "check", "nope", "--json"], env=_env(tmp_path)
    )

    assert result.exit_code == 2
    assert json.loads(result.output) == {"error": "Unknown profile or preset: nope", "ok": False}
    assert calls == []
    assert build_capabilities_payload()["features"]["profile.check-json"] is True
