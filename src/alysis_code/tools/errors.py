"""Tool failure contracts available without loading execution implementations."""

from __future__ import annotations

from typing import Any


class ToolError(RuntimeError):
    """A local tool failure, optionally identified as an engine policy denial.

    Only the guard that refuses an operation supplies ``denial_code``. Tool
    result dictionaries and human-readable messages do not grant that provenance.
    """

    denial_code: str | None = None

    def __init__(
        self,
        *args: object,
        result_payload: dict[str, Any] | None = None,
        denial_code: str | None = None,
    ) -> None:
        super().__init__(*args)
        if denial_code is not None:
            self.denial_code = denial_code
        self.result_payload = result_payload
        if self.denial_code is not None:
            self.result_payload = {**(result_payload or {}), "error_code": self.denial_code}


class GitError(ToolError):
    pass


class GitDirectoryAccessError(GitError):
    """A directory access failure, distinct from filter or revision refusals."""


class HistorySearchError(RuntimeError):
    pass


class SearchError(RuntimeError):
    pass


class ShellError(ToolError):
    pass


class SymbolSearchError(RuntimeError):
    pass
