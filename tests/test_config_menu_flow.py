from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import pytest
from click import Abort
from rich.console import Console

from alysis_code.cli_impl import config_menu as config_menu_mod
from alysis_code.cli_impl.config_menu import ROLE_ORDER, thinking_label_from_cfg
from alysis_code.config import (
    AppConfig,
    load_config,
    save_config,
)
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile


class PromptScript:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, text: str, *args: Any, **kwargs: Any) -> str:
        del args
        self.calls.append((text, kwargs))
        if not self.answers:
            raise AssertionError(f"Unexpected prompt: {text}")
        return self.answers.pop(0)


class InlineChoiceScript:
    def __init__(self, answers: list[str | None]) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str | None:
        self.calls.append(kwargs)
        if not self.answers:
            raise AssertionError(f"Unexpected inline picker: {kwargs}")
        return self.answers.pop(0)


class ConfirmScript:
    def __init__(self, answers: list[bool]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, text: str, *args: Any, **kwargs: Any) -> bool:
        del args
        self.calls.append((text, kwargs))
        if not self.answers:
            raise AssertionError(f"Unexpected confirm: {text}")
        return self.answers.pop(0)


def _patch_console(monkeypatch) -> io.StringIO:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    monkeypatch.setattr(config_menu_mod, "_resolve_console", lambda: console)
    return output


def _seed_config(tmp_path: Path, monkeypatch, *, model: str = "old") -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    cfg.model = model
    save_config(cfg)


def _menu_section_choice(section: str) -> str:
    state = config_menu_mod.ConfigMenuState.from_cfg(load_config())
    return str(
        next(
            index
            for index, (value, _label, _summary) in enumerate(
                config_menu_mod._top_level_menu_rows(state), start=1
            )
            if value == section
        )
    )


def _profile_edit_state() -> config_menu_mod.ConfigMenuState:
    cfg = AppConfig(model="old")
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key_env="OLD_API_KEY_ENV",
            default_model="claude-sonnet-4-6",
            notes="old notes",
        ),
    )
    set_active_profile(cfg, "anthropic")
    return config_menu_mod.ConfigMenuState.from_cfg(cfg)


