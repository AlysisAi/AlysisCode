from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ide.protocol import redact_secrets
from ..session_artifacts import SessionArtifactLayout
from .errors import HistorySearchError

_SNIPPET_MAX_CHARS = 240
_MAX_RESULTS = 500
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_TARGET_FILES = 2_000
_MAX_SNIPPET_CHARS = 4_000


def _safe_component(raw: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", str(raw).strip())
    return clean or "x"


def _clip_text(text: str, limit: int = _SNIPPET_MAX_CHARS) -> str:
    compact = re.sub(r"\s+", " ", text.strip())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


@dataclass(frozen=True)
class _ArtifactSearchRoot:
    path: Path
    layout: SessionArtifactLayout | None = None


def _search_roots(
    *,
    root: Path,
    session_id: str,
    session_artifact_root: Path | None,
) -> list[_ArtifactSearchRoot]:
    roots: list[_ArtifactSearchRoot] = []
    seen: set[Path] = set()

    if session_artifact_root is not None:
        artifact_root = session_artifact_root.resolve()
        if artifact_root.name == _safe_component(session_id) and artifact_root not in seen:
            roots.append(
                _ArtifactSearchRoot(
                    path=artifact_root,
                    layout=SessionArtifactLayout(filesystem_root=artifact_root),
                )
            )
            seen.add(artifact_root)

    history_root = (root / ".alysis" / "sessions" / _safe_component(session_id)).resolve()
    if history_root not in seen:
        roots.append(_ArtifactSearchRoot(path=history_root))
        seen.add(history_root)
    return roots


def _iter_target_files(
    *,
    roots: list[_ArtifactSearchRoot],
    include_history: bool,
    include_tool_outputs: bool,
    include_memory: bool,
) -> tuple[list[tuple[str, Path, SessionArtifactLayout | None]], bool]:
    targets: list[tuple[str, Path, SessionArtifactLayout | None]] = []
    for artifact_root in roots:
        base = artifact_root.path
        if include_history:
            for path in sorted((base / "history").glob("chunk_*.jsonl")):
                if _is_safe_artifact_file(path=path, root=base):
                    targets.append(("history", path, artifact_root.layout))
                    if len(targets) >= _MAX_TARGET_FILES:
                        return targets, True
        if include_tool_outputs:
            for path in sorted((base / "tool_outputs").glob("*.json")):
                if _is_safe_artifact_file(path=path, root=base):
                    targets.append(("tool_output", path, artifact_root.layout))
                    if len(targets) >= _MAX_TARGET_FILES:
                        return targets, True
        if include_memory:
            for filename in ("summary.json", "pins.json"):
                path = base / "memory" / filename
                if _is_safe_artifact_file(path=path, root=base):
                    targets.append(("memory", path, artifact_root.layout))
                    if len(targets) >= _MAX_TARGET_FILES:
                        return targets, True
    return targets, False


def _is_safe_artifact_file(*, path: Path, root: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _display_artifact_path(
    *,
    file_path: Path,
    workspace_root: Path,
    layout: SessionArtifactLayout | None,
) -> str:
    if layout is not None:
        return layout.display_reference_for_path(
            artifact_path=file_path,
            workspace_root=workspace_root,
        )
    try:
        return os.fspath(file_path.resolve().relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        return file_path.name


def history_search(
    *,
    root: Path,
    session_id: str,
    session_artifact_root: Path | None = None,
    pattern: str,
    max_results: int = 50,
    max_file_bytes: int = 200_000,
    max_total_bytes: int = _MAX_TOTAL_BYTES,
    max_snippet_chars: int = _SNIPPET_MAX_CHARS,
    include_history: bool = True,
    include_tool_outputs: bool = True,
    include_memory: bool = True,
) -> dict[str, Any]:
    root_abs = root.resolve()

    max_results = max(1, min(int(max_results), _MAX_RESULTS))
    max_file_bytes = max(1, min(int(max_file_bytes), _MAX_FILE_BYTES))
    max_total_bytes = max(1, min(int(max_total_bytes), _MAX_TOTAL_BYTES))
    max_snippet_chars = max(1, min(int(max_snippet_chars), _MAX_SNIPPET_CHARS))

    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise HistorySearchError(f"Invalid regex pattern: {exc}") from exc

    matches: list[dict[str, Any]] = []
    truncated = False
    result_limit_reached = False
    scanned_bytes = 0
    scanned_files = 0

    search_roots = _search_roots(
        root=root_abs,
        session_id=session_id,
        session_artifact_root=session_artifact_root,
    )
    if not any(search_root.path.exists() for search_root in search_roots):
        return {
            "pattern": str(redact_secrets(pattern)),
            "matches": matches,
            "truncated": truncated,
            "scanned_bytes": scanned_bytes,
            "scanned_files": scanned_files,
        }

    targets, target_list_truncated = _iter_target_files(
        roots=search_roots,
        include_history=include_history,
        include_tool_outputs=include_tool_outputs,
        include_memory=include_memory,
    )
    truncated = target_list_truncated

    for kind, file_path, layout in targets:
        remaining_bytes = max_total_bytes - scanned_bytes
        if remaining_bytes <= 0:
            truncated = True
            break
        read_limit = min(max_file_bytes, remaining_bytes)
        try:
            with file_path.open("rb") as fh:
                data = fh.read(read_limit + 1)
        except OSError:
            continue
        scanned_files += 1
        if len(data) > read_limit:
            data = data[:read_limit]
            truncated = True
        scanned_bytes += len(data)
        text = data.decode("utf-8", errors="replace")

        for line_no, line in enumerate(text.splitlines(), start=1):
            if not compiled.search(line):
                continue
            matches.append(
                {
                    "kind": kind,
                    "path": _display_artifact_path(
                        file_path=file_path,
                        workspace_root=root_abs,
                        layout=layout,
                    ),
                    "line": line_no,
                    "text": str(redact_secrets(_clip_text(line, max_snippet_chars))),
                }
            )
            if len(matches) >= max_results:
                truncated = True
                result_limit_reached = True
                break
        if result_limit_reached:
            break

    return {
        "pattern": str(redact_secrets(pattern)),
        "matches": matches,
        "truncated": truncated,
        "scanned_bytes": scanned_bytes,
        "scanned_files": scanned_files,
    }
