"""UI-neutral asynchronous access to the existing structured review engine."""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .cancellation import CooperativeCancellationError, InteractiveCancellationToken
from .code_review import (
    CodeReviewEngine,
    CodeReviewResult,
    ReviewFinding,
    ReviewRequest,
    ReviewScope,
)
from .diff_paths import parse_diff_git_line


def make_request(
    scope: str, *, base: str = "", head: str = "HEAD", revision: str = "HEAD"
) -> ReviewRequest:
    mode = ReviewScope(scope)
    request = (
        ReviewRequest.working_tree()
        if mode == ReviewScope.WORKING_TREE
        else ReviewRequest.commit(revision.strip())
        if mode == ReviewScope.COMMIT
        else ReviewRequest.branch(base=base.strip(), head=head.strip())
        if mode == ReviewScope.BRANCH
        else ReviewRequest.revision_range(base=base.strip(), head=head.strip())
    )
    request.validate()
    return request


@dataclass(frozen=True)
class FindingLocation:
    text: str
    row: int | None
    notice: str


def inspect_finding(result: CodeReviewResult, finding: ReviewFinding) -> FindingLocation:
    """Navigate the retained, redacted patch, never a possibly different live file.

    Working-tree patches can contain staged AND unstaged versions of a path.
    Ambiguous line references must not silently select one of those versions.
    """
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in result.diff.patch.splitlines():
        if line.startswith("diff --git "):
            old, new = parse_diff_git_line(line)
            current = [] if finding.path in (old, new) else None
            if current is not None:
                sections.append(current)
        if current is not None:
            current.append(line)
    if not sections:
        return FindingLocation(
            "", None, "File unavailable in retained review patch (possibly omitted or truncated)."
        )
    text = "\n\n".join("\n".join(section) for section in sections)
    if finding.line_start is None:
        return FindingLocation(
            text, None, "No line location supplied; showing reviewed file patch."
        )
    matches: list[int] = []
    offset = 0
    for section in sections:
        old_line = new_line = None
        deleted = "+++ /dev/null" in section
        for index, line in enumerate(section):
            hunk = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if hunk:
                old_line, new_line = map(int, hunk.groups())
                continue
            if new_line is None or not line or line[0] not in " +-":
                continue
            relevant = old_line if deleted else new_line
            belongs = line[0] != ("+" if deleted else "-")
            if belongs and relevant == finding.line_start:
                matches.append(offset + index)
            if line[0] != "+":
                old_line += 1
            if line[0] != "-":
                new_line += 1
        offset += len(section) + 1
    if len(matches) != 1:
        notice = (
            "Ambiguous staged/unstaged location; showing all reviewed versions."
            if matches
            else "Line unavailable in reviewed patch; it may be outside retained hunks or stale."
        )
        return FindingLocation(text, None, notice)
    return FindingLocation(
        text, matches[0], "Reviewed snapshot location; current workspace may have changed."
    )


class ReviewCancellationToken(InteractiveCancellationToken):
    def throw_if_cancelled(self, reason: str = "cancelled_by_user") -> None:
        if self.is_cancelled:
            raise CooperativeCancellationError(reason)


class ReviewController:
    def __init__(
        self,
        engine_factory: Callable[[ReviewCancellationToken], CodeReviewEngine],
        *,
        on_change: Callable[[], None],
        dispose_engine: Callable[[CodeReviewEngine], None] | None = None,
    ):
        self.engine_factory = engine_factory
        self.dispose_engine = dispose_engine
        self.on_change = on_change
        self.result: CodeReviewResult | None = None
        self.status = "Choose a scope, then start review."
        self.running = False
        self._generation = 0
        self._task: asyncio.Task | None = None
        self._token = ReviewCancellationToken()
        self._worker_lock = threading.Lock()
        self._worker_busy = threading.Event()

    def start(self, request: ReviewRequest) -> asyncio.Task:
        request.validate()
        self.cancel(notify=False)
        self.running = True
        self.result = None
        self.status = "Reviewing selected scope: collecting changes and waiting for provider…"
        if self._worker_busy.is_set():
            self.status = "Waiting for the previous cancelled review to finish before starting…"
        generation = self._generation
        self._token = ReviewCancellationToken()
        self._task = asyncio.create_task(
            self._run(request, generation, self._token, self.engine_factory, self.dispose_engine)
        )
        self.on_change()
        return self._task

    def _execute(
        self,
        request: ReviewRequest,
        token: ReviewCancellationToken,
        engine_factory: Callable[[ReviewCancellationToken], CodeReviewEngine],
        dispose_engine: Callable[[CodeReviewEngine], None] | None,
        started: Callable[[], None],
    ) -> CodeReviewResult:
        with self._worker_lock:
            token.throw_if_cancelled()
            self._worker_busy.set()
            engine = None
            try:
                started()
                engine = engine_factory(token)
                token.throw_if_cancelled()
                return engine.review(request, cancellation_token=token)
            finally:
                try:
                    if engine is not None and dispose_engine is not None:
                        dispose_engine(engine)
                finally:
                    self._worker_busy.clear()

    async def _run(
        self,
        request: ReviewRequest,
        generation: int,
        token: ReviewCancellationToken,
        engine_factory: Callable[[ReviewCancellationToken], CodeReviewEngine],
        dispose_engine: Callable[[CodeReviewEngine], None] | None,
    ) -> None:
        loop = asyncio.get_running_loop()

        def started():
            if generation == self._generation and self.running:
                self.status = (
                    "Reviewing selected scope: collecting changes and waiting for provider…"
                )
                self.on_change()

        try:
            result = await asyncio.to_thread(
                self._execute,
                request,
                token,
                engine_factory,
                dispose_engine,
                lambda: loop.call_soon_threadsafe(started),
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if generation == self._generation:
                # Provider exceptions can contain headers, prompts, or secrets.
                self.status = (
                    f"Review failed ({type(exc).__name__}). Check provider configuration and retry."
                )
        else:
            if generation == self._generation:
                self.result = result
                self.status = (
                    f"Review complete: {len(result.findings)} findings. {result.summary.overview}"
                )
        finally:
            if generation == self._generation:
                self.running = False
                self.on_change()

    def cancel(self, *, notify: bool = True) -> None:
        was_running = self.running
        self._generation += 1
        self._token.cancel_nonblocking()
        if self._task is not None:
            self._task.cancel()
        self.running = False
        if was_running:
            self.status = "Cancellation requested; results discarded. Providers supporting cancellation are signalled; others may finish in the background."
        if notify:
            self.on_change()
