from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("first_entrypoint", ["ide", "cli"])
def test_chat_dependency_overrides_work_after_either_entrypoint_initializes(first_entrypoint):
    # A fresh process is essential: previous CLI calls used to hide the IDE-first bug.
    source = """
import sys
from types import SimpleNamespace
from alysis_code import cli
from alysis_code.cli_impl import chat
from alysis_code.ide.stdio_bridge import _ensure_chat_loop_facade

if sys.argv[1] == 'cli':
    chat._sync_cli_globals(cli)
loop = _ensure_chat_loop_facade()
original_reset = loop._reset_chat_conversation
calls = []
cli._rebuild_session_tools_for_mode = lambda **kwargs: calls.append(kwargs['mode'])
cli.refresh_session_environment_context_message = lambda session: calls.append(session.mode)
cli._refresh_chat_hud_context_cache = lambda session: calls.append('hud')
cli._reset_chat_conversation = lambda **kwargs: None
chat._sync_cli_globals(cli)
assert loop._reset_chat_conversation is original_reset
assert loop._refresh_chat_hud_context_cache is cli._refresh_chat_hud_context_cache
session = SimpleNamespace(mode='review', store=None)
loop._apply_chat_effective_mode(session=session, next_mode='auto', persist_default_mode=False)
assert session.mode == 'auto' and calls == ['auto', 'auto'], calls
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", source, first_entrypoint],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
