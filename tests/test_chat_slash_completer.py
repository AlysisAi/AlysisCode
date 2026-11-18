from __future__ import annotations

from pathlib import Path

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text

from alysis_code.cli_impl.chat_slash_completer import (
    ChatSlashCompleter,
    get_chat_specs,
    max_completions,
)
from alysis_code.skills import discover_skills, resolve_skill_catalog


def _completion_names(completer: ChatSlashCompleter, text: str) -> list[str]:
    return [
        completion.text.removeprefix("/")
        for completion in completer.get_completions(Document(text=text), None)
    ]


def _display_text(value: object) -> str:
    return fragment_list_to_text(to_formatted_text(value))


def _source_between(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_get_chat_specs_match_curated_visible_surface() -> None:
    assert [spec.name for spec in get_chat_specs()] == [
        "help",
        "permissions",
        "persona",
        "ask",
        "status",
        "subagents",
        "terminals",
        "pwd",
        "usage",
        "context",
        "compact",
        "clear",
        "resume",
        "stream",
        "trace",
        "model",
        "config",
        "login",
        "logout",
        "toolbar",
        "image",
        "report",
        "feedback",
        "skill",
        "exit",
    ]


def test_max_completions_covers_static_chat_surface() -> None:
    assert max_completions() >= len(get_chat_specs())


def test_get_chat_completions_filters_by_prefix() -> None:
    completer = ChatSlashCompleter()

    assert _completion_names(completer, "/co") == ["context", "compact", "config"]


def test_permissions_completion_is_unique_and_mode_is_removed() -> None:
    completer = ChatSlashCompleter()
    names = [spec.name for spec in get_chat_specs()]

    assert _completion_names(completer, "/permissions") == ["permissions"]
    assert names.count("permissions") == 1
    assert "mode" not in names
    assert _completion_names(completer, "/mode") == ["model"]


def test_chat_completions_exclude_hidden_and_removed_commands() -> None:
    completer = ChatSlashCompleter()
    names = set(_completion_names(completer, "/"))
    spec_names = {spec.name for spec in get_chat_specs()}

    assert "quit" not in spec_names
    assert "ctx" not in spec_names
    assert "model" in spec_names
    assert "model-info" not in spec_names
    assert "paste-image" not in spec_names
    assert "clear-images" not in spec_names
    assert "skills" not in spec_names
    assert "subagents" in spec_names
    assert (
        not {
            "assets",
            "back",
            "done",
            "show",
            "goal",
            "task",
            "assistant",
            "execute",
        }
        & names
    )


def test_nested_chat_completions_cover_usage_and_skill_names() -> None:
    completer = ChatSlashCompleter(
        skill_names_provider=lambda: ["python", "docker"],
    )

    assert _completion_names(completer, "/usage ") == [
        "usage hud",
        "usage hud on",
        "usage hud off",
        "usage hud status",
    ]
    assert _completion_names(completer, "/terminals ") == [
        "terminals list",
        "terminals show",
        "terminals kill",
        "terminals help",
    ]
    assert _completion_names(completer, "/stream ") == [
        "stream on",
        "stream off",
        "stream status",
    ]
    assert _completion_names(completer, "/skill ") == ["skill python", "skill docker"]
    assert _completion_names(completer, "/skill py") == ["skill python"]
    assert _completion_names(completer, "/skill python task") == []
    assert _completion_names(completer, "$") == ["$python", "$docker"]
    assert _completion_names(completer, "$py") == ["$python"]


def test_dollar_skill_completion_only_applies_at_input_token_start() -> None:
    completer = ChatSlashCompleter(
        skill_names_provider=lambda: ["code-review"],
    )

    assert _completion_names(completer, "$") == ["$code-review"]
    assert _completion_names(completer, "$code-review task") == []
    assert _completion_names(completer, "review with $") == []


def test_dollar_skill_completion_excludes_disabled_bundled_skill(tmp_path: Path) -> None:
    user_config_dir = tmp_path / "usercfg"
    user_config_dir.mkdir()
    (user_config_dir / "skills.json").write_text(
        '{"version": 1, "disabled_names": ["code-review"]}',
        encoding="utf-8",
    )
    discovered = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=user_config_dir,
        home_dir=tmp_path / "empty-home",
    )
    catalog = resolve_skill_catalog(
        discovered=discovered,
        workspace_root=tmp_path,
        user_config_dir=user_config_dir,
    )
    completer = ChatSlashCompleter(
        skill_names_provider=lambda: sorted(catalog.effective.skills),
    )

    completions = _completion_names(completer, "$")
    assert "$commit" in completions
    assert "$code-review" not in completions


