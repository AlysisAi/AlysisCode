from __future__ import annotations

import contextlib
import io
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code import workspace_binding as workspace_binding_mod
from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl import chat as chat_facade
from alysis_code.cli_impl import setup_wizard as setup_wizard_mod
from alysis_code.cli_impl.commands import welcome as welcome_mod
from alysis_code.compaction.conversation_compactor import CompactionState
from alysis_code.config import AppConfig, ConfigError, load_config, save_config
from alysis_code.profile_presets import get_preset
from alysis_code.profiles import ProfileSpec


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_API_KEY": "",
        "OPENAI_API_KEY": "",
    }


def _subscription_cfg(*, model: str = "gpt-codex-test", effort: str = "high") -> AppConfig:
    cfg = AppConfig(model=model, llm_reasoning_effort=effort)
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model=model,
        reasoning_effort=effort,
    )
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
        "onboarded": True,
    }
    return cfg


def _patch_setup_wizard_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_wizard_mod,
        "_validate_api_key",
        lambda **_kwargs: setup_wizard_mod._ApiKeyValidationResult(status="validated"),
    )
    monkeypatch.setattr(
        setup_wizard_mod,
        "_prompt_and_check_sandbox",
        lambda _console, _cfg: setup_wizard_mod._SandboxStepResult(ready=True, status="docker"),
    )


