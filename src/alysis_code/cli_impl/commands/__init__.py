from __future__ import annotations

import sys
from typing import Any, TypeVar

_T = TypeVar("_T")


def _materialize_cli_globals(module: Any) -> None:
    """Prepare the canonical compatibility facade before copying its globals."""
    if module is not None and module is sys.modules.get("alysis_code.cli"):
        module._load_legacy_surface()


def _patchable(name: str, default: _T) -> _T:
    module = sys.modules.get("alysis_code.cli")
    # Inspect actual overrides without loading the legacy interactive facade just
    # to retrieve this command's already-imported default implementation.
    return vars(module).get(name, default) if module is not None else default
