from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from alysis_code import cli as cli_mod
from alysis_code.cli_impl.chat_slash_completer import max_completions_for_mode
from alysis_code.cli_impl.commands import prompt_helpers as prompt_helpers_mod


def test_chat_prompt_completion_menu_height_uses_dynamic_completion_count(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        prompt_helpers_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((80, 80)),
    )

    height = prompt_helpers_mod._chat_prompt_completion_menu_height("chat")
    expected_count = prompt_helpers_mod._chat_prompt_session_completion_count("chat")

    assert height == min(expected_count, max(8, int(80 * 0.6)))
    assert 8 <= height <= expected_count


def test_chat_prompt_completion_menu_height_handles_tiny_terminal(monkeypatch) -> None:
    monkeypatch.setattr(
        prompt_helpers_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((80, 10)),
    )

    height = prompt_helpers_mod._chat_prompt_completion_menu_height("chat")

    assert height == 8
    assert height <= prompt_helpers_mod._chat_prompt_session_completion_count("chat")


def test_chat_prompt_session_passes_dynamic_reserve_space_for_menu(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: list[dict[str, Any]] = []

    class _PromptSessionStub:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(dict(kwargs))
            self.app = SimpleNamespace(
                output=SimpleNamespace(responds_to_cpr=lambda: True),
                ttimeoutlen=0.5,
            )

    import prompt_toolkit  # type: ignore[import-not-found]

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _PromptSessionStub)
    monkeypatch.setattr(prompt_helpers_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(
        prompt_helpers_mod.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((80, 10)),
    )
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path))

    assert cli_mod._maybe_make_chat_prompt_session is not None
    session = prompt_helpers_mod._maybe_make_chat_prompt_session(
        console=Console(),
        root=tmp_path,
        pending_images=[],
        forge_state=prompt_helpers_mod._ForgeChatState(),
    )

    assert session is not None
    assert captured[0]["reserve_space_for_menu"] == 8


def test_chat_prompt_session_completion_count_covers_mode_switching() -> None:
    count = prompt_helpers_mod._chat_prompt_session_completion_count("chat")

    assert count >= max_completions_for_mode("chat")
    assert count >= max_completions_for_mode("forge")