def test_forge_llm_commands_use_subscription_readiness_gate(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def _gate(**kwargs: object) -> None:
        calls.append(dict(kwargs))
        raise cli_mod.typer.Exit(code=17)

    monkeypatch.setattr(cli_mod, "_require_active_subscription_ready", _gate)
    runner = CliRunner()
    cases = [
        (["forge", "plan"], {"model": None, "base_url": None}),
        (
            ["forge", "review", "T01", "--model", "override"],
            {"model": "override", "base_url": None},
        ),
        (
            ["forge", "swarm", "--base-url", "https://override.example/v1"],
            {"model": None, "base_url": "https://override.example/v1"},
        ),
        (
            ["forge", "exec", "T01", "--model", "override"],
            {"model": "override", "base_url": None},
        ),
    ]

    for argv, expected in cases:
        result = runner.invoke(alysis_app, argv)
        assert result.exit_code == 17, result.output
        assert calls[-1] == expected


def test_no_args_shows_home_screen(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        alysis_app,
        [],
        input="quit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert "Alysis Code Home" in result.output
    assert "Quick actions" in result.output


def test_no_args_interactive_defaults_to_chat(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_chat(**kwargs: object) -> None:
        captured.update(kwargs)

    def _unexpected_prompt(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("ALYSIS_HOME_PROMPT", raising=False)
    monkeypatch.setattr(cli_mod.typer, "prompt", _unexpected_prompt)
    monkeypatch.setattr(cli_mod, "chat", _fake_chat)
    monkeypatch.setattr(cli_mod, "_maybe_run_first_run_setup_wizard", lambda: True)
    monkeypatch.setattr(cli_mod, "_maybe_run_startup_config_menu", lambda: None)
    monkeypatch.setattr(cli_mod, "_provider_auth_ready_for_chat", lambda: False)

    cli_mod.main(type("Ctx", (), {"invoked_subcommand": None})(), require_clean_build=False)

    assert captured == {
        "path": Path("."),
        "create_path": False,
        "allow_broad_workspace": False,
        "image": None,
        "mode": None,
        "model": None,
        "base_url": None,
        "temperature": None,
        "stream": None,
        "max_steps": None,
        "subagents": None,
        "no_log": False,
        "verify_cmd": None,
        "api_key_env": None,
        "api_key_stdin": False,
        "api_key": None,
        "yes": False,
        "diagnostic_log": None,
    }


def test_config_set_cannot_bypass_subscription_model_and_effort_menu(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_config(_subscription_cfg())

    result = CliRunner().invoke(
        alysis_app,
        ["config", "set", "model", "not-in-catalog"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "/config" in result.output and "Default Model" in result.output
    assert load_config().model == "gpt-codex-test"


def test_run_rejects_blank_instruction_before_agent_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def _unexpected_run_impl(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("blank run instruction reached agent execution")

    monkeypatch.setattr(chat_facade, "run_impl", _unexpected_run_impl)
    runner = CliRunner()

    for instruction in ("", "   "):
        result = runner.invoke(
            alysis_app,
            ["run", "--path", str(tmp_path / "repo"), "--create-path", instruction],
            env=_env(tmp_path),
        )

        assert result.exit_code == 1
        assert "Missing argument 'INSTRUCTION'" in result.output
        assert "instruction is empty" in result.output


def test_setup_wizard_saves_config(monkeypatch, tmp_path: Path) -> None:
    _patch_setup_wizard_dependencies(monkeypatch)
    runner = CliRunner()
    custom_choice = len(get_preset("openai-responses").suggested_models) + 1
    result = runner.invoke(
        alysis_app,
        ["setup"],
        # Keep exercising custom alias entry as the curated model list grows.
        input=f"\n1\n1\npersisted-key\n{custom_choice}\ngpt-5-nano\n1\n{tmp_path}\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    cfg_path = tmp_path / "cfg" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    # gpt-5-nano is retired (shutdown 2026-12-11); OpenAI's documented
    # replacement is gpt-5.6-luna.
    assert cfg["model"] == "gpt-5.6-luna"
    assert cfg["default_workspace_path"] == os.fspath(tmp_path.resolve())


def test_setup_wizard_can_persist_api_key(monkeypatch, tmp_path: Path) -> None:
    _patch_setup_wizard_dependencies(monkeypatch)
    runner = CliRunner()
    custom_choice = len(get_preset("openai-responses").suggested_models) + 1
    result = runner.invoke(
        alysis_app,
        ["setup"],
        input=f"\n1\n1\npersisted-key\n{custom_choice}\ngpt-5-nano\n1\n{tmp_path}\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    credentials = json.loads((tmp_path / "cfg" / "credentials.json").read_text(encoding="utf-8"))
    assert credentials["profile_keys"]["openai-responses"] == "persisted-key"

    show = runner.invoke(alysis_app, ["config", "show"], env=_env(tmp_path))
    assert show.exit_code == 0
    payload = json.loads(show.output)
    assert payload["api_key_set"] is True
    assert payload["api_key_source"] == "stored:profile=openai-responses"


def test_config_commands_can_set_and_clear_persisted_api_key(tmp_path: Path) -> None:
    runner = CliRunner()
    set_result = runner.invoke(
        alysis_app,
        ["config", "set-api-key"],
        input="persisted-key\n",
        env=_env(tmp_path),
    )
    assert set_result.exit_code == 0
    credentials_path = tmp_path / "cfg" / "credentials.json"
    assert credentials_path.exists()

    clear_result = runner.invoke(
        alysis_app,
        ["config", "clear-api-key"],
        env=_env(tmp_path),
    )
    assert clear_result.exit_code == 0
    assert credentials_path.exists() is False


def test_chat_bottom_toolbar_contains_status_fields(monkeypatch) -> None:
    class _DummyClient:
        model = "gpt-test"
        temperature = 1.0

    class _DummySession:
        client = _DummyClient()
        cfg = AppConfig(
            model="gpt-test",
            toolbar_items=["mode", "model", "images", "temp", "ctx", "subagents", "tokens", "cost"],
        )
        stream = True
        mode = "review"
        subagents_enabled = False
        root = Path("/tmp/demo-repo")
        _toolbar_default_temperature = 0.2
        _usage_hud_enabled = True
        _hud_context_cache = type("Ctx", (), {"percent_left": 87.5})()

        class _Summary:
            @staticmethod
            def totals() -> dict[str, object]:
                return {
                    "total_tokens": 42,
                    "cost_usd": 0.0123,
                    "known_cost_calls": 1,
                    "unknown_cost_calls": 0,
                }

        usage_summary = _Summary()

    monkeypatch.setattr(cli_mod, "_current_branch_label", lambda _root: "main")
    toolbar = cli_mod._chat_bottom_toolbar(session=_DummySession(), pending_images=["a.png"])
    assert "review" in toolbar
    assert "gpt-test" in toolbar
    assert "stream=on" not in toolbar
    assert "trace=compact" not in toolbar
    assert "1 image" in toolbar
    assert "temp 1.0" in toolbar
    assert "context left: 87.5%" in toolbar
    assert "subagents off" in toolbar
    assert "42 tok" in toolbar


def test_guarded_workspace_prompt_text_shows_escape_hint(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _PromptSessionStub:
        def __init__(self, **kwargs: object) -> None:
            captured["key_bindings"] = kwargs.get("key_bindings")

        def prompt(self, text: str, **kwargs: object) -> str:
            captured["text"] = text
            captured["default"] = kwargs.get("default")
            return "greenfield"

    import prompt_toolkit  # type: ignore[import-not-found]

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(prompt_toolkit, "PromptSession", _PromptSessionStub)

    result = cli_mod._guarded_workspace_prompt_text("New folder name", default="new-project")

    assert result == "greenfield"
    assert captured["text"] == "New folder name (Esc to go back): "
    assert captured["default"] == "new-project"
    assert captured["key_bindings"] is not None


def test_chat_status_surfaces_web_search_runtime_details(monkeypatch) -> None:
    class _DummyClient:
        model = "gpt-test"
        temperature = 0.4
        api_key = "main-key"

    class _DummyStore:
        session_id = "sess_123"

    class _DummySession:
        client = _DummyClient()
        store = _DummyStore()
        cfg = AppConfig(
            model="gpt-test",
            base_url="https://api.openai.com/v1",
            web_search_mode="on",
            web_search_model="gpt-4.1-mini",
        )
        stream = False
        mode = "auto"
        subagents_enabled = False
        root = Path("/tmp/demo-repo")
        model_registry = None

        @staticmethod
        def pending_operation_labels() -> list[str]:
            return ["mode: fast", "persona: ask"]

    monkeypatch.setattr(cli_mod, "_current_branch_label", lambda _root: "main")
    monkeypatch.setattr(cli_mod, "_is_git_dirty", lambda _root: False)
    monkeypatch.setattr(cli_mod, "_chat_trace_level", lambda _session: "compact")

    console = Console(record=True, width=140)
    cli_mod._print_chat_status(console=console, session=_DummySession(), pending_images=[])
    rendered = console.export_text()

    assert "execution_policy" in rendered
    assert "autonomous" in rendered
    assert "chat_step_limit" in rendered
    assert "task_step_limit" in rendered
    assert "subagent_step_limit" in rendered
    assert "active_step_limit" in rendered
    assert rendered.count("unlimited") >= 4
    assert "web_search" in rendered
    assert "available" in rendered
    assert "web_search_mode" in rendered
    assert "on" in rendered
    assert "web_search_registration" in rendered
    assert "web_search_base_url" in rendered
    assert "https://api.openai.com/v1" in rendered
    assert "web_search_model" in rendered
    assert "gpt-4.1-mini" in rendered
    assert "web_search_api_key" in rendered
    assert "available" in rendered
    assert "web_search_setup" in rendered
    assert "Native OpenAI Responses web search is ready" in rendered
    assert "pending_1" in rendered
    assert "mode: fast" in rendered
    assert "pending_2" in rendered
    assert "persona: ask" in rendered

    spec = cli_mod._chat_status_panel_spec(session=_DummySession(), pending_images=[])
    pending_rows = next(rows for title, rows in spec["sections"] if title == "Pending operations")
    assert pending_rows == [
        ("1", "mode: fast", "warn"),
        ("2", "persona: ask", "warn"),
    ]


def test_chat_bottom_toolbar_hides_default_optional_fields(monkeypatch) -> None:
    class _DummyClient:
        model = "gpt-test"
        temperature = 0.2

    class _DummySession:
        client = _DummyClient()
        stream = True
        mode = "auto"
        subagents_enabled = True
        root = Path("/tmp/demo-repo")
        _toolbar_default_temperature = 0.2
        _usage_hud_enabled = False
        _hud_context_cache = type("Ctx", (), {"percent_left": 91.0})()

    monkeypatch.setattr(cli_mod, "_current_branch_label", lambda _root: "main")
    toolbar = cli_mod._chat_bottom_toolbar(
        session=_DummySession(),
        pending_images=[],
        forge_state=cli_mod._ForgeChatState(),
    )
    assert "auto" in toolbar
    assert "gpt-test" in toolbar
    assert "context left: 91.0%" in toolbar
    assert "subagents on" in toolbar
    assert "plan off" not in toolbar
    assert "no-stream" not in toolbar
    assert "trace compact" not in toolbar
    assert "0 image" not in toolbar
    assert "temp 0.2" not in toolbar


def test_chat_bottom_toolbar_shows_non_default_optional_fields(monkeypatch) -> None:
    class _DummyClient:
        model = "gpt-test"
        temperature = 0.7

    class _DummySession:
        client = _DummyClient()
        cfg = AppConfig(
            model="gpt-test",
            toolbar_items=["mode", "model", "stream", "trace", "temp", "ctx", "subagents"],
        )
        stream = False
        mode = "auto"
        subagents_enabled = False
        root = Path("/tmp/demo-repo")
        _toolbar_default_temperature = 0.2
        _usage_hud_enabled = False
        _hud_context_cache = type("Ctx", (), {"percent_left": 75.0})()

    monkeypatch.setattr(cli_mod, "_current_branch_label", lambda _root: "main")
    monkeypatch.setattr(cli_mod, "_chat_trace_level", lambda _session: "full")
    toolbar = cli_mod._chat_bottom_toolbar(
        session=_DummySession(),
        pending_images=[],
        forge_state=cli_mod._ForgeChatState(),
    )
    assert "no-stream" in toolbar
    assert "trace full" in toolbar
    assert "temp 0.7" in toolbar
    assert "0 image" not in toolbar
    assert "/plan" not in toolbar


def test_chat_bottom_toolbar_ignores_stale_plan_item_from_saved_config() -> None:
    class _DummyClient:
        model = "gpt-test"
        temperature = 0.2

    class _DummySession:
        client = _DummyClient()
        # "plan" was a valid toolbar item before chat plan mode was removed;
        # a saved config may still list it and must simply be ignored.
        cfg = AppConfig(model="gpt-test", toolbar_items=["mode", "plan"])
        stream = True
        mode = "review"
        subagents_enabled = False
        root = Path("/tmp/demo-repo")
        _toolbar_default_temperature = 0.2
        _usage_hud_enabled = False
        _hud_context_cache = type("Ctx", (), {"percent_left": 80.0})()

    toolbar = cli_mod._chat_bottom_toolbar(
        session=_DummySession(),
        pending_images=[],
        forge_state=cli_mod._ForgeChatState(),
    )

    assert "review" in toolbar
    assert "plan" not in toolbar
    assert "/plan" not in toolbar


def test_chat_help_panel_has_no_chat_plan_command() -> None:
    console = Console(record=True, width=140)

    console.print(cli_mod._chat_help_panel(ui_mode="chat"))
    rendered = console.export_text()

    assert "/persona" in rendered
    assert "architect" in rendered
    assert "/pwd" in rendered
    assert "/plan" not in rendered
    assert "Plan Mode" not in rendered
    assert "plan-first" not in rendered


def test_chat_bottom_toolbar_includes_forge_context(monkeypatch) -> None:
    class _DummyClient:
        model = "gpt-test"
        temperature = 1.0

    class _DummySession:
        client = _DummyClient()
        cfg = AppConfig(
            model="gpt-test", toolbar_items=["mode", "model", "ctx", "subagents", "forge"]
        )
        stream = True
        mode = "review"
        root = Path("/tmp/demo-repo")

    forge_state = cli_mod._ForgeChatState(
        ui_mode="forge",
        paths=type("RunPaths", (), {"run_id": "run123"})(),  # type: ignore[arg-type]
        plan={"tasks": [{"id": "T01"}]},
    )

    monkeypatch.setattr(cli_mod, "_current_branch_label", lambda _root: "main")
    toolbar = cli_mod._chat_bottom_toolbar(
        session=_DummySession(),
        pending_images=[],
        forge_state=forge_state,
    )
    assert "run123" in toolbar
    assert "1 task" in toolbar


def test_chat_bottom_toolbar_shows_help_only_when_all_items_disabled() -> None:
    class _DummyClient:
        model = "gpt-test"
        temperature = 0.2

    class _DummySession:
        client = _DummyClient()
        cfg = AppConfig(model="gpt-test", toolbar_items=[])
        stream = True
        mode = "review"
        subagents_enabled = True
        _toolbar_default_temperature = 0.2
        _usage_hud_enabled = False
        _hud_context_cache = type("Ctx", (), {"percent_left": 88.0})()

    toolbar = cli_mod._chat_bottom_toolbar(session=_DummySession(), pending_images=[])
    assert toolbar == " /help "


def test_chat_prompt_label_is_aesthetic_and_consistent() -> None:
    assert cli_mod._chat_prompt_label() == "> "
    assert cli_mod._chat_prompt_label(mode="safe") == "> "
    assert cli_mod._chat_prompt_label(ui_mode="forge") == "forge > "
    assert cli_mod._chat_prompt_fallback_label() == ">"
    assert cli_mod._chat_prompt_fallback_label(mode="safe") == ">"
    assert cli_mod._chat_prompt_fallback_label(ui_mode="forge", mode="safe") == "forge"


def test_chat_prompt_label_formatted_ignores_mode_tag_for_chat() -> None:
    try:
        from prompt_toolkit.formatted_text import to_formatted_text
    except Exception:
        assert cli_mod._chat_prompt_label_formatted(mode="safe") == "> "
        return

    formatted = to_formatted_text(cli_mod._chat_prompt_label_formatted(mode="safe"))
    text = "".join(str(fragment[1]) for fragment in formatted)
    assert "[safe]" not in text
    assert ">" in text


def test_chat_prompt_label_formatted_uses_pointer_in_forge_mode() -> None:
    try:
        from prompt_toolkit.formatted_text import to_formatted_text
    except Exception:
        assert cli_mod._chat_prompt_label_formatted(ui_mode="forge") == "forge > "
        return

    formatted = to_formatted_text(cli_mod._chat_prompt_label_formatted(ui_mode="forge"))
    text = "".join(str(fragment[1]) for fragment in formatted)
    assert "forge" in text
    assert "▸" in text
    assert "::" not in text


def test_chat_help_panel_uses_compact_text_on_narrow_terminal(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "_is_narrow_terminal", lambda: True)
    panel = cli_mod._chat_help_panel()
    assert isinstance(panel.renderable, str)
    assert "[bold]Getting Started[/bold]" in panel.renderable
    assert "[bold]Execution[/bold]" in panel.renderable
    assert "[bold]Context[/bold]" in panel.renderable
    assert "[bold]Tools & Subagents[/bold]" in panel.renderable
    assert "[bold]Configuration[/bold]" in panel.renderable
    assert "/help  commands & config" in panel.renderable
    assert (
        "/image [path]  attach an image or file (drag & drop, or Ctrl+V to paste)"
        in panel.renderable
    )
    assert "/toolbar  customize toolbar items" in panel.renderable
    assert "/report [text]  create feedback bundle + issue draft" in panel.renderable
    assert "/feedback [text]  alias for /report" in panel.renderable
    assert "/keys" not in panel.renderable
    assert "/tour" not in panel.renderable
    assert "/examples" not in panel.renderable
    assert "/usage-hud" not in panel.renderable
    assert "/usage  token count & cost; /usage hud on|off toggles HUD" in panel.renderable
    assert "/subagents  open an active subagent run" in panel.renderable
    assert "/assets" not in panel.renderable
    assert "/skills" not in panel.renderable
    assert "/context  context window left" in panel.renderable
    assert "/ctx" not in panel.renderable
    assert (
        "/clear  wipe conversation (keeps session id + log; Ctrl+L clears terminal)"
        in panel.renderable
    )


def test_suggest_chat_command_respects_forge_visibility() -> None:
    assert cli_mod._suggest_chat_command("/goa") is None
    assert cli_mod._suggest_chat_command("/goa", ui_mode="forge") == "/goal"
    assert cli_mod._suggest_chat_command("/tour") is None


def test_chat_visible_command_lists_match_curated_surface() -> None:
    assert cli_mod._CHAT_GLOBAL_VISIBLE_COMMANDS == [
        "/help",
        "/login",
        "/permissions",
        "/persona",
        "/ask",
        "/status",
        "/subagents",
        "/terminals",
        "/pwd",
        "/usage",
        "/context",
        "/compact",
        "/clear",
        "/resume",
        "/stream",
        "/trace",
        "/config",
        "/toolbar",
        "/assets",
        "/image",
        "/forge",
        "/report",
        "/feedback",
        "/skill",
        "/exit",
    ]
    assert cli_mod._chat_visible_commands() == [
        command for command in cli_mod._CHAT_GLOBAL_VISIBLE_COMMANDS if command != "/assets"
    ]
    assert cli_mod._chat_completer_commands() == cli_mod._ordered_unique_strings(
        [command for command in cli_mod._CHAT_GLOBAL_VISIBLE_COMMANDS if command != "/assets"]
        + [
            "/forge resume",
            "/usage hud",
            "/usage hud on",
            "/usage hud off",
            "/usage hud status",
            "/terminals list",
            "/terminals show",
            "/terminals kill",
            "/terminals help",
        ]
    )
    assert cli_mod._FORGE_COMPLETER_COMMANDS == cli_mod._ordered_unique_strings(
        cli_mod._FORGE_SHARED_CHAT_COMMANDS
        + [
            "/usage hud",
            "/usage hud on",
            "/usage hud off",
            "/usage hud status",
            "/terminals list",
            "/terminals show",
            "/terminals kill",
            "/terminals help",
            "/assets",
            "/execute plan",
            "/goal",
            "/task",
            "/show",
            "/done",
            "/back",
            "/plan tasks",
            "/plan markdown",
            "/plan md",
            "/plan edit",
        ]
    )


def test_removed_subagent_command_is_absent_from_chat_registries() -> None:
    assert "/subagent" not in cli_mod._CHAT_GLOBAL_VISIBLE_COMMANDS
    assert "/subagent" not in cli_mod._CHAT_COMMANDS
    assert not any(
        command.startswith("/subagent ") for command in cli_mod._chat_completer_commands()
    )
    suggestion = cli_mod._suggest_chat_command("/subagen")
    assert suggestion != "/subagent"
    assert suggestion is None or suggestion in cli_mod._CHAT_COMMANDS
    stream = io.StringIO()
    Console(file=stream, force_terminal=False).print(cli_mod._chat_help_panel())
    assert re.search(r"/subagent(?:\s|$)", stream.getvalue()) is None


def test_plural_subagents_command_is_in_every_chat_registry() -> None:
    assert "/subagents" in cli_mod._CHAT_GLOBAL_VISIBLE_COMMANDS
    assert "/subagents" in cli_mod._CHAT_COMMANDS
    assert "/subagents" in cli_mod._chat_completer_commands()
    stream = io.StringIO()
    Console(file=stream, force_terminal=False).print(cli_mod._chat_help_panel())
    assert "/subagents" in stream.getvalue()


def test_chat_config_menu_reload_uses_injected_helpers_without_login_shadowing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from alysis_code.cli_impl import config_menu as config_menu_mod
    from alysis_code.cli_impl.chat import loop as chat_loop_mod

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(
        config_menu_mod,
        "run_config_menu",
        lambda: SimpleNamespace(saved=True, changes=["model"], api_key_changed=False),
    )

    reloaded: list[AppConfig] = []

    def fake_apply_config_menu_changes_to_session(*, session: Any, cfg: AppConfig) -> None:
        reloaded.append(cfg)
        session.cfg = cfg

    monkeypatch.setattr(cli_mod, "load_config", lambda: AppConfig(model="menu-model"))
    monkeypatch.setattr(
        chat_loop_mod,
        "_apply_config_menu_changes_to_session",
        fake_apply_config_menu_changes_to_session,
    )
    session = SimpleNamespace(cfg=AppConfig(model="old"), client=SimpleNamespace(model="old"))
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/config",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert [cfg.model for cfg in reloaded] == ["menu-model"]
    rendered = stream.getvalue()
    assert "Saved 1 change" in rendered
    assert "session reload failed" not in rendered


def test_chat_stream_command_toggles_session_and_reports_status(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model")
    events: list[tuple[str, dict[str, object]]] = []
    session = SimpleNamespace(
        cfg=cfg,
        stream=True,
        store=SimpleNamespace(append=lambda kind, payload: events.append((kind, payload))),
    )
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False)

    result = cli_mod._handle_chat_command(
        input_text="/stream off",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=cli_mod._ForgeChatState(),
    )
    status_result = cli_mod._handle_chat_command(
        input_text="/stream status",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert status_result == "handled"
    assert session.stream is False
    assert cfg.stream is False
    assert events == [
        (
            "chat_local_command",
            {"command": "/stream", "has_argument": True},
        ),
        (
            "session_setting_changed",
            {"setting": "stream", "value": False},
        ),
        (
            "chat_local_command",
            {"command": "/stream", "has_argument": True},
        ),
    ]
    assert "Streaming set for this session: off" in stream.getvalue()
    assert "Streaming is off" in stream.getvalue()


def test_chat_trace_setting_records_only_actual_changes() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class _Surface:
        trace_level = "compact"

        def set_trace_level(self, level: str) -> str:
            self.trace_level = level
            return level

    session = SimpleNamespace(
        surface=_Surface(),
        store=SimpleNamespace(append=lambda kind, payload: events.append((kind, payload))),
    )

    assert cli_mod._set_chat_trace_level(session=session, level="full") == "full"
    assert cli_mod._set_chat_trace_level(session=session, level="full") == "full"
    assert events == [
        (
            "session_setting_changed",
            {"setting": "trace_level", "value": "full"},
        )
    ]


def test_chat_config_menu_closes_session_when_connection_protocol_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from alysis_code.cli_impl import config_menu as config_menu_mod
    from alysis_code.cli_impl.chat import loop as chat_loop_mod

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(
        config_menu_mod,
        "run_config_menu",
        lambda: SimpleNamespace(saved=True, changes=["active_profile"], api_key_changed=False),
    )
    monkeypatch.setattr(cli_mod, "load_config", lambda: _subscription_cfg())
    applied: list[bool] = []
    monkeypatch.setattr(
        chat_loop_mod,
        "_apply_config_menu_changes_to_session",
        lambda **_kwargs: applied.append(True),
    )
    session = SimpleNamespace(cfg=AppConfig(model="old"), client=SimpleNamespace(model="old"))
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/config",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "exit"
    assert applied == []
    assert "session is closing" in stream.getvalue()


def test_chat_login_closes_session_when_connection_protocol_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from alysis_code import config as config_mod
    from alysis_code.cli_impl.chat import loop as chat_loop_mod
    from alysis_code.cli_impl.commands import auth as auth_mod

    monkeypatch.setattr(
        auth_mod,
        "login_connection_interactively",
        lambda _selected, *, console: None,
    )
    monkeypatch.setattr(config_mod, "load_config", lambda: _subscription_cfg())
    applied: list[bool] = []
    monkeypatch.setattr(
        chat_loop_mod,
        "_apply_config_menu_changes_to_session",
        lambda **_kwargs: applied.append(True),
    )
    session = SimpleNamespace(cfg=AppConfig(model="old"), client=SimpleNamespace(model="old"))
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/login openai-codex",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "exit"
    assert applied == []
    assert "session is closing" in stream.getvalue()


def test_chat_config_menu_closes_subscription_session_when_model_pair_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from alysis_code.cli_impl import config_menu as config_menu_mod
    from alysis_code.cli_impl.chat import loop as chat_loop_mod

    previous = _subscription_cfg(model="gpt-codex-old", effort="high")
    reloaded = _subscription_cfg(model="gpt-codex-new", effort="xhigh")
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(
        config_menu_mod,
        "run_config_menu",
        lambda: SimpleNamespace(
            saved=True,
            changes=["model", "llm_reasoning_effort"],
            api_key_changed=False,
        ),
    )
    monkeypatch.setattr(cli_mod, "load_config", lambda: reloaded)
    applied: list[bool] = []
    monkeypatch.setattr(
        chat_loop_mod,
        "_apply_config_menu_changes_to_session",
        lambda **_kwargs: applied.append(True),
    )
    session = SimpleNamespace(
        cfg=previous,
        client=SimpleNamespace(model="gpt-codex-old"),
    )
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/config",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "exit"
    assert applied == []
    assert "session is closing" in stream.getvalue()


def test_chat_config_set_model_reports_success_after_reload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from alysis_code.cli_impl.chat import loop as chat_loop_mod
    from alysis_code.config import load_config

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    reloaded: list[AppConfig] = []

    def fake_apply_config_menu_changes_to_session(*, session: Any, cfg: AppConfig) -> None:
        reloaded.append(cfg)
        session.cfg = cfg

    monkeypatch.setattr(
        chat_loop_mod,
        "_apply_config_menu_changes_to_session",
        fake_apply_config_menu_changes_to_session,
    )
    session = SimpleNamespace(cfg=AppConfig(model="old"), client=SimpleNamespace(model="old"))
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/config set model new-model",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert load_config().model == "new-model"
    assert [cfg.model for cfg in reloaded] == ["new-model"]
    rendered = stream.getvalue()
    assert "Saved model" in rendered
    assert "Config update failed" not in rendered


def test_chat_config_set_cannot_bypass_subscription_model_picker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    cfg = _subscription_cfg()
    save_config(cfg)
    session = SimpleNamespace(cfg=cfg, client=SimpleNamespace(model=cfg.model))
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/config set model not-in-catalog",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert load_config().model == "gpt-codex-test"
    assert "Default Model" in stream.getvalue()


def test_model_command_cannot_bypass_subscription_config_selection(tmp_path: Path) -> None:
    cfg = _subscription_cfg()
    session = SimpleNamespace(cfg=cfg, client=SimpleNamespace(model=cfg.model))
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/model not-in-catalog",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert session.client.model == "gpt-codex-test"
    assert "/config" in stream.getvalue()


def test_model_command_refresh_failure_keeps_verified_model_and_route(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from alysis_code.cli_impl.chat import loop as chat_loop_mod

    cfg = AppConfig(model="verified-model")
    verified_route = SimpleNamespace(fingerprint="verified-route")
    client = SimpleNamespace(model="verified-model", route_identity=verified_route)
    session = SimpleNamespace(cfg=cfg, client=client)

    def fail_after_partial_mutation(*, session: Any, cfg: AppConfig) -> None:
        session.cfg = cfg
        session.client.model = cfg.model
        session.client.route_identity = SimpleNamespace(fingerprint="unverified-route")
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(
        chat_loop_mod,
        "_apply_config_menu_changes_to_session",
        fail_after_partial_mutation,
    )
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/model unverified-model",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert session.cfg is cfg
    assert session.cfg.model == "verified-model"
    assert session.client.model == "verified-model"
    assert session.client.route_identity is verified_route
    assert "Model was not changed" in stream.getvalue()
    assert "Model set for this session" not in stream.getvalue()


def test_model_command_without_config_refuses_unsafe_bare_client_mutation(tmp_path: Path) -> None:
    client = SimpleNamespace(model="verified-model")
    session = SimpleNamespace(client=client)
    stream = io.StringIO()

    result = cli_mod._handle_chat_command(
        input_text="/model unverified-model",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=stream, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert client.model == "verified-model"
    assert "cannot safely refresh" in stream.getvalue()


def test_explicit_chat_defers_subscription_readiness_to_tui(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gates: list[dict[str, object]] = []
    launches: list[dict[str, object]] = []

    def _gate(**kwargs: object) -> None:
        gates.append(dict(kwargs))

    monkeypatch.setattr(cli_mod, "_require_active_subscription_ready", _gate)
    monkeypatch.setattr(
        chat_facade,
        "chat_impl",
        lambda _cli, *args, **kwargs: launches.append({"args": args, "kwargs": dict(kwargs)}),
    )

    result = CliRunner().invoke(alysis_app, ["chat"], env=_env(tmp_path))

    assert result.exit_code == 0
    assert gates == [{"model": None, "base_url": None, "require_ready": False}]
    assert len(launches) == 1


def test_subscription_availability_distinguishes_login_from_model_selection(
    monkeypatch,
) -> None:
    from alysis_code import provider_auth as provider_auth_mod
    from alysis_code.cli_impl.commands import startup as startup_mod
    from alysis_code.profiles import SUBSCRIPTION_SELECTION_REQUIRED_KEY

    cfg = _subscription_cfg()
    adapter = SimpleNamespace(
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        account_status=lambda: SimpleNamespace(
            connected=False,
            verified=True,
            detail="No ChatGPT subscription account is connected.",
        ),
    )
    monkeypatch.setattr(provider_auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    disconnected = startup_mod._subscription_availability(cfg)

    assert disconnected.active is True
    assert disconnected.ready is False
    assert disconnected.provider_id == "openai-codex"
    assert disconnected.selection_required is False
    assert "/login" in disconnected.message

    cfg.extra_fields[SUBSCRIPTION_SELECTION_REQUIRED_KEY] = "openai-codex"
    adapter.account_status = lambda: SimpleNamespace(
        connected=True,
        verified=True,
        detail="Connected with ChatGPT.",
    )

    selection = startup_mod._subscription_availability(cfg)

    assert selection.ready is False
    assert selection.selection_required is True
    assert "Default Model" in selection.message


def test_disconnected_subscription_opens_tui_without_creating_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from alysis_code.cli_impl import tui as tui_pkg
    from alysis_code.cli_impl.commands import startup as startup_mod

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_config(_subscription_cfg())
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(tui_pkg, "is_tui_enabled", lambda: True)
    monkeypatch.setattr(
        startup_mod,
        "_subscription_availability",
        lambda _cfg: startup_mod._SubscriptionAvailability(
            active=True,
            ready=False,
            provider_id="openai-codex",
            message="The selected AI subscription is not connected.",
        ),
    )

    def _no_session(**_kwargs: object) -> object:
        raise AssertionError("a disconnected subscription must not build a model session")

    def _fake_tui(state: object, **kwargs: object):
        captured["state"] = state
        captured.update(kwargs)
        return "/exit", []

    monkeypatch.setattr(cli_mod, "create_session", _no_session)
    monkeypatch.setattr(tui_pkg, "run_tui", _fake_tui)

    result = CliRunner().invoke(
        alysis_app,
        ["chat", "--path", os.fspath(tmp_path)],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert captured["session_builder"] is None
    assert captured["subscription_provider_id"] == "openai-codex"
    assert captured["unavailable_message"] == ("The selected AI subscription is not connected.")
    assert captured["open_config_on_start"] is False
    assert captured["state"].connection_status == "subscription not connected"
    login_picker = captured["picker_providers"]["/login"]
    login_spec = login_picker()
    assert [row["value"] for row in login_spec["rows"]] == ["alysis", "openai-codex"]
    assert login_spec["on_select"]("openai-codex") == {"exit": ("login_connection", "openai-codex")}


def test_tui_login_runs_auth_then_relaunches_chat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from alysis_code.cli_impl import tui as tui_pkg
    from alysis_code.cli_impl.commands import auth as auth_mod
    from alysis_code.cli_impl.commands import startup as startup_mod

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_config(_subscription_cfg())
    connected: list[str] = []
    relaunched: list[dict[str, object]] = []

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(tui_pkg, "is_tui_enabled", lambda: True)
    monkeypatch.setattr(
        startup_mod,
        "_subscription_availability",
        lambda _cfg: startup_mod._SubscriptionAvailability(
            active=True,
            ready=False,
            provider_id="openai-codex",
            message="Connect first.",
        ),
    )
    monkeypatch.setattr(
        tui_pkg,
        "run_tui",
        lambda _state, **_kwargs: (("login_connection", "openai-codex"), []),
    )
    monkeypatch.setattr(
        auth_mod,
        "login_connection_interactively",
        lambda connection_id, *, console: connected.append(connection_id),
    )
    monkeypatch.setattr(
        startup_mod,
        "_run_default_chat_action",
        lambda **kwargs: relaunched.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        cli_mod,
        "create_session",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected session build")),
    )

    result = CliRunner().invoke(
        alysis_app,
        ["chat", "--path", os.fspath(tmp_path)],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert connected == ["openai-codex"]
    assert len(relaunched) == 1
    assert relaunched[0]["path"] == tmp_path.resolve()
    assert relaunched[0]["model"] is None
    assert relaunched[0]["base_url"] is None


def test_tui_routing_mode_change_applies_in_place_without_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Router-free path: routing_mode is a deprecated no-op, so a routing-only
    # /config save reloads live instead of restarting the TUI session.
    from prompt_toolkit.application import current as prompt_toolkit_current

    from alysis_code.cli_impl import tui as tui_pkg
    from alysis_code.cli_impl.chat import loop as chat_loop_mod
    from alysis_code.cli_impl.commands import startup as startup_mod
    from alysis_code.cli_impl.tui.app import ConfigReloadOutcome

    relaunched: list[dict[str, object]] = []
    exits: list[tuple[str, str]] = []
    surface_events: list[tuple[str, dict[str, object]]] = []
    base_url = "https://router-restart.example/v1"

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_config(
        AppConfig(
            model="persisted-model",
            base_url="https://persisted.example/v1",
            routing_mode="auto",
        )
    )
    monkeypatch.delenv("ALYSIS_ROUTING_MODE", raising=False)
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(tui_pkg, "is_tui_enabled", lambda: True)

    def _create_session(**kwargs: object) -> SimpleNamespace:
        cfg = kwargs["cfg"]
        assert isinstance(cfg, AppConfig)
        return SimpleNamespace(
            cfg=cfg.model_copy(deep=True),
            root=kwargs["root"],
            mode=kwargs["mode"],
            routing_mode=cfg.routing_mode,
            router_client=object(),
            store=SimpleNamespace(
                append=lambda event_type, payload: surface_events.append((event_type, payload))
            ),
            close=lambda: None,
        )

    applied: list[object] = []
    monkeypatch.setattr(
        chat_loop_mod,
        "_apply_config_menu_changes_to_session",
        lambda *, session, cfg: applied.append(cfg),
    )

    def _run_tui(_state: object, **kwargs: object) -> tuple[tuple[str, str], list[object]]:
        session_builder = kwargs["session_builder"]
        assert callable(session_builder)
        session_builder(SimpleNamespace())

        updated = load_config()
        updated.routing_mode = "code_only"
        save_config(updated)

        on_config_saved = kwargs["on_config_saved"]
        assert callable(on_config_saved)
        assert on_config_saved() is ConfigReloadOutcome.APPLIED
        assert exits == []
        return ("quit", ""), []

    monkeypatch.setattr(
        prompt_toolkit_current,
        "get_app",
        lambda: SimpleNamespace(exit=lambda *, result: exits.append(result)),
    )
    monkeypatch.setattr(
        tui_pkg,
        "run_tui",
        _run_tui,
    )
    monkeypatch.setattr(
        startup_mod,
        "_run_default_chat_action",
        lambda **kwargs: relaunched.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        cli_mod,
        "create_session",
        _create_session,
    )

    result = CliRunner().invoke(
        alysis_app,
        [
            "chat",
            "--path",
            os.fspath(tmp_path),
            "--model",
            "cli-model",
            "--base-url",
            base_url,
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert relaunched == []
    assert len(applied) == 1
    assert getattr(applied[0], "routing_mode", "") == "code_only"
    assert getattr(applied[0], "model", "") == "cli-model"
    assert getattr(applied[0], "base_url", "") == base_url
    assert surface_events == [
        ("chat_surface_started", {"surface": "tui"}),
        ("chat_surface_finished", {"surface": "tui", "status": "completed"}),
    ]


def test_one_shot_run_checks_subscription_readiness_before_launch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_config(_subscription_cfg())
    monkeypatch.setattr(cli_mod, "_provider_auth_ready_for_chat", lambda: False)

    result = CliRunner().invoke(
        alysis_app,
        ["run", "inspect"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1


def test_explicit_subscription_model_override_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_config(_subscription_cfg())

    result = CliRunner().invoke(
        alysis_app,
        ["chat", "--model", "outside-config"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "managed in `/config`" in result.output


def test_chat_command_sections_describe_forge_workspace_safe_resume_contract() -> None:
    sections = cli_mod._chat_command_sections(ui_mode="chat")
    getting_started_rows = dict(sections[0][1])
    tools_rows = dict(sections[3][1])

    assert getting_started_rows["/usage"] == "token count & cost; /usage hud on|off toggles HUD"
    assert (
        dict(sections[2][1])["/clear"]
        == "wipe conversation (keeps session id + log; Ctrl+L clears terminal)"
    )
    assert "/subagent" not in tools_rows
    assert tools_rows["/skill"] == "no args lists; <name> shows info; <name> <task> attaches"
    forge_tools_rows = dict(cli_mod._chat_command_sections(ui_mode="forge")[3][1])
    assert forge_tools_rows["/skill"] == "no args lists; <name> shows info; <name> <task> attaches"
    assert "/forge [resume]" in tools_rows
    assert "fresh run by default" in tools_rows["/forge [resume]"]
    assert "same-workspace re-entry resumes session-local state" in tools_rows["/forge [resume]"]
    assert "tracks the current focus" in tools_rows["/forge [resume]"]
    assert "current pointer" in tools_rows["/forge [resume]"]

    footer_lines = cli_mod._forge_help_footer_lines()
    assert any("only in the same workspace" in line for line in footer_lines)
    assert any("tracks the chat's current focus" in line for line in footer_lines)
    assert any("Changing workspaces starts a fresh run" in line for line in footer_lines)


def test_chat_command_registry_keeps_hidden_commands_known() -> None:
    hidden_commands = {
        "/cd",
        "/ctx",
        "/model-info",
        "/model",
        "/paste-image",
        "/images",
        "/clear-images",
    }
    assert hidden_commands.issubset(set(cli_mod._CHAT_COMMANDS))
    # Chat plan mode is gone: none of its spellings may be advertised as a
    # chat command anywhere. /plan itself is Forge-only.
    retired_plan_commands = {
        "/plan <task>",
        "/plan draft",
        "/plan mode",
        "/plan readonly",
        "/plan on",
        "/plan approve",
        "/plan off",
        "/plan status",
    }
    assert retired_plan_commands.isdisjoint(set(cli_mod._CHAT_COMMANDS))
    assert retired_plan_commands.isdisjoint(set(cli_mod._chat_completer_commands()))
    assert "/plan" not in cli_mod._CHAT_GLOBAL_VISIBLE_COMMANDS
    assert "/plan" not in cli_mod._chat_completer_commands()
    assert "/plan markdown" in cli_mod._chat_completer_commands(ui_mode="forge")


def test_clear_command_wipes_conversation_but_preserves_session_identity(monkeypatch) -> None:
    refresh_calls: list[Any] = []

    def _fake_refresh_chat_hud_context_cache(session: Any) -> None:
        refresh_calls.append(session)

    monkeypatch.setattr(
        cli_mod, "_refresh_chat_hud_context_cache", _fake_refresh_chat_hud_context_cache
    )

    class _Store:
        def __init__(self) -> None:
            self.session_id = "sess_clear_123"
            self.events: list[tuple[str, dict[str, Any]]] = []

        def append(self, event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, dict(payload)))

    startup_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Repo summary"},
        {
            "role": "user",
            "content": "<workspace_binding_context>\nactive_workdir_relpath: .\n</workspace_binding_context>\n",
        },
        {
            "role": "user",
            "content": "<environment_context>\nmode: review\n</environment_context>\n",
        },
    ]
    session = type("Session", (), {})()
    session.messages = [
        *startup_messages,
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    session.startup_messages = startup_messages
    session.pinned_prefix_len = len(startup_messages)
    session.mode = "review"
    session.yes = False
    session.non_interactive = False
    session.one_shot_execution = False
    session.verification_enabled = True
    session.deny_write_prefixes = []
    session.allow_write_globs = None
    session.effective_verification_commands = []
    session.authoritative_verification_commands = None
    session.verification_selection_source = ""
    session.verification_selection_reason = ""
    session.verification_contract_type = ""
    session.verification_authoritative = False
    session.root = Path("/tmp/demo")
    session.focus_dir = Path("/tmp/demo")
    session.focus_relpath = "."
    session.workspace_kind = "plain_dir"
    session.binding_requested_path = None
    session.binding_source = None
    session.binding_risk_level = None
    session.binding_created_path = None
    session.active_workdir_relpath = "."
    session.store = _Store()
    session.request_context_measurement = object()
    session.conversation_compactor = type(
        "Compactor",
        (),
        {
            "state": CompactionState(
                summary={"topic": "retain-no-history"},
                history_chunk_index=9,
                memory_message_index=4,
                pinned_prefix_len=len(startup_messages),
                pins=[{"title": "keep me? no"}],
                pins_message_index=5,
            )
        },
    )()

    pending_images = ["queued.png"]
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False)

    result = cli_mod._handle_chat_command(
        input_text="/clear",
        root=Path("/tmp/demo"),
        session=session,
        pending_images=pending_images,
        console=console,
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert session.store.session_id == "sess_clear_123"
    assert len(session.messages) == len(startup_messages)
    assert session.messages[:2] == startup_messages[:2]
    assert session.messages[2]["content"].startswith("<workspace_binding_context>")
    assert session.messages[3]["content"].startswith("<environment_context>")
    assert session.conversation_compactor.state.summary == {}
    assert session.conversation_compactor.state.history_chunk_index == 9
    assert session.conversation_compactor.state.memory_message_index is None
    assert session.conversation_compactor.state.pins == []
    assert session.conversation_compactor.state.pins_message_index is None
    assert session.conversation_compactor.state.pinned_prefix_len == len(startup_messages)
    assert session.request_context_measurement is None
    assert pending_images == []
    assert refresh_calls == [session]
    assert session.store.events == [
        (
            "chat_local_command",
            {"command": "/clear", "has_argument": False},
        ),
        ("conversation_cleared", {"trigger": "user_command"}),
    ]
    assert stream.getvalue().strip().endswith("Conversation cleared.")


def test_local_command_audit_is_normalized_and_does_not_capture_argument_text() -> None:
    class _Store:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def append(self, event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, dict(payload)))

    session = type("Session", (), {"store": _Store()})()
    console = Console(file=io.StringIO(), force_terminal=False)
    dispatch_kwargs = {
        "root": Path("/tmp/demo"),
        "session": session,
        "pending_images": [],
        "console": console,
        "forge_state": cli_mod._ForgeChatState(),
    }

    assert (
        cli_mod._handle_chat_command(
            input_text="/No-Such-Cmd sensitive argument text",
            **dispatch_kwargs,
        )
        == "handled"
    )
    assert (
        cli_mod._handle_chat_command(
            input_text="ordinary prompt with private text",
            **dispatch_kwargs,
        )
        == "send"
    )
    assert (
        cli_mod._handle_chat_command(
            input_text="EXIT ignored-argument",
            **dispatch_kwargs,
        )
        == "exit"
    )

    assert session.store.events == [
        (
            "chat_local_command",
            {"command": "/no-such-cmd", "has_argument": True},
        ),
        (
            "chat_local_command",
            {"command": "/exit", "has_argument": True},
        ),
    ]


def test_print_welcome_banner_is_boxless_with_context(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((120, 20)),
    )
    banner = cli_mod.printWelcome(
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )
    lines = banner.splitlines()
    assert lines
    assert banner.startswith("\n")
    assert banner.endswith("\n")
    assert "alysis chat" not in banner
    assert "I'll build it" not in banner

    plain_lines = [cli_mod.stripAnsi(line) for line in lines]
    plain = "\n".join(plain_lines)

    assert "╭" not in plain
    assert "╰" not in plain
    assert "│" not in plain
    assert " by " not in plain
    assert "Alysis Code  ·  AlysisAI" in plain
    assert "The autonomous coding agent" in plain
    assert "workspace ~/myproject   model opus-4.7   version 0.8.2" in plain
    assert "/forge     begin an autonomous run" in plain
    assert "/status    view run state and usage" in plain
    assert "/help      show all commands" in plain
    assert not any("◇  ◇" in line for line in plain_lines)
    assert any("█" in line for line in plain_lines)


def test_print_welcome_text_palette_adapts_to_terminal_theme(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((120, 20)),
    )
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("OWL_THEME", raising=False)
    monkeypatch.delenv("ALYSIS_THEME", raising=False)

    monkeypatch.setenv("COLORFGBG", "0;15")
    light_banner = cli_mod.printWelcome(
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )

    monkeypatch.setenv("COLORFGBG", "15;0")
    dark_banner = cli_mod.printWelcome(
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )

    assert "\x1b[1;30mAlysis Code" in light_banner
    assert "\x1b[30mThe autonomous coding agent" in light_banner
    assert "\x1b[2;30m" not in light_banner
    assert "\x1b[1;97mAlysis Code" in dark_banner
    assert "\x1b[97mThe autonomous coding agent" in dark_banner
    assert "\x1b[2;37m" not in dark_banner


def test_print_welcome_uses_neutral_palette_when_theme_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((120, 20)),
    )
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(
        welcome_mod,
        "_detect_owl_theme",
        lambda _stream: "neutral",
    )

    banner = cli_mod.printWelcome(
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )

    assert "\x1b[48;5;231m" not in banner
    assert "\x1b[97mThe autonomous coding agent" not in banner
    assert "\x1b[30mThe autonomous coding agent" not in banner
    assert "\x1b[1mAlysis Code" in banner
    assert "The autonomous coding agent" in banner


def test_dark_welcome_uses_light_owl_on_white_panel(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((120, 20)),
    )
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("OWL_THEME", raising=False)
    monkeypatch.setenv("COLORFGBG", "15;0")

    banner = cli_mod.printWelcome(
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )

    assert "\x1b[48;5;231m" in banner
    assert "\x1b[38;5;16m" in banner
    assert "\x1b[38;5;252m" not in banner


def test_dark_owl_frames_reuse_light_artwork_when_uncolored() -> None:
    light_frames = welcome_mod._load_owl_logo_frames(
        stream=None,
        color_enabled=False,
        theme="light",
    )
    dark_frames = welcome_mod._load_owl_logo_frames(
        stream=None,
        color_enabled=False,
        theme="dark",
    )

    assert dark_frames == light_frames


def test_neutral_owl_frames_inherit_terminal_foreground() -> None:
    frames = welcome_mod._load_owl_logo_frames(
        stream=None,
        color_enabled=True,
        theme="neutral",
    )

    output = "\n".join(line for frame in frames for line in frame)
    assert "\x1b[" not in output
    assert "█" in output


def test_dark_owl_panel_remaps_pale_grays() -> None:
    frame = [["\x1b[38;5;247m░\x1b[0m \x1b[38;5;244m▒\x1b[0m"]]

    painted = welcome_mod._paint_owl_light_panel(frame)
    output = "\n".join(line for painted_frame in painted for line in painted_frame)

    assert "\x1b[38;5;247m" not in output
    assert "\x1b[38;5;238m" in output
    assert "\x1b[38;5;236m" in output
    assert "\x1b[48;5;231m  " in output


def test_print_welcome_banner_keeps_compact_owl_beside_text_at_80_columns(monkeypatch) -> None:
    # This layout assertion uses an unframed logo, independent of host theme discovery.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((80, 20)),
    )
    banner = cli_mod.printWelcome(
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )
    plain_lines = [cli_mod.stripAnsi(line) for line in banner.splitlines()]
    plain = "\n".join(plain_lines)

    assert all(len(line) < 80 for line in plain_lines)
    assert "..." not in plain
    assert "The autonomous coding agent" in plain
    assert "workspace ~/myproject" in plain
    assert "model opus-4.7   version 0.8.2" in plain
    assert "/forge     begin an autonomous run" in plain
    assert "/status    view run state and usage" in plain
    assert "/help      show all commands" in plain
    # At 80 columns the cropped compact owl can sit beside the brand/details
    # without truncating the content or reaching the terminal edge.
    assert any("▝▜██▅▅▅▅▅▅▅██▛▘" in line for line in plain_lines)
    assert any("█" in line for line in plain_lines)
    owl_index = next(i for i, line in enumerate(plain_lines) if "█" in line)
    brand_index = next(i for i, line in enumerate(plain_lines) if "Alysis Code" in line)
    assert owl_index == brand_index


def test_print_welcome_banner_keeps_long_context_off_terminal_edge_at_80_columns(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((80, 20)),
    )
    banner = cli_mod.printWelcome(
        workspace=Path.home() / "Desktop",
        model="claude-sonnet-4-5",
        version="0.1.4",
    )
    plain_lines = [cli_mod.stripAnsi(line) for line in banner.splitlines()]
    plain = "\n".join(plain_lines)

    assert all(len(line) < 80 for line in plain_lines)
    assert "..." not in plain
    assert "workspace ~/Desktop" in plain
    assert "model claude-sonnet-4-5" in plain
    assert "version 0.1.4" in plain


def test_print_welcome_banner_keeps_wide_side_by_side_layout_untruncated(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((120, 20)),
    )
    banner = cli_mod.printWelcome(
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )
    plain_lines = [cli_mod.stripAnsi(line) for line in banner.splitlines()]
    plain = "\n".join(plain_lines)

    assert all(len(line) < 120 for line in plain_lines)
    assert "..." not in plain
    assert "workspace ~/myproject   model opus-4.7   version 0.8.2" in plain
    assert any("█" in line and "Alysis Code" in line for line in plain_lines)


def test_print_welcome_banner_narrow_mode_stacks_and_omits_context(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((50, 20)),
    )
    banner = cli_mod.printWelcome(
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )
    plain = cli_mod.stripAnsi(banner)

    assert banner.startswith("\n")
    assert banner.endswith("\n")
    assert "workspace ~/myproject" not in plain
    assert "model opus-4.7" not in plain
    assert "version 0.8.2" not in plain
    assert "─" not in plain
    assert "Alysis Code  ·  AlysisAI" in plain
    assert "/forge  /status  /help" in plain


def test_print_welcome_animates_owl_logo_once_on_tty(monkeypatch) -> None:
    class _TTYStream:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        def write(self, value: str) -> int:
            self.chunks.append(value)
            return len(value)

        def flush(self) -> None:
            return None

        def isatty(self) -> bool:
            return True

        def getvalue(self) -> str:
            return "".join(self.chunks)

    stream = _TTYStream()
    monkeypatch.setattr(cli_mod.sys, "stdout", stream)
    monkeypatch.setattr(cli_mod.sys, "stdin", stream)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("ALYSIS_NO_INTRO", raising=False)
    monkeypatch.delenv("ALYSIS_NO_OWL", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALYSIS_CI", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("COLORFGBG", "15;0")
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((80, 20)),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(cli_mod.time, "sleep", sleeps.append)

    cli_mod.printWelcome(
        console=Console(file=stream),
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )
    output = stream.getvalue()

    frame_count = len(welcome_mod._load_owl_logo_frames(stream=stream, color_enabled=True))
    assert sleeps == [0.10] * frame_count
    assert "Alysis Code" in output
    assert "◇  ◇" not in output
    assert "\x1b[" in output
    assert "\x1b[2K" in output
    animated_lines = re.findall(r"\r\x1b\[2K([^\n]*)", output)
    assert animated_lines
    assert all(cli_mod.visibleLength(line) < 80 for line in animated_lines)


def test_detect_owl_theme_reads_windows_terminal_settings_jsonc(
    tmp_path: Path, monkeypatch
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        """
        {
          // Windows Terminal settings.json is JSONC.
          "profiles": {
            "list": [
              {
                "guid": "{11111111-1111-1111-1111-111111111111}",
                "colorScheme": "Alysis Code Dark",
              },
            ],
          },
          "schemes": [
            {
              "name": "Alysis Code Dark",
              "background": "#101820",
            },
          ],
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.delenv("OWL_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setenv("OWL_FALLBACK_THEME", "light")
    monkeypatch.setenv("WT_SESSION", "test-session")
    monkeypatch.setenv("WT_PROFILE_ID", "{11111111-1111-1111-1111-111111111111}")
    monkeypatch.setenv("ALYSIS_WINDOWS_TERMINAL_SETTINGS", str(settings))

    assert welcome_mod._detect_owl_theme(stream=None) == "dark"


def test_detect_owl_theme_knows_windows_terminal_builtin_light_scheme(
    tmp_path: Path, monkeypatch
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        """
        {
          "profiles": {
            "defaults": {
              "colorScheme": "One Half Light"
            },
            "list": []
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.delenv("OWL_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setenv("OWL_FALLBACK_THEME", "dark")
    monkeypatch.setenv("WT_SESSION", "test-session")
    monkeypatch.setenv("ALYSIS_WINDOWS_TERMINAL_SETTINGS", str(settings))

    assert welcome_mod._detect_owl_theme(stream=None) == "light"


def test_detect_owl_theme_defaults_windows_terminal_to_campbell(
    tmp_path: Path, monkeypatch
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text('{"profiles":{"list":[]}}', encoding="utf-8")
    monkeypatch.delenv("OWL_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setenv("OWL_FALLBACK_THEME", "light")
    monkeypatch.setenv("WT_SESSION", "test-session")
    monkeypatch.setenv("ALYSIS_WINDOWS_TERMINAL_SETTINGS", str(settings))

    assert welcome_mod._detect_owl_theme(stream=None) == "dark"


def test_detect_owl_theme_defaults_unreadable_windows_terminal_to_dark(
    tmp_path: Path, monkeypatch
) -> None:
    missing_settings = tmp_path / "missing-settings.json"
    monkeypatch.delenv("OWL_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    monkeypatch.setenv("OWL_FALLBACK_THEME", "light")
    monkeypatch.setenv("WT_SESSION", "test-session")
    monkeypatch.setenv("ALYSIS_WINDOWS_TERMINAL_SETTINGS", str(missing_settings))

    assert welcome_mod._detect_owl_theme(stream=None) == "dark"


def test_should_animate_owl_logo_requires_interactive_stdio(monkeypatch) -> None:
    class _TTYStream:
        def isatty(self) -> bool:
            return True

    class _PipeStream:
        def isatty(self) -> bool:
            return False

    stream = _TTYStream()
    monkeypatch.setattr(cli_mod.sys, "stdout", stream)
    monkeypatch.setattr(cli_mod.sys, "stdin", stream)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ALYSIS_NO_OWL", raising=False)
    monkeypatch.delenv("ALYSIS_NO_INTRO", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALYSIS_CI", raising=False)

    assert cli_mod._should_animate_owl_logo(stream) is True
    assert cli_mod._should_animate_owl_logo(_TTYStream()) is False
    assert cli_mod._should_animate_owl_logo(_PipeStream()) is False

    monkeypatch.setenv("ALYSIS_NO_OWL", "1")
    assert cli_mod._should_animate_owl_logo(stream) is False
    monkeypatch.delenv("ALYSIS_NO_OWL", raising=False)

    monkeypatch.setenv("ALYSIS_NO_INTRO", "0")
    assert cli_mod._should_animate_owl_logo(stream) is False
    monkeypatch.delenv("ALYSIS_NO_INTRO", raising=False)

    monkeypatch.setenv("CI", "1")
    assert cli_mod._should_animate_owl_logo(stream) is False


def test_print_welcome_skips_animation_for_no_color(monkeypatch) -> None:
    class _TTYStream:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        def write(self, value: str) -> int:
            self.chunks.append(value)
            return len(value)

        def flush(self) -> None:
            return None

        def isatty(self) -> bool:
            return True

        def getvalue(self) -> str:
            return "".join(self.chunks)

    stream = _TTYStream()
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("ALYSIS_NO_INTRO", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((80, 20)),
    )
    monkeypatch.setattr(cli_mod.time, "sleep", lambda _seconds: None)

    banner = cli_mod.printWelcome(
        console=Console(file=stream),
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )
    output = stream.getvalue()

    assert "\x1b[" not in output
    assert "◆  ◆" not in output
    assert "◇  ◆" not in output
    assert output == banner + "\n"


def test_print_welcome_skips_animation_for_no_intro(monkeypatch) -> None:
    class _TTYStream:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        def write(self, value: str) -> int:
            self.chunks.append(value)
            return len(value)

        def flush(self) -> None:
            return None

        def isatty(self) -> bool:
            return True

        def getvalue(self) -> str:
            return "".join(self.chunks)

    stream = _TTYStream()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("ALYSIS_NO_INTRO", "1")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        cli_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 20): os.terminal_size((80, 20)),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(cli_mod.time, "sleep", sleeps.append)

    cli_mod.printWelcome(
        console=Console(file=stream),
        workspace=Path.home() / "myproject",
        model="anthropic/opus-4.7",
        version="0.8.2",
    )
    output = stream.getvalue()

    assert sleeps == []
    assert "◆  ◆" not in output
    assert "◇  ◆" not in output
    assert "\x1b[13A\r" not in output


def test_owl_shell_scripts_do_not_override_home_env_var() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "src/alysis_code/assets/owl/show-owl.sh"
    text = script.read_text(encoding="utf-8")
    assert 'HOME="${ESC}[H"' not in text
    assert "\nHOME=" not in text


def test_show_owl_dark_theme_uses_light_frames_with_white_panel() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "src/alysis_code/assets/owl/show-owl.sh"
    text = script.read_text(encoding="utf-8")

    assert "white_panel_file()" in text
    assert 'dark)\n    ASCII_DIR="$CANONICAL_ASCII_DIR"' in text
    assert ("ascii" + "-dark") not in text


def test_home_chat_action_forwards_plain_defaults(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_chat(**kwargs: object) -> None:
        captured.update(kwargs)

    prompts = iter(["chat"])

    def _fake_prompt(*_args: object, **_kwargs: object) -> str:
        return next(prompts)

    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("ALYSIS_HOME_PROMPT", "1")
    monkeypatch.setattr(cli_mod.typer, "prompt", _fake_prompt)
    monkeypatch.setattr(cli_mod, "chat", _fake_chat)
    monkeypatch.setattr(cli_mod, "_maybe_run_startup_config_menu", lambda: None)
    monkeypatch.setattr(cli_mod, "_provider_auth_ready_for_chat", lambda: True)
    cli_mod.main(type("Ctx", (), {"invoked_subcommand": None})(), require_clean_build=False)

    assert captured == {
        "path": Path("."),
        "create_path": False,
        "allow_broad_workspace": False,
        "image": None,
        "mode": None,
        "model": None,
        "base_url": None,
        "temperature": None,
        "stream": None,
        "max_steps": None,
        "subagents": None,
        "no_log": False,
        "verify_cmd": None,
        "api_key_env": None,
        "api_key_stdin": False,
        "api_key": None,
        "yes": False,
        "diagnostic_log": None,
    }


def test_home_run_action_forwards_plain_defaults(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run(**kwargs: object) -> None:
        captured.update(kwargs)

    prompts = iter(["run", "summarize this repo"])

    def _fake_prompt(*_args: object, **_kwargs: object) -> str:
        return next(prompts)

    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("ALYSIS_HOME_PROMPT", "1")
    monkeypatch.setattr(cli_mod.typer, "prompt", _fake_prompt)
    monkeypatch.setattr(cli_mod, "run", _fake_run)
    cli_mod.main(type("Ctx", (), {"invoked_subcommand": None})(), require_clean_build=False)

    assert captured == {
        "instruction": "summarize this repo",
        "path": Path("."),
        "create_path": False,
        "allow_broad_workspace": False,
        "image": None,
        "mode": None,
        "model": None,
        "base_url": None,
        "temperature": None,
        "stream": None,
        "max_steps": None,
        "subagents": None,
        "no_log": False,
        "verify_cmd": None,
        "api_key_env": None,
        "api_key_stdin": False,
        "api_key": None,
        "yes": False,
        "benchmark": False,
        "deadline_seconds": None,
        "require_deadline": False,
        "diagnostic_log": None,
    }


def test_home_plan_action_forwards_plain_defaults(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_plan(**kwargs: object) -> None:
        captured.update(kwargs)

    prompts = iter(["plan"])

    def _fake_prompt(*_args: object, **_kwargs: object) -> str:
        return next(prompts)

    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("ALYSIS_HOME_PROMPT", "1")
    monkeypatch.setattr(cli_mod.typer, "prompt", _fake_prompt)
    monkeypatch.setattr(cli_mod, "forge_plan", _fake_plan)
    cli_mod.main(type("Ctx", (), {"invoked_subcommand": None})(), require_clean_build=False)

    assert captured == {"path": Path(".")}


def test_forge_enter_command_detection() -> None:
    assert cli_mod._is_forge_enter_command(cmd="/forge", arg="")
    assert cli_mod._is_forge_enter_command(cmd=":forge", arg="")
    assert cli_mod._is_forge_enter_command(cmd="/forge", arg="resume")
    assert cli_mod._is_forge_enter_command(cmd=":forge", arg="resume")
    assert cli_mod._is_forge_enter_command(cmd="/forge", arg="later")
    assert not cli_mod._is_forge_enter_command(cmd="/plan", arg="")


def test_setup_wizard_can_pick_first_suggested_model(monkeypatch, tmp_path: Path) -> None:
    _patch_setup_wizard_dependencies(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        alysis_app,
        ["setup"],
        input=f"\n1\n1\npersisted-key\n1\n1\n{tmp_path}\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    cfg_path = tmp_path / "cfg" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    # Model choice "1" is the preset's first suggestion: GPT-6 Astra.
    assert cfg["model"] == "gpt-6-astra"
    assert cfg["default_workspace_path"] == os.fspath(tmp_path.resolve())


def test_setup_wizard_reprompts_invalid_workspace(monkeypatch, tmp_path: Path) -> None:
    _patch_setup_wizard_dependencies(monkeypatch)
    runner = CliRunner()
    custom_choice = len(get_preset("openai-responses").suggested_models) + 1
    result = runner.invoke(
        alysis_app,
        ["setup"],
        input=f"\n1\n1\npersisted-key\n{custom_choice}\ngpt-5-nano\n1\n/does/not/exist\n{tmp_path}\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    cfg_path = tmp_path / "cfg" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    # gpt-5-nano is retired (shutdown 2026-12-11); OpenAI's documented
    # replacement is gpt-5.6-luna.
    assert cfg["model"] == "gpt-5.6-luna"
    assert cfg["default_workspace_path"] == os.fspath(tmp_path.resolve())


def test_home_run_action_accepts_numeric_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run(**kwargs: object) -> None:
        captured.update(kwargs)

    prompts = iter(["2", "summarize this repo"])

    def _fake_prompt(*_args: object, **_kwargs: object) -> str:
        return next(prompts)

    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("ALYSIS_HOME_PROMPT", "1")
    monkeypatch.setattr(cli_mod.typer, "prompt", _fake_prompt)
    monkeypatch.setattr(cli_mod, "run", _fake_run)
    cli_mod.main(type("Ctx", (), {"invoked_subcommand": None})(), require_clean_build=False)

    assert captured == {
        "instruction": "summarize this repo",
        "path": Path("."),
        "create_path": False,
        "allow_broad_workspace": False,
        "image": None,
        "mode": None,
        "model": None,
        "base_url": None,
        "temperature": None,
        "stream": None,
        "max_steps": None,
        "subagents": None,
        "no_log": False,
        "verify_cmd": None,
        "api_key_env": None,
        "api_key_stdin": False,
        "api_key": None,
        "yes": False,
        "benchmark": False,
        "deadline_seconds": None,
        "require_deadline": False,
        "diagnostic_log": None,
    }


def test_chat_auto_binds_sane_project_dir_before_session_creation(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**kwargs: object) -> _DummySession:
        captured.update(kwargs)
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.chdir(repo)

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="exit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["root"] == repo.resolve()
    assert captured["enable_chat_turn_step_budget"] is True
    assert captured["chat_turn_fixed_override"] is None
    binding = captured["workspace_binding"]
    assert binding.workspace_context.workspace_root == repo.resolve()
    assert binding.workspace_context.focus_path == repo.resolve()


def test_chat_preserves_dash_leading_unusual_prompt_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    prompt_text = "--starts-with-dash 'quoted' $(echo nope) Ω"
    captured: dict[str, object] = {}

    class _DummyStore:
        enabled = False
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        cfg = AppConfig(model="test-model")
        mode = "review"
        subagents_enabled = False
        messages: list[dict[str, object]] = []

        def run_turn(self, instruction: str, **kwargs: object) -> int:
            captured["instruction"] = instruction
            captured["kwargs"] = kwargs
            return 0

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.chdir(repo)

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input=f"{prompt_text}\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["instruction"] == prompt_text
    assert captured["closed"] is True


def test_chat_passes_explicit_max_steps_as_chat_turn_fixed_override(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**kwargs: object) -> _DummySession:
        captured.update(kwargs)
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.chdir(repo)

    result = runner.invoke(
        alysis_app,
        [
            "chat",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--max-steps",
            "7",
        ],
        input="exit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["enable_chat_turn_step_budget"] is True
    assert captured["chat_turn_fixed_override"] == 7


def test_chat_keeps_legacy_cap_dormant_under_autonomous_policy(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "model": "test-model",
                "step_budget_policy": "adaptive",
                "max_steps": 25,
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**kwargs: object) -> _DummySession:
        captured.update(kwargs)
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.chdir(repo)

    result = runner.invoke(
        alysis_app,
        ["chat", "--api-key", "k", "--no-log"],
        input="exit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["max_steps"] == 25
    assert captured["chat_turn_fixed_override"] is None
    assert captured["cfg"].step_budget_policy == "autonomous"


def test_chat_home_path_uses_guarded_binding_flow(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    home.mkdir()
    captured: dict[str, object] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**kwargs: object) -> _DummySession:
        captured.update(kwargs)
        return _DummySession()

    def fake_resolve_startup_workspace_binding(**kwargs: object):
        captured["binding_request"] = kwargs
        return workspace_binding_mod.resolve_workspace_binding(
            home,
            allow_broad_workspace=True,
            source="explicit_path",
        )

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_resolve_startup_workspace_binding",
        fake_resolve_startup_workspace_binding,
    )

    result = runner.invoke(
        alysis_app,
        ["chat", "--path", os.fspath(home), "--model", "test-model", "--api-key", "k", "--no-log"],
        input="exit\n",
        env={**_env(tmp_path), "ALYSIS_TUI": "0"},
    )

    assert result.exit_code == 0
    binding_request = captured["binding_request"]
    assert binding_request["requested_path"] == home
    assert binding_request["source"] == "explicit_path"
    assert captured["root"] == home.resolve()


def test_chat_guarded_create_folder_flow_works_with_mocked_prompts(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    home.mkdir()
    created = home / "new-project"
    captured: dict[str, object] = {}
    captured_prompt: dict[str, object] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**kwargs: object) -> _DummySession:
        captured.update(kwargs)
        return _DummySession()

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(cli_mod, "_maybe_make_chat_prompt_session", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli_mod,
        "_select_guarded_workspace_action_interactive",
        lambda **_kwargs: ("create_folder", True),
    )
    monkeypatch.setattr(
        cli_mod,
        "_guarded_workspace_prompt_text",
        lambda text, *, default=None: (
            captured_prompt.update({"text": text, "default": default}) or "new-project"
        ),
    )
    monkeypatch.setattr(
        cli_mod.typer,
        "prompt",
        lambda text, *_args, **_kwargs: (
            (_ for _ in ()).throw(
                AssertionError("typer.prompt should not run for guarded workspace text entry")
            )
            if str(text).startswith("New folder name")
            else "exit"
        ),
    )
    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())

    result = runner.invoke(
        alysis_app,
        ["chat", "--path", os.fspath(home), "--model", "test-model", "--api-key", "k", "--no-log"],
        input="exit\n",
        env={**_env(tmp_path), "ALYSIS_TUI": "0"},
    )

    assert result.exit_code == 0
    assert created.exists()
    assert captured["root"] == created.resolve()
    binding = captured["workspace_binding"]
    assert binding.created_path is True
    assert binding.requested_path == created.resolve()
    assert captured_prompt == {"text": "New folder name", "default": "new-project"}


def test_chat_forge_from_guarded_workspace_is_blocked(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    home.mkdir()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        def close(self) -> None:
            return None

    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())
    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(
        cli_mod,
        "_prompt_forge_entry_plan_assistant",
        lambda *, console: (_ for _ in ()).throw(AssertionError("planner prompt should not run")),
    )

    result = runner.invoke(
        alysis_app,
        [
            "chat",
            "--path",
            os.fspath(home),
            "--allow-broad-workspace",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        input="/forge\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Forge requires a narrower workspace before planning." in result.output


def test_chat_welcome_prints_after_workspace_resolution_with_context(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs: object) -> _DummySession:
        return _DummySession()

    def fake_resolve_startup_workspace_binding(**kwargs: object):
        console = kwargs.get("console")
        if console is not None:
            console.print("WORKSPACE_RESOLUTION_MARKER")
        return workspace_binding_mod.resolve_workspace_binding(tmp_path, source="explicit_path")

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_resolve_startup_workspace_binding",
        fake_resolve_startup_workspace_binding,
    )

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="exit\n",
        env={**_env(tmp_path), "COLUMNS": "140"},
    )

    assert result.exit_code == 0
    welcome_index = result.output.find("Alysis Code")
    marker_index = result.output.find("WORKSPACE_RESOLUTION_MARKER")
    assert welcome_index >= 0
    assert marker_index >= 0
    assert marker_index < welcome_index
    assert f"workspace {tmp_path.name}" in result.output
    assert "model test-model" in result.output


def test_workspace_startup_pickers_disable_alt_screen(tmp_path: Path, monkeypatch) -> None:
    captured: list[tuple[bool, bool]] = []

    def fake_run_inline_option_selector(**kwargs: object):
        captured.append(
            (bool(kwargs.get("use_alt_screen", True)), bool(kwargs.get("confirm_on_digit", False)))
        )
        return None, True

    monkeypatch.setattr(cli_mod, "_run_inline_option_selector", fake_run_inline_option_selector)

    binding = workspace_binding_mod.resolve_workspace_binding(tmp_path, source="explicit_path")
    cli_mod._select_guarded_workspace_action_interactive(
        binding=binding,
        candidates=(),
        allow_use_current_action=True,
        console=cli_mod._console(),
    )

    candidate = workspace_binding_mod.WorkspaceCandidate(
        path=tmp_path,
        score=1,
        project_signals=("README",),
        source="child",
    )
    cli_mod._select_workspace_candidate_interactive(
        base_path=tmp_path,
        candidates=(candidate,),
        console=cli_mod._console(),
    )

    assert captured == [(False, True), (False, True)]


def test_inline_picker_keeps_layout_stable_while_navigating(monkeypatch) -> None:
    from prompt_toolkit.keys import Keys
    from rich.console import Group
    from rich.table import Table

    captures: list[Any] = []

    class _FakeLive:
        def __init__(self, renderable: Any, **_kwargs: object) -> None:
            captures.append(renderable)

        def __enter__(self) -> _FakeLive:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def update(self, renderable: Any, *, refresh: bool = False) -> None:
            captures.append(renderable)

    class _FakeInput:
        def raw_mode(self):
            class _RawMode:
                def __enter__(self) -> None:
                    return None

                def __exit__(self, *_args: object) -> None:
                    return None

            return _RawMode()

        def close(self) -> None:
            return None

    class _KeyPress:
        def __init__(self, key: object, data: str = "") -> None:
            self.key = key
            self.data = data

    def contains_table(renderable: Any) -> bool:
        if isinstance(renderable, Table):
            return True
        if isinstance(renderable, Group):
            return any(contains_table(child) for child in renderable.renderables)
        children = getattr(renderable, "renderables", None)
        if children is not None:
            return any(contains_table(child) for child in children)
        return False

    narrow_values = iter([True, False, False])
    key_batches = iter(
        [
            [_KeyPress(Keys.Down)],
            [_KeyPress(Keys.Enter, "\r")],
        ]
    )
    selector_globals = cli_mod._run_inline_option_selector.__globals__

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(cli_mod, "_is_narrow_terminal", lambda: next(narrow_values))
    monkeypatch.setitem(selector_globals, "_Live", _FakeLive)
    monkeypatch.setitem(selector_globals, "_terminal_too_small", lambda: False)
    monkeypatch.setitem(
        selector_globals, "_watch_terminal_resize", lambda: contextlib.nullcontext(lambda: False)
    )
    monkeypatch.setitem(
        selector_globals, "_read_input_keys_with_timeout", lambda **_kwargs: next(key_batches)
    )
    monkeypatch.setattr("prompt_toolkit.input.create_input", lambda: _FakeInput())

    rows = [
        ("one", "1) first", "First description."),
        ("two", "2) second", "Second description."),
    ]
    selected, available = cli_mod._run_inline_option_selector(
        console=cli_mod._console(),
        rows=rows,
        current_value="one",
        panel_builder=lambda selected_value, interactive: cli_mod._selectable_options_panel(
            title="Picker",
            rows=rows,
            selected_value=selected_value,
            interactive=interactive,
        ),
        unavailable_label="Picker",
        use_alt_screen=False,
    )

    assert (selected, available) == ("two", True)
    assert len(captures) == 2
    assert contains_table(captures[0]) is False
    assert contains_table(captures[1]) is False


def test_chat_prints_session_info_line_without_ready_hint(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs: object) -> _DummySession:
        return _DummySession()

    def fake_resolve_startup_workspace_binding(**_kwargs: object):
        return workspace_binding_mod.resolve_workspace_binding(tmp_path, source="explicit_path")

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_resolve_startup_workspace_binding",
        fake_resolve_startup_workspace_binding,
    )

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="exit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert tmp_path.name in result.output
    assert "test-model" in result.output
    assert "Starting chat session" in result.output
    assert "Workspace:" not in result.output
    assert "Ready. Type your message at the > prompt." not in result.output


def test_chat_retries_with_warn_shell_sandbox_when_default_strict_backend_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    seen_cfgs: list[AppConfig] = []

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**kwargs: object) -> _DummySession:
        cfg = kwargs["cfg"]
        assert isinstance(cfg, AppConfig)
        seen_cfgs.append(cfg)
        if len(seen_cfgs) == 1:
            raise ConfigError(
                "Shell sandbox strict mode is enabled, but no usable backend is available: "
                "auto backend could not find bwrap or docker. Install bubblewrap (Linux) or "
                "Docker, or set ALYSIS_SHELL_SANDBOX_MODE=off for explicit unsafe host execution."
            )
        return _DummySession()

    def fake_resolve_startup_workspace_binding(**_kwargs: object):
        return workspace_binding_mod.resolve_workspace_binding(tmp_path, source="explicit_path")

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_resolve_startup_workspace_binding",
        fake_resolve_startup_workspace_binding,
    )

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="exit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert len(seen_cfgs) == 2
    assert "Shell sandbox unavailable:" in result.output
    assert seen_cfgs[1].extra_fields["shell_sandbox"]["mode"] == "warn"


def test_chat_does_not_retry_shell_sandbox_when_strict_mode_is_explicit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    call_count = 0

    def fake_create_session(**_kwargs: object):
        nonlocal call_count
        call_count += 1
        raise ConfigError(
            "Shell sandbox strict mode is enabled, but no usable backend is available: "
            "auto backend could not find bwrap or docker. Install bubblewrap (Linux) or "
            "Docker, or set ALYSIS_SHELL_SANDBOX_MODE=off for explicit unsafe host execution."
        )

    def fake_resolve_startup_workspace_binding(**_kwargs: object):
        return workspace_binding_mod.resolve_workspace_binding(tmp_path, source="explicit_path")

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_resolve_startup_workspace_binding",
        fake_resolve_startup_workspace_binding,
    )

    env = _env(tmp_path)
    env["ALYSIS_SHELL_SANDBOX_MODE"] = "strict"
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="exit\n",
        env=env,
    )

    assert result.exit_code == 2
    assert call_count == 1
    assert "Config error:" in result.output
    assert "Shell sandbox unavailable:" not in result.output


def test_chat_fallback_prompt_uses_arrow_suffix(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()

        def close(self) -> None:
            return None

    def fake_create_session(**_kwargs: object) -> _DummySession:
        return _DummySession()

    def fake_resolve_startup_workspace_binding(**_kwargs: object):
        return workspace_binding_mod.resolve_workspace_binding(tmp_path, source="explicit_path")

    def fake_prompt(text: str, **kwargs: object) -> str:
        captured["text"] = text
        captured["kwargs"] = kwargs
        return "exit"

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(
        cli_mod,
        "_resolve_startup_workspace_binding",
        fake_resolve_startup_workspace_binding,
    )
    monkeypatch.setattr(cli_mod, "_maybe_make_chat_prompt_session", lambda **_kwargs: None)
    monkeypatch.setattr(cli_mod.typer, "prompt", fake_prompt)

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["text"] == ">"
    prompt_kwargs = captured["kwargs"]
    assert isinstance(prompt_kwargs, dict)
    assert prompt_kwargs.get("prompt_suffix") == " "


def test_chat_interactive_path_skips_model_session_summary_generation(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured_flags: list[bool] = []
    run_turn_calls: list[str] = []

    class _DummyStore:
        enabled = True
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 0.2

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        cfg = AppConfig(model="test-model", default_mode="review")
        stream = False
        mode = "review"
        subagents_enabled = False
        root = tmp_path

        @staticmethod
        def context_left():
            return None

        @staticmethod
        def run_turn(instruction: str, **_kwargs: object) -> int:
            run_turn_calls.append(instruction)
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_create_session(**_kwargs: object) -> _DummySession:
        return _DummySession()

    def fake_ensure_session_summary_metadata(
        *, session: object, allow_model_summary: bool = True
    ) -> None:
        _ = session
        captured_flags.append(allow_model_summary)

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)
    monkeypatch.setattr(cli_mod, "_maybe_make_chat_prompt_session", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli_mod,
        "_ensure_session_summary_metadata",
        fake_ensure_session_summary_metadata,
    )

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="hello\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert run_turn_calls == ["hello"]
    assert captured_flags == [False, False]
