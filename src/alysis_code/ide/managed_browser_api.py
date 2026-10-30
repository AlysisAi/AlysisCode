"""Lazy canonical browser types for callers that only need annotations.

Qualified annotations can be resolved by typing.get_type_hints without making
ordinary tool registration import the browser runtime or its egress proxy.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .managed_browser import (
        BrowserArtifact as BrowserArtifact,
    )
    from .managed_browser import (
        BrowserError as BrowserError,
    )
    from .managed_browser import (
        BrowserSessionStatus as BrowserSessionStatus,
    )
    from .managed_browser import (
        ManagedBrowserService as ManagedBrowserService,
    )

__all__ = ["BrowserArtifact", "BrowserError", "BrowserSessionStatus", "ManagedBrowserService"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(".managed_browser", __package__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
