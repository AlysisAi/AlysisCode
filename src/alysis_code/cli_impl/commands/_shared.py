from __future__ import annotations

import io
import shutil
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from click import get_current_context

from ...forge_events import machine_output_active
from ...surface.console import make_console
from ...workspace_context import resolve_workspace_context
from . import _patchable

if TYPE_CHECKING:
    from rich.console import Console


JSON_OPTION_HELP = (
    "Emit one machine-readable JSON value on stdout and nothing else. "
    "Exit code 0 means the command ran; the payload carries the auth state."
)


class Mode(StrEnum):
    review = "review"
    auto = "auto"
    readonly = "readonly"
    fullaccess = "fullaccess"


def _Table(*args: Any, **kwargs: Any) -> Any:
    from rich.table import Table

    return Table(*args, **kwargs)


class _DiscardStream(io.TextIOBase):
    """Sink for Rich output while stdout belongs to the machine event stream."""

    encoding = "utf-8"

    def write(self, text: str) -> int:  # type: ignore[override]
        return len(text)

    def isatty(self) -> bool:
        return False


def _quiet_console() -> Console:
    return make_console(
        file=_DiscardStream(),
        force_terminal=False,
        no_color=True,
        width=_terminal_width(),
    )


def _console() -> Console:
    # In machine mode stdout carries NDJSON only, so Rich renders into a sink instead.
    # Deep execution code keeps printing through the console it was handed; nothing
    # below this point needs to know which mode it is in.
    if machine_output_active():
        return _quiet_console()
    return make_console(width=_terminal_width())


def _terminal_width(default: int = 120) -> int:
    try:
        ctx = get_current_context(silent=True)
    except RuntimeError:
        ctx = None
    if ctx is not None:
        ctx_width = getattr(ctx, "terminal_width", None)
        if isinstance(ctx_width, int) and ctx_width > 0:
            return ctx_width
        max_content_width = getattr(ctx, "max_content_width", None)
        if isinstance(max_content_width, int) and max_content_width > 0:
            return max_content_width
    stdout = getattr(sys, "stdout", None)
    if stdout is None or not getattr(stdout, "isatty", lambda: False)():
        return default
    try:
        return shutil.get_terminal_size((default, 40)).columns
    except OSError:
        return default


def _resolve_tool_workspace_root(*, path: Path) -> Path:
    workspace_context = _patchable("resolve_workspace_context", resolve_workspace_context)(
        path.expanduser().resolve()
    )
    return workspace_context.workspace_root
