"""Read-only host runtime roots supplied by a trusted Desktop launcher."""

from __future__ import annotations

import json
from pathlib import Path

from .branding import env_get
from .config import ConfigError


def desktop_runtime_roots() -> tuple[Path, ...]:
    raw = env_get("ALYSIS_DESKTOP_RUNTIME_ROOTS")
    if raw is None:
        return ()
    try:
        if len(raw) > 131_072:
            raise ValueError("oversized runtime policy")
        values = json.loads(raw)
        if (
            not isinstance(values, list)
            or len(values) > 16
            or any(
                not isinstance(value, str)
                or not value
                or "\0" in value
                or not Path(value).is_absolute()
                for value in values
            )
        ):
            raise ValueError("invalid runtime roots")
        roots = tuple(dict.fromkeys(Path(value) for value in values))
        for root in roots:
            if (
                (root.exists() and not root.is_dir())
                or root.resolve() != root
                or any(
                    parent.is_symlink() or getattr(parent, "is_junction", lambda: False)()
                    for parent in (root, *root.parents)
                )
            ):
                raise ValueError("runtime root is invalid or aliased")
        return roots
    except (ValueError, TypeError, OSError) as exc:
        raise ConfigError(
            "Invalid trusted Desktop runtime policy; no command was launched."
        ) from exc
