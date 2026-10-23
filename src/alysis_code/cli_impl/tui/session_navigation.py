"""Scoped session-picker data, with no dependency on the composer.

The caller supplies the account's configured sessions directory and the active
workspace. Opening, searching, renaming, and cancelling never mutate drafts or
attachments. UI switching must retain both until resume succeeds, and restore
both on failure; the picker only returns an explicitly selected session id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...ide.protocol import redact_secrets
from ...ide.session_search import (
    SessionSearchError,
    SessionSearchLimits,
    search_workspace_sessions,
)
from ...session_store import (
    SessionInfo,
    canonical_workspace_path,
    filter_sessions_to_local_owner,
    list_sessions,
    session_belongs_to_workspace,
)


@dataclass(frozen=True, slots=True)
class SessionPickerRow:
    session_id: str
    title: str
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class SessionPickerResult:
    rows: tuple[SessionPickerRow, ...]
    truncated: bool = False


def _bounded_title(info: SessionInfo) -> str:
    """Read classic-compatible titles and first prompts without unbounded I/O."""
    try:
        metadata_path = info.path.with_suffix(".meta.json")
        if metadata_path.is_symlink():
            raise OSError("Linked metadata is unavailable")
        with metadata_path.open("rb") as handle:
            metadata = json.loads(handle.read(64 * 1024))
        if isinstance(metadata, dict):
            for key in ("custom_name", "summary"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return " ".join(str(redact_secrets(value)).split())[:120]
    except (OSError, ValueError, UnicodeError):
        pass
    try:
        with info.path.open("rb") as handle:
            raw = handle.read(64 * 1024)
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "user_message":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            value = payload.get("display_content") or payload.get("content")
            if (
                isinstance(value, str)
                and value.strip()
                and not value.lstrip().startswith("<environment_context>")
            ):
                return " ".join(str(redact_secrets(value)).split())[:120]
    except OSError:
        pass
    return f"Conversation {info.session_id}"


class SessionPickerProvider:
    """Recheck owner and workspace on every read/write; never widen scope.

    Unknown-scope legacy sessions retain the existing explicit /resume route.
    Known git-root legacy sessions participate only with a supplied matching root.
    """

    def __init__(self, sessions_dir: Path, workspace_root: Path, git_root: Path | None = None):
        self.sessions_dir = Path(sessions_dir)
        self.workspace_root = canonical_workspace_path(workspace_root)
        self.git_root = canonical_workspace_path(git_root)
        if self.workspace_root is None:
            raise SessionSearchError("Workspace path is invalid.")

    def _scoped(self) -> list[SessionInfo]:
        return [
            info
            for info in filter_sessions_to_local_owner(list_sessions(self.sessions_dir))
            if session_belongs_to_workspace(info, self.workspace_root, self.git_root)
            and not info.path.is_symlink()
            and info.path.resolve().parent == self.sessions_dir.resolve()
        ]

    def resolve(self, session_id: str) -> SessionInfo:
        for info in self._scoped():
            if info.session_id == session_id:
                return info
        raise SessionSearchError("Session is unavailable in this account and workspace.")

    def search(self, query: str = "") -> SessionPickerResult:
        scoped = self._scoped()
        clean = query.strip()
        # The initial page is compact, but a search examines the shared service's
        # full bounded window, including older sessions outside that page.
        candidates = scoped[: 100 if clean else 50]
        rows = {
            info.session_id: SessionPickerRow(info.session_id, _bounded_title(info))
            for info in candidates
        }
        if not clean:
            return SessionPickerResult(tuple(rows.values()), len(scoped) > len(candidates))
        if len(clean) == 1:
            # The bounded shared search requires two characters. Keep the picker
            # responsive while typing the first character; search titles and ids.
            return SessionPickerResult(
                tuple(
                    row
                    for key, row in rows.items()
                    if clean.casefold() in (row.title + " " + key).casefold()
                ),
                len(scoped) > len(candidates),
            )
        # Use the shared bounded content search, including offloaded tool output.
        result = search_workspace_sessions(
            sessions_dir=self.sessions_dir,
            workspace_root=self.workspace_root,
            git_root=self.git_root,
            query=clean,
            limits=SessionSearchLimits(max_sessions=100, max_results=100),
        )
        matches = {
            key: row
            for key, row in rows.items()
            if clean.casefold() in (row.title + " " + key).casefold()
        }
        for match in result["results"]:
            key = match["session_id"]
            if key in rows and key not in matches:
                matches[key] = SessionPickerRow(key, rows[key].title, match["snippet"])
        return SessionPickerResult(
            tuple(matches.values()),
            bool(result["truncated"]) or len(scoped) > len(candidates),
        )

    def rename(self, session_id: str, title: str) -> SessionPickerRow:
        from ..commands.chat_resume_helpers import _rename_resume_session_custom_title

        info = self.resolve(session_id)
        if info.path.with_suffix(".meta.json").is_symlink():
            raise SessionSearchError("Session metadata is unavailable.")
        success, message = _rename_resume_session_custom_title(info=info, new_title=title)
        if not success:
            raise SessionSearchError(message)
        return SessionPickerRow(info.session_id, _bounded_title(info))
