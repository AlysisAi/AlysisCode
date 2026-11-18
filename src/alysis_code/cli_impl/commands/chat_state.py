# ruff: noqa: F401,F403,F405,I001
# Legacy split module: dependencies are synced by cli_surface.py.
from __future__ import annotations

from .cli_common import *
from ..chat.state import _ChatExecutionRequest


# Grace window for VT100 escape sequences before a lone Esc byte is treated as
# the standalone Esc hotkey (paste clipboard image) in the classic prompt.
_CHAT_PROMPT_ESCAPE_SEQUENCE_TIMEOUT_S = 1.0

__all__ = [name for name in globals() if (not name.startswith("__") or name == "__version__")]
