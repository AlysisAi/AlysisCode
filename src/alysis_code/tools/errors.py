"""Tool failure contracts available without loading execution implementations."""

from __future__ import annotations

from typing import Any


class GitError(RuntimeError):
    pass


class GitDirectoryAccessError(GitError):
    """A directory access failure, distinct from filter or revision refusals."""


class HistorySearchError(RuntimeError):
    pass


class SearchError(RuntimeError):
    pass


class ShellError(RuntimeError):
    def __init__(self, message: str, *, result_payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        # The tool result to record instead of the bare message, when there is one.
        self.result_payload = result_payload


class SymbolSearchError(RuntimeError):
    pass
