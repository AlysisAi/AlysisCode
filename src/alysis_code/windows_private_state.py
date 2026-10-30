"""Keep native sandbox grants from exposing the host's private application state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config
from .branding import env_get
from .config import ConfigError
from .local_memory import memory_home


def protect_private_state(request: dict[str, Any]) -> None:
    # The supervisor supplies these from main-owned paths, never renderer input.
    # Standalone CLI runs always protect their own effective config and data roots.
    roots = [config.config_path().parent, config._data_dir()]
    # Custom memory storage may live outside the data directory, even inside a
    # selected project. It remains host-owned and must not bypass memory scope.
    memory = memory_home()
    if not any(memory.resolve().is_relative_to(root.resolve()) for root in roots):
        roots.append(memory)
    raw = env_get("ALYSIS_DESKTOP_CONTROL_PATHS")
    if raw:
        try:
            if len(raw) > 131_072:
                raise ValueError("oversized control-path policy")
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) > 256:
                raise ValueError("invalid control-path list")
            if any(not isinstance(value, str) or not value or "\0" in value for value in values):
                raise ValueError("invalid control path")
            roots.extend(Path(value) for value in values)
        except (ValueError, TypeError) as exc:
            raise ConfigError(
                "Invalid private application-state policy; no command was launched."
            ) from exc

    from .windows_sandbox import _local_path

    grants = [Path(value) for value in (*request["read"], *request["write"])]
    discovery = [Path(value) for value in request.get("enumerate", [])]
    boundaries: list[str] = []
    cwd = Path(request["cwd"])
    desktop_root = env_get("ALYSIS_DESKTOP_USER_DATA")
    if desktop_root:
        parent = _local_path(Path(desktop_root))
        # Main creates unpredictable temporary/settings-recovery names beside
        # its control files. Exact-file exclusions cannot protect future names.
        # Narrow work-tasks/<id> grants remain valid; a userData ancestor cannot.
        if any(parent.is_relative_to(grant) for grant in grants):
            raise ConfigError(
                "A sandbox grant cannot contain the Desktop application's data folder. "
                "Choose a narrower project folder."
            )
        if any(parent.is_relative_to(root) for root in discovery):
            # PSEC permits more-specific content roots inside a denied parent.
            # Keep runtime/work-task grants, but hide the profile itself and
            # current or future siblings of those explicitly admitted roots.
            boundaries.append(str(parent))
    denied: list[str] = []
    for path in dict.fromkeys(roots):
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise ConfigError("Private application-state paths must be absolute local paths.")
        # Resolve only existing, inspected paths. A missing path inside a grant
        # cannot safely be omitted: the child could plant future trusted state.
        if path.exists():
            canonical = _local_path(path, directory=False)
        else:
            canonical = path.resolve()
            if any(
                canonical.is_relative_to(grant) or grant.is_relative_to(canonical)
                for grant in grants
            ):
                raise ConfigError(
                    "Private application state inside the selected sandbox scope is missing. "
                    "Choose a project folder outside the application's settings and data."
                )
            if not any(canonical.is_relative_to(root) for root in discovery):
                continue
            # PSEC cannot deny an absent object. Keep its nearest existing
            # parent private so later host-created state cannot become visible
            # to an already-running directory-discovery command. Do not create
            # files or relax a content grant to achieve this.
            parent = next((p for p in canonical.parents if p.exists()), None)
            if parent is None:
                raise ConfigError(
                    "Cannot protect missing private state during directory discovery."
                )
            boundaries.append(str(_local_path(parent)))
            continue
        if cwd.is_relative_to(canonical) or any(
            Path(value).is_relative_to(canonical) for value in request["write"]
        ):
            raise ConfigError(
                "Private application state cannot be a sandbox working directory or writable root."
            )
        if not any(
            canonical.is_relative_to(grant) or grant.is_relative_to(canonical)
            for grant in (*grants, *discovery)
        ):
            continue
        denied.append(str(canonical))
    # MXC pins writable deny-write objects and their ancestors until job drain.
    # A read denial alone would leave a rename/replacement opportunity.
    request["deny_read"] = list(dict.fromkeys([*request["deny_read"], *denied]))
    request["deny_write"] = list(dict.fromkeys([*request["deny_write"], *denied]))
    if boundaries:
        request["discovery_deny"] = list(
            dict.fromkeys([*request.get("discovery_deny", []), *boundaries])
        )