def test_slash_skill_completion_excludes_disabled_bundled_skill(tmp_path: Path) -> None:
    user_config_dir = tmp_path / "usercfg"
    user_config_dir.mkdir()
    (user_config_dir / "skills.json").write_text(
        '{"version": 1, "disabled_names": ["code-review"]}',
        encoding="utf-8",
    )
    discovered = discover_skills(
        focus_path=tmp_path,
        workspace_root=tmp_path,
        user_config_dir=user_config_dir,
        home_dir=tmp_path / "empty-home",
    )
    catalog = resolve_skill_catalog(
        discovered=discovered,
        workspace_root=tmp_path,
        user_config_dir=user_config_dir,
    )
    completer = ChatSlashCompleter(
        skill_names_provider=lambda: sorted(catalog.effective.skills),
    )

    completions = _completion_names(completer, "/skill ")
    assert "skill commit" in completions
    assert "skill code-review" not in completions


def test_no_completions_for_plain_text() -> None:
    completer = ChatSlashCompleter()

    assert _completion_names(completer, "hello world") == []


def test_completion_carries_usage_and_description() -> None:
    completer = ChatSlashCompleter()
    completion = list(completer.get_completions(Document(text="/model-i"), None))

    assert completion == []

    usage_completion = list(completer.get_completions(Document(text="/usage "), None))[0]
    assert "/usage hud" in _display_text(usage_completion.display)
    assert "Open usage HUD controls" in _display_text(usage_completion.display)
    assert _display_text(usage_completion.display_meta) == ""


@pytest.mark.parametrize("text", ["$", "/skill "])
def test_completion_display_rows_are_bounded(text: str) -> None:
    completer = ChatSlashCompleter(
        skill_names_provider=lambda: ["skill-with-a-very-long-name-that-keeps-going"],
    )

    rows = [
        _display_text(completion.display)
        for completion in completer.get_completions(Document(text=text), None)
    ]

    assert rows
    assert all(len(row) <= 72 for row in rows)
    assert any("..." in row for row in rows)


def test_completion_yields_all_matching_top_level_commands() -> None:
    completer = ChatSlashCompleter()

    completions = list(completer.get_completions(Document(text="/"), None))

    assert len(completions) == len(get_chat_specs())
    completion_names = [completion.text for completion in completions]
    assert completion_names
    assert "/plan" not in completion_names
    assert "/resume" in completion_names
    assert "/config" in completion_names
    assert "/subagent" not in completion_names
    assert "/subagents" in completion_names
    assert "/skill" in completion_names
    assert "/forge" not in completion_names
    assert "/exit" in completion_names


def test_removed_subagent_command_has_no_completions() -> None:
    completer = ChatSlashCompleter()

    top_level = _completion_names(completer, "/suba")
    nested = _completion_names(completer, "/subagent ")

    assert "subagent" not in top_level
    assert nested == []


def test_specs_match_legacy_chat_handler() -> None:
    source = Path("src/alysis_code/cli_impl/chat/commands.py").read_text(encoding="utf-8")
    handler_source = _source_between(
        source,
        "def _handle_chat_command(",
        "def _handle_terminals_command(",
    )

    for spec in get_chat_specs():
        assert f'"/{spec.name}"' in handler_source
