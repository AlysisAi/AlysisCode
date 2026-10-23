from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..session_store import (
    canonical_workspace_path,
    filter_sessions_to_local_owner,
    list_sessions,
    sanitize_session_id,
    session_belongs_to_workspace,
)
from .protocol import redact_secrets


class SessionSearchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SessionSearchLimits:
    max_sessions: int = 50
    max_results: int = 50
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    max_snippet_chars: int = 600
    max_artifacts_per_session: int = 500


_MAX_SESSIONS = 100
_MAX_RESULTS = 100
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_SNIPPET_CHARS = 4_000
_MAX_ARTIFACTS_PER_SESSION = 1_000


def search_workspace_sessions(
    *,
    sessions_dir: Path,
    workspace_root: Path,
    query: str,
    limits: SessionSearchLimits | None = None,
    git_root: Path | None = None,
) -> dict[str, Any]:
    active = _normalized_limits(limits or SessionSearchLimits())
    clean_query = _validated_query(query)
    canonical_workspace = canonical_workspace_path(workspace_root)
    if canonical_workspace is None:
        raise SessionSearchError("Workspace path is invalid.")

    infos = filter_sessions_to_local_owner(list_sessions(sessions_dir))
    canonical_git = canonical_workspace_path(git_root)
    all_scoped = [
        info
        for info in infos
        if session_belongs_to_workspace(info, canonical_workspace, canonical_git)
    ]
    scoped = all_scoped[: active.max_sessions]
    lowered = clean_query.casefold()
    results: list[dict[str, Any]] = []
    scanned_bytes = 0
    scanned_events = 0
    truncated = len(all_scoped) > len(scoped)

    for info in scoped:
        if len(results) >= active.max_results or scanned_bytes >= active.max_total_bytes:
            truncated = True
            break
        budget = min(active.max_file_bytes, active.max_total_bytes - scanned_bytes)
        try:
            raw = _read_bounded(info.path, budget)
        except OSError:
            continue
        scanned_bytes += len(raw)
        try:
            file_size = info.path.stat().st_size
        except OSError:
            file_size = len(raw)
        if file_size > len(raw):
            truncated = True
        for line_number, raw_line in enumerate(raw.splitlines(), start=1):
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            scanned_events += 1
            searchable = _searchable_event_text(event)
            match_index = searchable.casefold().find(lowered)
            if match_index < 0:
                continue
            snippet, snippet_truncated = _snippet(
                searchable,
                match_index,
                len(clean_query),
                active.max_snippet_chars,
            )
            results.append(
                {
                    "result_id": f"{info.session_id}:{line_number}",
                    "session_id": info.session_id,
                    "event_type": str(event.get("type") or "event")[:128],
                    "timestamp": str(event.get("ts") or "")[:128] or None,
                    "snippet": str(redact_secrets(snippet)),
                    "snippet_truncated": snippet_truncated,
                }
            )
            if len(results) >= active.max_results:
                truncated = True
                break
        if len(results) >= active.max_results or scanned_bytes >= active.max_total_bytes:
            continue
        artifacts, artifacts_truncated = _session_tool_output_artifacts(
            sessions_dir=sessions_dir,
            workspace_root=canonical_workspace,
            session_id=info.session_id,
            max_items=active.max_artifacts_per_session,
        )
        truncated = truncated or artifacts_truncated
        for artifact_path, artifact_ref in artifacts:
            if len(results) >= active.max_results or scanned_bytes >= active.max_total_bytes:
                truncated = True
                break
            budget = min(active.max_file_bytes, active.max_total_bytes - scanned_bytes)
            try:
                artifact_raw = _read_bounded(artifact_path, budget)
            except OSError:
                continue
            scanned_bytes += len(artifact_raw)
            try:
                artifact_size = artifact_path.stat().st_size
            except OSError:
                artifact_size = len(artifact_raw)
            if artifact_size > len(artifact_raw):
                truncated = True
            artifact_text = artifact_raw.decode("utf-8", errors="replace")
            match_index = artifact_text.casefold().find(lowered)
            if match_index < 0:
                continue
            snippet, snippet_truncated = _snippet(
                artifact_text,
                match_index,
                len(clean_query),
                active.max_snippet_chars,
            )
            artifact_key = hashlib.sha256(artifact_ref.encode("utf-8")).hexdigest()[:16]
            try:
                timestamp = datetime.fromtimestamp(
                    artifact_path.stat().st_mtime,
                    tz=UTC,
                ).isoformat()
            except (OSError, OverflowError, ValueError):
                timestamp = None
            results.append(
                {
                    "result_id": f"{info.session_id}:tool_output:{artifact_key}",
                    "session_id": info.session_id,
                    "event_type": "tool_output",
                    "source_kind": "tool_output",
                    "artifact_path": artifact_ref,
                    "timestamp": timestamp,
                    "snippet": str(redact_secrets(snippet)),
                    "snippet_truncated": snippet_truncated,
                }
            )

    return {
        "workspace_root": os.fspath(canonical_workspace),
        "query": str(redact_secrets(clean_query)),
        "results": results,
        "scanned_sessions": len(scoped),
        "scanned_events": scanned_events,
        "scanned_bytes": scanned_bytes,
        "truncated": truncated,
        "redacted": True,
        "secret_values_included": False,
    }


