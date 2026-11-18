# ruff: noqa: F401,F403,F405,I001
from __future__ import annotations

from . import cli_common as _cli_common
from . import chat_state as _chat_state
from . import startup as _startup
from . import chat_terminal as _chat_terminal
from . import welcome as _welcome
from . import chat_resume_helpers as _chat_resume_helpers
from . import chat_status as _chat_status
from . import prompt_helpers as _prompt_helpers
from . import update as _update

from .cli_common import *
from .chat_state import *
from .startup import *
from .chat_terminal import *
from .welcome import *
from .chat_resume_helpers import *
from .chat_status import *
from .prompt_helpers import *
from .update import *

_CLI_SURFACE_MODULES = (
    _cli_common,
    _chat_state,
    _startup,
    _chat_terminal,
    _welcome,
    _chat_resume_helpers,
    _chat_status,
    _prompt_helpers,
    _update,
)

_CLI_SURFACE_INTERNAL_NAMES = {
    "_CLI_SURFACE_INTERNAL_NAMES",
    "_CLI_SURFACE_MODULES",
    "_chat_resume_helpers",
    "_chat_state",
    "_chat_status",
    "_chat_terminal",
    "_cli_common",
    "_prompt_helpers",
    "_startup",
    "_sync_module_globals",
    "_update",
    "_welcome",
}


def _sync_module_globals() -> None:
    surface_globals = globals()
    for module in _CLI_SURFACE_MODULES:
        module_globals = module.__dict__
        for name, value in surface_globals.items():
            if name.startswith("__") or name in _CLI_SURFACE_INTERNAL_NAMES:
                continue
            module_globals[name] = value


_sync_module_globals()

__all__ = [
    name
    for name in globals()
    if (not name.startswith("__") or name == "__version__")
    and name not in _CLI_SURFACE_INTERNAL_NAMES
]
