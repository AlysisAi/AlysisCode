from __future__ import annotations

import io
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from alysis_code.cli_impl import config_menu as config_menu_mod
from alysis_code.config import load_config, save_config
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


def _patch_console(monkeypatch) -> io.StringIO:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    monkeypatch.setattr(config_menu_mod, "_resolve_console", lambda: console)
    return output


def _seed_config(
    tmp_path: Path,
    monkeypatch,
    *,
    model: str = "old",
    routing_mode: str = "auto",
    step_budget_policy: str = "adaptive",
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    cfg.model = model
    cfg.routing_mode = routing_mode
    cfg.step_budget_policy = step_budget_policy
    save_config(cfg)


def test_run_config_menu_uses_raw_top_level_loop(monkeypatch, tmp_path: Path) -> None:
    _seed_config(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    actions = iter(["subagents", "save"])
    visited: list[str] = []

    def fake_top_level(*, state: config_menu_mod.ConfigMenuState, console: Console) -> str:
        del state, console
        return next(actions)

    def fake_subagent_section(state: config_menu_mod.ConfigMenuState, console: Console) -> None:
        del console
        visited.append("subagents")
        state.set_role_model("coding", "anthropic/claude-sonnet-4-6")

    monkeypatch.setattr(config_menu_mod, "_run_config_top_level", fake_top_level)
    monkeypatch.setattr(config_menu_mod, "_run_subagent_section", fake_subagent_section)

    result = config_menu_mod.run_config_menu()

    assert result.saved is True
    assert visited == ["subagents"]
    assert load_config().extra_fields["role_models"]["coding"] == "anthropic/claude-sonnet-4-6"


def test_run_config_menu_raw_picker_sequence_persists_expected_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _seed_config(
        tmp_path,
        monkeypatch,
        routing_mode="code_only",
        step_budget_policy="fixed",
    )
    cfg = load_config()
    add_profile(cfg, ProfileSpec(name="openai", base_url="https://api.openai.com/v1"))
    add_profile(cfg, ProfileSpec(name="anthropic", base_url="https://api.anthropic.com/v1/openai"))
    set_active_profile(cfg, "openai")
    save_config(cfg)
    _patch_console(monkeypatch)

    actions = iter(["profile", "save"])
    picker_answers = iter(
        [
            "switch",
            "anthropic",
        ]
    )
    prompt = PromptScript([])

    def fake_top_level(*, state: config_menu_mod.ConfigMenuState, console: Console) -> str:
        del state, console
        return next(actions)

    def fake_picker(**kwargs: Any) -> str | None:
        del kwargs
        return next(picker_answers)

    monkeypatch.setattr(config_menu_mod, "_run_config_top_level", fake_top_level)
    monkeypatch.setattr(config_menu_mod, "_run_config_picker", fake_picker)
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    result = config_menu_mod.run_config_menu()
    saved_cfg = load_config()

    assert result.saved is True
    assert saved_cfg.extra_fields["active_profile"] == "anthropic"
    # routing_mode is a deprecated no-op field: the menu no longer exposes or
    # rewrites it, so the seeded value survives the save untouched.
    assert saved_cfg.routing_mode == "code_only"
    assert saved_cfg.step_budget_policy == "limited"


def test_top_level_live_menu_renders_unknown_key_message(monkeypatch, tmp_path: Path) -> None:
    import prompt_toolkit.input as prompt_input

    import alysis_code.cli as cli_mod

    _seed_config(tmp_path, monkeypatch)
    _patch_console(monkeypatch)
    console = config_menu_mod._resolve_console()
    state = config_menu_mod.ConfigMenuState.from_cfg(load_config())
    rows = config_menu_mod._top_level_menu_rows(state)
    unknown_messages: list[str | None] = []
    key_batches = iter(
        [
            [SimpleNamespace(key="x", data="x")],
            [SimpleNamespace(key="escape", data="")],
        ]
    )

    class FakeInput:
        def raw_mode(self) -> Any:
            return nullcontext()

        def close(self) -> None:
            return None

    @contextmanager
    def fake_watch_terminal_resize() -> Any:
        yield lambda: False

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(cli_mod, "_terminal_too_small", lambda: False)
    monkeypatch.setattr(cli_mod, "_watch_terminal_resize", fake_watch_terminal_resize)
    monkeypatch.setattr(
        cli_mod, "_read_input_keys_with_timeout", lambda **_kwargs: next(key_batches)
    )
    monkeypatch.setattr(prompt_input, "create_input", lambda: FakeInput())

    def panel_with_unknown_key(
        selected_value: str | None,
        interactive: bool,
        unknown_key: str | None,
    ) -> Any:
        unknown_messages.append(unknown_key)
        return config_menu_mod._build_config_top_level_panel(
            state=state,
            selected_value=selected_value,
            interactive=interactive,
            unknown_key_message=unknown_key,
        )

    selected, interactive_available, reason = config_menu_mod._try_run_config_live_menu(
        console=console,
        rows=rows,
        current_value=rows[0][0],
        panel_builder=lambda selected_value, interactive: (
            config_menu_mod._build_config_top_level_panel(
                state=state,
                selected_value=selected_value,
                interactive=interactive,
            )
        ),
        unknown_key_panel_builder=panel_with_unknown_key,
        command_hotkeys={"escape": "cancel"},
    )

    assert selected == "cancel"
    assert interactive_available is True
    assert reason is None
    assert "x" in unknown_messages


def test_prompt_main_action_falls_back_to_numeric_when_terminal_is_non_interactive(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import alysis_code.cli as cli_mod

    _seed_config(tmp_path, monkeypatch)
    output = _patch_console(monkeypatch)
    prompt = PromptScript(["q"])
    state = config_menu_mod.ConfigMenuState.from_cfg(load_config())

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: True)
    monkeypatch.setattr(config_menu_mod.typer, "prompt", prompt)

    action = config_menu_mod._prompt_main_action(config_menu_mod._resolve_console(), state)

    assert action == "cancel"
    assert prompt.calls[0][0] == "Choice"
    assert (
        "Interactive picker unavailable: non-interactive terminal. Using numeric input."
        in output.getvalue()
    )
