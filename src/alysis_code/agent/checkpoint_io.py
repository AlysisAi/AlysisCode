"""Filesystem-only checkpoint child, independent of parent process management."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..artifact_filesystem import FilesystemArtifactLayout
from ..checkpoint_settings import CheckpointSettings
from .anytime_checkpoint import AnytimeCheckpointManager


def main() -> None:
    # The JSON command comes only from the owning host process. Never accept a
    # function/module name or executable supplied by a task or workspace file.
    payload = json.load(sys.stdin)
    manager = AnytimeCheckpointManager(
        root=Path(payload["root"]),
        layout=FilesystemArtifactLayout(Path(payload["artifact_root"]), payload["locator_prefix"]),
        config=CheckpointSettings(**payload["config"]),
        excluded_paths=tuple(Path(item) for item in payload["excluded_paths"]),
    )
    manager._restore_worker_state(payload["state"])
    manager._io_stop_at = float(payload["stop_at"])
    manager._check_io_deadline()
    action = payload["action"]
    if action == "select_task":
        event = manager._select_task_inline(**payload["arguments"])
    elif action == "consider":
        arguments = payload["arguments"]
        arguments["accepted_commands"] = set(arguments["accepted_commands"])
        event = manager._consider_inline(**arguments)
    else:
        raise ValueError("unsupported_checkpoint_operation")
    manager._check_io_deadline()
    print(json.dumps({"state": manager._worker_state(), "event": event}, ensure_ascii=True))


if __name__ == "__main__":
    main()