def past_session_context_block(result: dict[str, Any]) -> dict[str, Any]:
    session_id = str(result.get("session_id") or "").strip()
    result_id = str(result.get("result_id") or "").strip()
    snippet = str(result.get("snippet") or "").strip()
    if not session_id or not result_id or not snippet:
        raise SessionSearchError("Search result cannot be attached.")
    return {
        "type": "past_session",
        "content": str(redact_secrets(snippet)),
        "session_id": session_id,
        "turn_id": result_id,
        "title": "Past Alysis Code session match",
        "provenance": {
            "source": "alysis.session.search",
            "source_id": result_id,
        },
        "truncated": bool(result.get("snippet_truncated")),
    }


def _validated_query(value: str) -> str:
    if not isinstance(value, str):
        raise SessionSearchError("Search query must be a string.")
    clean = value.strip()
    if len(clean) < 2 or len(clean) > 256 or any(ord(char) < 32 for char in clean):
        raise SessionSearchError("Search query length or characters are invalid.")
    return clean


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(max(0, limit))


def _normalized_limits(limits: SessionSearchLimits) -> SessionSearchLimits:
    return SessionSearchLimits(
        max_sessions=max(1, min(int(limits.max_sessions), _MAX_SESSIONS)),
        max_results=max(1, min(int(limits.max_results), _MAX_RESULTS)),
        max_file_bytes=max(1, min(int(limits.max_file_bytes), _MAX_FILE_BYTES)),
        max_total_bytes=max(1, min(int(limits.max_total_bytes), _MAX_TOTAL_BYTES)),
        max_snippet_chars=max(1, min(int(limits.max_snippet_chars), _MAX_SNIPPET_CHARS)),
        max_artifacts_per_session=max(
            1,
            min(int(limits.max_artifacts_per_session), _MAX_ARTIFACTS_PER_SESSION),
        ),
    )


def _session_tool_output_artifacts(
    *,
    sessions_dir: Path,
    workspace_root: Path,
    session_id: str,
    max_items: int,
) -> tuple[list[tuple[Path, str]], bool]:
    session_component = sanitize_session_id(session_id)
    candidates = (
        (
            sessions_dir.resolve() / session_component,
            "session_artifacts/tool_outputs/",
        ),
        (
            (workspace_root / ".alysis" / "sessions" / session_component).resolve(),
            f".alysis/sessions/{session_component}/tool_outputs/",
        ),
    )
    artifacts: list[tuple[Path, str]] = []
    seen_roots: set[Path] = set()
    for artifact_root, display_prefix in candidates:
        if artifact_root in seen_roots:
            continue
        seen_roots.add(artifact_root)
        tool_output_root = artifact_root / "tool_outputs"
        try:
            paths = sorted(tool_output_root.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            if len(artifacts) >= max_items:
                return artifacts, True
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                path.resolve().relative_to(artifact_root)
            except (OSError, RuntimeError, ValueError):
                continue
            artifacts.append((path, display_prefix + path.name))
    return artifacts, False


def _searchable_event_text(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    safe = {
        "type": event.get("type"),
        "payload": payload if isinstance(payload, dict | list | str) else None,
    }
    try:
        return json.dumps(redact_secrets(safe), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(redact_secrets(safe))


def _snippet(text: str, index: int, query_chars: int, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    flank = max(0, (limit - query_chars) // 2)
    start = max(0, index - flank)
    end = min(len(text), start + limit)
    if end - start < limit:
        start = max(0, end - limit)
    value = text[start:end]
    return (
        ("…" if start else "") + value + ("…" if end < len(text) else ""),
        True,
    )