def test_main_menu_save_when_no_changes_returns_no_save(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    prompt = PromptScript(["s"])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    def fail_confirm(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("cancel confirmation should not be called")

    monkeypatch.setattr(config_menu_mod, "_confirm_cancel_when_dirty", fail_confirm)

    result = config_menu_mod.run_config_menu()

    assert result.saved is True
    assert result.changes == {}


def test_main_menu_uses_inline_prompt(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    prompt = PromptScript(["q"])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    def fail_picker(**_kwargs: Any) -> str | None:
        raise AssertionError("top-level menu should not use the inline section picker")

    monkeypatch.setattr(config_menu_mod, "_prompt_inline_choice", fail_picker)

    result = config_menu_mod.run_config_menu()

    assert result.saved is False
    assert prompt.calls[0][0] == "Choice"
    assert "1) Provider Profile" in output.getvalue()


def test_default_section_updates_model(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    prompt = PromptScript(["3", "gpt-5", "", "60.0", "s"])
    inline = InlineChoiceScript([config_menu_mod._CUSTOM_MODEL_VALUE, "auto"])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)
    monkeypatch.setattr(config_menu_mod, "_prompt_inline_choice", inline)

    result = config_menu_mod.run_config_menu()

    assert result.saved is True
    assert load_config().model == "gpt-5"


def test_thinking_pick_uses_inline_picker(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    prompt = PromptScript(["3", "", "60", "s"])
    inline = InlineChoiceScript(["old", "high"])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)
    monkeypatch.setattr(config_menu_mod, "_prompt_inline_choice", inline)

    result = config_menu_mod.run_config_menu()

    assert result.saved is True
    assert thinking_label_from_cfg(load_config()) == "high"
    assert inline.calls[0]["title"] == "Default Model"


def test_save_preserves_unexposed_role_model_keys(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Keys the menu does not expose — including the deprecated "router" key
    # from the removed semantic router and the retired "forge_role_models"
    # block — must survive an unrelated save.
    _seed_config(tmp_path, monkeypatch)
    cfg = load_config()
    cfg.extra_fields["role_models"] = {
        "comprehension": "vision-reader-model",
        "router": "legacy-router-model",
    }
    cfg.extra_fields["forge_role_models"] = {"comprehension": "forge-vision-reader-model"}
    save_config(cfg)
    _patch_console(monkeypatch)
    answers: list[str] = [_menu_section_choice("subagents")]
    for role in ROLE_ORDER:
        answers.append("anthropic/claude-sonnet-4-6" if role == "coding" else "")
        if role in config_menu_mod._ROLE_TEMPERATURE_FIELDS:
            answers.append("")
    answers.append("s")
    prompt = PromptScript(answers)
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    result = config_menu_mod.run_config_menu()
    saved_cfg = load_config()

    assert result.saved is True
    assert saved_cfg.extra_fields["role_models"] == {
        "comprehension": "vision-reader-model",
        "router": "legacy-router-model",
        "coding": "anthropic/claude-sonnet-4-6",
    }
    assert saved_cfg.extra_fields["forge_role_models"] == {
        "comprehension": "forge-vision-reader-model"
    }


def test_subagent_role_override(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    answers: list[str] = [_menu_section_choice("subagents")]
    for role in ROLE_ORDER:
        answers.append("anthropic/claude-sonnet-4-6" if role == "coding" else "")
        if role in config_menu_mod._ROLE_TEMPERATURE_FIELDS:
            answers.append("")
    answers.append("s")
    prompt = PromptScript(answers)
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    result = config_menu_mod.run_config_menu()

    role_models = load_config().extra_fields["role_models"]
    assert result.saved is True
    assert role_models["coding"] == "anthropic/claude-sonnet-4-6"
    assert set(role_models) == {"coding"}


def test_legacy_router_override_has_no_menu_row_and_stays_out_of_summaries() -> None:
    base_state = config_menu_mod.ConfigMenuState.from_cfg(AppConfig(model="default"))
    cfg = AppConfig(model="default")
    cfg.extra_fields = {"role_models": {"router": "cheap-router-model"}}
    state = config_menu_mod.ConfigMenuState.from_cfg(cfg)

    base_rows = {
        value: (label, summary)
        for value, label, summary in config_menu_mod._top_level_menu_rows(base_state)
    }
    rows = {
        value: (label, summary)
        for value, label, summary in config_menu_mod._top_level_menu_rows(state)
    }

    assert "router" not in rows
    assert all("Routing" not in label for label, _summary in rows.values())
    assert rows["subagents"][1] == base_rows["subagents"][1]


def test_web_search_section_updates_policy_and_backend(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    state = config_menu_mod.ConfigMenuState.from_cfg(AppConfig(model="default"))
    picker = InlineChoiceScript(["off", "external"])
    monkeypatch.setattr(config_menu_mod, "_run_config_picker", picker)

    config_menu_mod._run_web_search_section(state, console)

    assert state.fields["web_search_policy"] == "off"
    assert state.fields["web_search_mode"] == "external"
    assert [call["title"] for call in picker.calls] == ["Web Search", "Web Search Backend"]
    assert "updated" in output.getvalue()


def test_cancel_with_dirty_prompts_confirm(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    prompt = PromptScript(["3", "gpt-5", "", "60", "q"])
    inline = InlineChoiceScript([config_menu_mod._CUSTOM_MODEL_VALUE, "auto"])
    confirm = ConfirmScript([True])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)
    monkeypatch.setattr(config_menu_mod, "_prompt_inline_choice", inline)
    monkeypatch.setattr(config_menu_mod.typer, "confirm", confirm)

    result = config_menu_mod.run_config_menu()

    assert result.saved is False
    assert load_config().model == "old"
    assert confirm.calls[0][0] == "Discard pending changes?"

    output = _patch_console(monkeypatch)
    prompt = PromptScript(["3", "gpt-5", "", "60", "q", "s"])
    inline = InlineChoiceScript([config_menu_mod._CUSTOM_MODEL_VALUE, "auto"])
    confirm = ConfirmScript([False])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)
    monkeypatch.setattr(config_menu_mod, "_prompt_inline_choice", inline)
    monkeypatch.setattr(config_menu_mod.typer, "confirm", confirm)

    result = config_menu_mod.run_config_menu()

    assert result.saved is True
    assert output.getvalue().count("Alysis Code Configuration") >= 2
    assert load_config().model == "gpt-5"


def test_auto_focus_api_key_runs_section_first(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    prompt = PromptScript(["", ""])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    result = config_menu_mod.run_config_menu(auto_focus="api_key")

    assert result.saved is False
    assert prompt.calls[0][0] == 'New API key (Enter to keep current, "clear" to remove)'


def test_config_menu_provider_section_lists_profiles(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    cfg = load_config()
    add_profile(cfg, ProfileSpec(name="anthropic", base_url="https://api.anthropic.com/v1/openai"))
    save_config(cfg)
    output = _patch_console(monkeypatch)
    prompt = PromptScript(["1", "q"])
    inline = InlineChoiceScript(["back"])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)
    monkeypatch.setattr(config_menu_mod, "_prompt_inline_choice", inline)

    result = config_menu_mod.run_config_menu()

    assert result.saved is False
    assert "anthropic" in output.getvalue()


def test_config_menu_preset_rows_explain_compatibility_and_native_modes() -> None:
    presets = config_menu_mod._ordered_profile_presets_for_setup()
    keys = [preset.key for preset in presets]
    advanced = config_menu_mod._advanced_profile_presets_for_setup()
    advanced_keys = [preset.key for preset in advanced]

    # The /config "add preset" picker mirrors setup: native first-party
    # providers lead and every other hosted provider follows in registration
    # order; the account-gated hosted MiMo preset stays behind the advanced
    # picker while no hosted campaign is active.
    assert keys[:3] == ["openai-responses", "anthropic", "gemini"]
    assert "alysis" not in keys
    assert "xiaomi-mimo" in keys
    assert "deepseek" in keys
    assert "anthropic-compat" not in keys
    assert "gemini-compat" not in keys
    assert "ollama" not in keys
    assert "alysis" in advanced_keys
    assert "deepseek" not in advanced_keys
    assert "anthropic-compat" in advanced_keys
    assert "gemini-compat" in advanced_keys
    assert "ollama" in advanced_keys
    assert "Compatibility protocol" in config_menu_mod._preset_description(
        next(preset for preset in advanced if preset.key == "anthropic-compat")
    )
    assert "Native first-party protocol" in config_menu_mod._preset_description(
        next(preset for preset in presets if preset.key == "anthropic")
    )


def test_config_menu_advanced_preset_flow_can_choose_gemini_compat(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    state = config_menu_mod.ConfigMenuState.from_cfg(AppConfig(model="old"))
    picker = InlineChoiceScript([config_menu_mod._ADVANCED_PROVIDER_PRESETS_VALUE, "gemini-compat"])
    prompt = PromptScript(["gemini-compat"])
    monkeypatch.setattr(config_menu_mod, "_run_config_picker", picker)
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    config_menu_mod._run_profile_add_preset(state, console)

    profile = state.profiles["gemini-compat"]
    assert profile["protocol"] == "openai_compat"
    assert profile["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"


def test_config_menu_add_preset_surfaces_provider_diagnostic_warnings(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    state = config_menu_mod.ConfigMenuState.from_cfg(
        AppConfig(model="old", web_search_mode="external")
    )
    picker = InlineChoiceScript(["openai-responses"])
    prompt = PromptScript(["openai-responses"])
    monkeypatch.setattr(config_menu_mod, "_run_config_picker", picker)
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    config_menu_mod._run_profile_add_preset(state, console)

    # The picker highlights the first native provider by default; the diagnostic
    # warnings below are what this test actually guards.
    assert picker.calls[0]["current_value"] == "openai-responses"
    rendered = output.getvalue()
    assert "Provider diagnostic:" in rendered
    assert "web_search_mode=external is incompatible" in rendered
    assert "web_search_adapter=openai_responses" in rendered


def test_config_menu_active_preset_prefers_active_profile_protocol_over_base_url() -> None:
    cfg = AppConfig(model="gpt-5.5")
    add_profile(
        cfg,
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            default_model="gpt-5.5",
            web_search_adapter="openai_responses",
        ),
    )
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            protocol="anthropic_messages",
            base_url="https://api.anthropic.com/v1",
            default_model="claude-sonnet-4-6",
            web_search_adapter="anthropic_messages",
        ),
    )
    add_profile(
        cfg,
        ProfileSpec(
            name="gemini",
            protocol="gemini_generate_content",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            default_model="gemini-3.5-flash",
            web_search_adapter="gemini_grounding",
        ),
    )

    set_active_profile(cfg, "openai-responses")
    state = config_menu_mod.ConfigMenuState.from_cfg(cfg)
    assert config_menu_mod._active_preset(state).key == "openai-responses"

    state.set_active_profile_name("anthropic")
    assert config_menu_mod._active_preset(state).key == "anthropic"

    state.set_active_profile_name("gemini")
    assert config_menu_mod._active_preset(state).key == "gemini"


def test_config_menu_switch_active_profile_persists(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    cfg = load_config()
    add_profile(cfg, ProfileSpec(name="openai", base_url="https://api.openai.com/v1"))
    add_profile(cfg, ProfileSpec(name="anthropic", base_url="https://api.anthropic.com/v1/openai"))
    set_active_profile(cfg, "openai")
    save_config(cfg)
    _patch_console(monkeypatch)
    prompt = PromptScript(["1", "s"])
    inline = InlineChoiceScript(["switch", "anthropic"])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)
    monkeypatch.setattr(config_menu_mod, "_prompt_inline_choice", inline)

    result = config_menu_mod.run_config_menu()

    assert result.saved is True
    assert load_config().extra_fields["active_profile"] == "anthropic"


def test_api_key_clear_flow_persists_removal(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    prompt = PromptScript(["2", "clear", "s"])
    confirm = ConfirmScript([True])
    cleared: list[str] = []

    def clear_api_key() -> bool:
        cleared.append("legacy")
        return True

    def clear_profile_key(profile_name: str) -> bool:
        cleared.append(f"profile:{profile_name}")
        return True

    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)
    monkeypatch.setattr(config_menu_mod.typer, "confirm", confirm)
    monkeypatch.setattr(config_menu_mod, "clear_persisted_api_key", clear_api_key)
    monkeypatch.setattr(config_menu_mod, "clear_persisted_profile_key", clear_profile_key)

    result = config_menu_mod.run_config_menu()

    assert result.saved is True
    assert result.api_key_changed is True
    assert len(cleared) == 1
    assert cleared[0] == "legacy" or cleared[0].startswith("profile:")
    assert confirm.calls[0][0] == "Clear the stored API key?"


def test_config_menu_add_custom_profile_persists(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    prompt = PromptScript(
        [
            "1",
            "anthropic",
            "https://api.anthropic.com/v1/openai",
            "",
            "s",
        ]
    )
    inline = InlineChoiceScript(["add_custom"])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)
    monkeypatch.setattr(config_menu_mod, "_prompt_inline_choice", inline)

    result = config_menu_mod.run_config_menu()

    cfg = load_config()
    profiles = cfg.extra_fields["profiles"]
    assert result.saved is True
    assert "anthropic" in profiles
    assert profiles["anthropic"]["base_url"] == "https://api.anthropic.com/v1/openai"
    assert cfg.extra_fields["active_profile"] == "anthropic"


def test_config_menu_remove_profile_persists(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    cfg = load_config()
    add_profile(cfg, ProfileSpec(name="openai", base_url="https://api.openai.com/v1"))
    add_profile(cfg, ProfileSpec(name="anthropic", base_url="https://api.anthropic.com/v1/openai"))
    set_active_profile(cfg, "openai")
    save_config(cfg)
    _patch_console(monkeypatch)
    prompt = PromptScript(["1", "s"])
    inline = InlineChoiceScript(["remove", "anthropic"])
    confirm = ConfirmScript([True])
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)
    monkeypatch.setattr(config_menu_mod, "_prompt_inline_choice", inline)
    monkeypatch.setattr(config_menu_mod.typer, "confirm", confirm)

    result = config_menu_mod.run_config_menu()

    profiles = load_config().extra_fields["profiles"]
    assert result.saved is True
    assert "anthropic" not in profiles
    assert confirm.calls[0][0] == "Remove profile anthropic?"


def test_profile_edit_accepts_env_var_name(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    state = _profile_edit_state()
    prompt = PromptScript(
        [
            "",
            "ANTHROPIC_API_KEY",
            "",
            "",
            "",
            "",
            "",
            "production profile",
        ]
    )
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    config_menu_mod._run_profile_edit_current(state, console)

    profile = ProfileSpec.from_dict("anthropic", state.profiles["anthropic"])
    assert profile.api_key_env == "ANTHROPIC_API_KEY"
    assert profile.notes == "production profile"
    assert prompt.calls[1][0] == config_menu_mod._API_KEY_ENV_PROMPT
    rendered = output.getvalue()
    assert "Edit Profile: anthropic" in rendered
    assert "API key env var NAME" in rendered
    assert "Web search adapter" in rendered
    assert "Web search model" in rendered
    assert "The actual API key" in rendered
    assert "This field is only the name of the env variable" in rendered


def test_profile_edit_reprompts_invalid_web_search_adapter(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    state = _profile_edit_state()
    prompt = PromptScript(
        [
            "",
            "",
            "",
            "",
            "bad_adapter",
            "groq_compound",
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    config_menu_mod._run_profile_edit_current(state, console)

    profile = ProfileSpec.from_dict("anthropic", state.profiles["anthropic"])
    assert profile.web_search_adapter == "groq_compound"
    assert prompt.calls[4][0] == "Web search adapter"
    assert prompt.calls[5][0] == "Web search adapter"
    rendered = output.getvalue()
    assert "web_search_adapter must be one of:" in rendered
    assert "Allowed adapters:" in rendered


def test_profile_edit_env_var_secret_paste_warns_and_reprompts(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    state = _profile_edit_state()
    pasted_secret = "sk-ant-api03-" + ("X" * 40)
    prompt = PromptScript(
        [
            "",
            pasted_secret,
            "ANTHROPIC_API_KEY",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    config_menu_mod._run_profile_edit_current(state, console)

    profile = ProfileSpec.from_dict("anthropic", state.profiles["anthropic"])
    assert profile.api_key_env == "ANTHROPIC_API_KEY"
    assert prompt.calls[2][0] == "Re-enter API key env var name (or 'force' to keep this value)"
    rendered = output.getvalue()
    assert "That looks like an API key, not a API key env var name." in rendered
    assert "Keys here are written to plaintext profile config." in rendered


def test_profile_edit_env_var_secret_paste_force_accepts_with_warning(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    state = _profile_edit_state()
    pasted_secret = "sk-ant-api03-" + ("Y" * 40)
    prompt = PromptScript(
        [
            "",
            pasted_secret,
            "force",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    config_menu_mod._run_profile_edit_current(state, console)

    profile = ProfileSpec.from_dict("anthropic", state.profiles["anthropic"])
    assert profile.api_key_env == pasted_secret
    assert "Confirmed: storing potentially-sensitive value in plaintext." in output.getvalue()


def test_profile_edit_extra_headers_whole_secret_warns_and_reprompts(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    state = _profile_edit_state()
    pasted_secret = "sk-ant-api03-" + ("H" * 40)
    prompt = PromptScript(
        [
            "",
            "ANTHROPIC_API_KEY",
            "",
            "",
            "",
            "",
            pasted_secret,
            "X-Trace=1",
            "",
        ]
    )
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    config_menu_mod._run_profile_edit_current(state, console)

    profile = ProfileSpec.from_dict("anthropic", state.profiles["anthropic"])
    assert profile.extra_headers == {"x-trace": "1"}
    assert prompt.calls[7][0] == "Re-enter Extra headers (or 'force' to keep this value)"
    assert "That looks like an API key, not a Extra headers." in output.getvalue()


def test_profile_edit_notes_secret_paste_warns_and_reprompts(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    state = _profile_edit_state()
    pasted_secret = "sk-ant-api03-" + ("Z" * 40)
    prompt = PromptScript(
        [
            "",
            "ANTHROPIC_API_KEY",
            "",
            "",
            "",
            "",
            "",
            pasted_secret,
            "safe notes",
        ]
    )
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    config_menu_mod._run_profile_edit_current(state, console)

    profile = ProfileSpec.from_dict("anthropic", state.profiles["anthropic"])
    assert profile.notes == "safe notes"
    assert prompt.calls[8][0] == "Re-enter Notes (or 'force' to keep this value)"
    assert "That looks like an API key, not a Notes." in output.getvalue()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sk-ant-api03-" + ("A" * 32), True),
        ("sk-" + ("B" * 32), True),
        ("sk_" + ("C" * 32), True),
        ("anthropic-" + ("D" * 24), True),
        ("Bearer " + ("E" * 32), True),
        ("ghp_" + ("F" * 32), True),
        ("gho_" + ("G" * 32), True),
        ("github_pat_" + ("H" * 32), True),
        ("A" * 40, True),
        ("", False),
        ("sk-short", False),
        ("ANTHROPIC_API_KEY", False),
        ("claude-sonnet-4-6", False),
        ("https://api.anthropic.com/v1", False),
        ("this is a long non-secret note with spaces", False),
    ],
)
def test_looks_like_secret(value: str, expected: bool) -> None:
    assert config_menu_mod._looks_like_secret(value) is expected


def test_section_abort_returns_to_top_level(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    answers = iter(["3", "q"])

    def prompt(text: str, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        if text == "Model":
            raise Abort()
        return next(answers)

    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)
    monkeypatch.setattr(
        config_menu_mod,
        "_prompt_inline_choice",
        InlineChoiceScript([config_menu_mod._CUSTOM_MODEL_VALUE]),
    )

    result = config_menu_mod.run_config_menu()

    assert result.saved is False
    assert 'Section "Default Model" cancelled.' in output.getvalue()


def test_save_and_exit_reports_permission_error_without_raising(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _seed_config(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    state = config_menu_mod.ConfigMenuState.from_cfg(load_config())
    state.set_field("model", "new")

    def fail_save_config(_cfg: AppConfig) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(config_menu_mod, "save_config", fail_save_config)

    result = config_menu_mod._save_and_exit(
        state,
        load_config(),
        config_menu_mod._resolve_console(),
    )

    assert result.saved is False
    assert result.error is not None
    assert "read-only file system" in result.error
    assert "Failed to save config:" in output.getvalue()
    assert "Check write permission on" in output.getvalue()


def test_subagent_section_abort_rolls_back_partial_role_edits(monkeypatch) -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    state = config_menu_mod.ConfigMenuState.from_cfg(AppConfig(model="default"))
    state.set_role_model("coding", "old-coding")
    state.set_role_model("planner", "old-planner")
    state.set_role_temperature("coding", "0.1")
    state.set_role_temperature("planner", "0.2")
    role_models_before = dict(state.role_models)
    role_temperatures_before = dict(state.role_temperatures)
    answers = iter(["new-coding", "0.7", "new-planner", "0.8"])

    def prompt(text: str, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        if "Review model" in text:
            raise Abort()
        return next(answers)

    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    config_menu_mod._run_subagent_section(state, console)

    assert state.role_models == role_models_before
    assert state.role_temperatures == role_temperatures_before
    assert 'Section "Subagent model overrides" cancelled.' in output.getvalue()
