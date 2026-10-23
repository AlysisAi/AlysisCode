"""Thread-safe conversation model for the full-screen TUI.

The agent turn runs on a worker thread and pushes content here (streamed
assistant tokens, tool-trace lines, errors); the prompt_toolkit UI thread reads
snapshots of it on every redraw. All mutation goes through a lock and triggers a
(thread-safe) ``invalidate`` so the next redraw picks up the change.

Kept free of any agent imports so it can be unit-tested in isolation.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .forge_status import forge_status_bucket, forge_status_is_terminal

# (role, text) — role drives styling in the app: "user" | "assistant" | "reasoning"
# | "trace" | "patch" (structured JSON) | "tool_output" | "error" | "warn" | "info" | "system".
Entry = tuple[str, str]


def _normalize_visible(text: str) -> str:
    """Whitespace-collapsed comparison key for assistant text, so a re-emitted
    reply that differs only in incidental spacing still reads as a duplicate."""
    return " ".join(str(text or "").split())


class TuiTranscript:
    def __init__(self, *, invalidate: Callable[[], None] | None = None) -> None:
        self.entries: list[Entry] = []
        self._lock = threading.RLock()
        self._status: str | None = None
        self._assistant_index: int | None = None
        self._response_assistant_indices: list[int] = []
        # Live provider-generated reasoning-summary block: index of the open
        # entry, when it started, and elapsed seconds for each closed block.
        self._reasoning_index: int | None = None
        self._reasoning_start: float | None = None
        self._reasoning_block_id: str | None = None
        self._reasoning_block_ids: dict[int, str] = {}
        self._reasoning_secs: dict[int, int] = {}
        self._trace_level = "compact"
        # Live Forge execution view: a task table + phase/spinner rendered at the
        # bottom of the transcript while ``/execute plan`` runs the swarm on a
        # worker thread. ``None`` when no run is active. Shape:
        #   {"run_id", "tasks": [{"id","title","status"}], "active": str|None,
        #    "active_map": {task_id: {"started": float, "phase": str,
        #                             "message": str}},
        #    "phase": str, "message": str, "started": float, "done": bool}
        # ``active`` is the MOST-RECENT task the swarm touched (drives the single
        # bottom phase line, kept for back-compat); ``active_map`` tracks EVERY
        # currently-running task so the renderer can spin them all and show a
        # per-task elapsed timer. A task is pruned from ``active_map`` the moment
        # its status turns terminal (done/failed/obsolete).
        self._forge: dict[str, object] | None = None
        self._invalidate: Callable[[], None] = invalidate or (lambda: None)

    def set_invalidate(self, fn: Callable[[], None]) -> None:
        self._invalidate = fn

    @property
    def trace_level(self) -> str:
        with self._lock:
            return self._trace_level

    def set_trace_level(self, level: str) -> str:
        normalized = str(level or "").strip().lower()
        with self._lock:
            if normalized not in {"off", "compact", "full"}:
                normalized = self._trace_level
            self._trace_level = normalized
        self._touch()
        return normalized

    def _touch(self) -> None:
        try:
            self._invalidate()
        except Exception:
            pass

    # ---- safe provider reasoning-summary block ----
    def _close_reasoning_locked(self) -> None:
        """Close the open reasoning block, recording its elapsed seconds. Caller
        holds the lock. Any content/tool/turn boundary collapses the summary."""
        if self._reasoning_index is not None:
            if self._reasoning_start is not None:
                self._reasoning_secs[self._reasoning_index] = max(
                    0, int(time.monotonic() - self._reasoning_start)
                )
            self._reasoning_index = None
            self._reasoning_start = None
            self._reasoning_block_id = None

    def begin_reasoning(self, block_id: str) -> None:
        """Open a reasoning block with a stable provider-call identifier.

        Reusing the active identifier is idempotent. A different identifier
        closes the previous block first so retries and later agent steps can
        never merge their summaries merely because no visible tool line landed
        between the two provider calls.
        """

        normalized = str(block_id or "").strip()
        if not normalized:
            return
        with self._lock:
            if self._reasoning_index is not None and self._reasoning_block_id == normalized:
                return
            self._close_reasoning_locked()
            self.entries.append(("reasoning", ""))
            self._reasoning_index = len(self.entries) - 1
            self._reasoning_start = time.monotonic()
            self._reasoning_block_id = normalized
            self._reasoning_block_ids[self._reasoning_index] = normalized
        self._touch()

    def end_reasoning(self, block_id: str | None = None) -> None:
        """Collapse the open summary block (e.g. when a tool starts) without
        appending anything — records its elapsed seconds for the summary."""
        with self._lock:
            normalized = str(block_id or "").strip()
            if normalized and normalized != self._reasoning_block_id:
                return
            self._close_reasoning_locked()
        self._touch()

    def stream_reasoning(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            if self._reasoning_index is None:
                self.entries.append(("reasoning", ""))
                self._reasoning_index = len(self.entries) - 1
                self._reasoning_start = time.monotonic()
                self._reasoning_block_id = f"legacy-{self._reasoning_index}"
                self._reasoning_block_ids[self._reasoning_index] = self._reasoning_block_id
            role, current = self.entries[self._reasoning_index]
            self.entries[self._reasoning_index] = (role, current + delta)
        self._touch()

    # ---- generic appends ----
    def append(self, role: str, text: str) -> None:
        with self._lock:
            self._close_reasoning_locked()
            self.entries.append((role, text))
            # Any non-streamed line closes the open assistant block.
            if role != "assistant":
                self._assistant_index = None
        self._touch()

    def append_trace(self, line: str) -> None:
        """Append a trace line, coalescing into the previous trace block.

        Consecutive trace lines (e.g. a stream of swarm-progress events) join into
        one multi-line entry instead of becoming separate blocks — the renderer
        puts a blank spacer *between* entries, so without coalescing a busy run
        would be sprayed across the pane."""
        with self._lock:
            self._close_reasoning_locked()
            if self.entries and self.entries[-1][0] == "trace":
                role, current = self.entries[-1]
                self.entries[-1] = (role, f"{current}\n{line}")
            else:
                self.entries.append(("trace", line))
            self._assistant_index = None
        self._touch()

    def append_user(self, text: str) -> None:
        with self._lock:
            self._close_reasoning_locked()
            self.entries.append(("user", text))
            self._assistant_index = None
            self._response_assistant_indices.clear()
            # A fresh submission ends the prior live Forge view (its final table
            # stays in scrollback only while it is the latest thing shown).
            self._forge = None
        self._touch()

    # ---- streaming assistant block ----
    def begin_turn(self) -> None:
        with self._lock:
            self._close_reasoning_locked()
            self._assistant_index = None
            self._response_assistant_indices.clear()
            self._status = None
        self._touch()

    def begin_assistant_response(self) -> None:
        """Keep separate model responses in separate transcript blocks."""
        # Gate candidates do not receive the final-message callback. Reconcile
        # the preceding candidate before starting another provider response.
        self.finish_assistant()
        with self._lock:
            self._close_reasoning_locked()
            self._assistant_index = None
            self._response_assistant_indices.clear()
        self._touch()

    def stream_assistant(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            # The first answer token collapses the reasoning block.
            self._close_reasoning_locked()
            if self._assistant_index is None:
                self.entries.append(("assistant", ""))
                self._assistant_index = len(self.entries) - 1
                self._response_assistant_indices.append(self._assistant_index)
            role, current = self.entries[self._assistant_index]
            self.entries[self._assistant_index] = (role, current + delta)
            self._status = None
        self._touch()

    def restart_assistant(self) -> None:
        """Clear the open streamed block: a transport retry is about to
        restream the reply from scratch, so the abandoned generation's tokens
        must not remain in front of it."""
        with self._lock:
            # A trace/progress row can close the live block mid-response. Its
            # text still belongs to the abandoned attempt and must be cleared.
            for index in self._response_assistant_indices:
                role, _current = self.entries[index]
                self.entries[index] = (role, "")
        self._touch()

    def finish_assistant(self, text: str = "") -> None:
        with self._lock:
            self._close_reasoning_locked()
            if len(self._response_assistant_indices) > 1:
                combined = "".join(self.entries[i][1] for i in self._response_assistant_indices)
                if not text:
                    text = combined
                if _normalize_visible(text).startswith(_normalize_visible(combined)):
                    # Background trace rows can split one response into several
                    # blocks. Reconcile its whole text before adding final-only
                    # notices, retaining the trace rows and their metadata.
                    first = self._response_assistant_indices[0]
                    self.entries[first] = ("assistant", combined)
                    for index in reversed(self._response_assistant_indices[1:]):
                        self._remove_entry_locked(index)
                    self._assistant_index = first
            if self._assistant_index is not None:
                role, current = self.entries[self._assistant_index]
                if not current and text:
                    self.entries[self._assistant_index] = (role, text)
                elif current and text:
                    cur_norm = _normalize_visible(current)
                    txt_norm = _normalize_visible(text)
                    boundary_index = len(current) - len(text)
                    glued_boundary = (
                        current.endswith(text)
                        and boundary_index > 0
                        and not current[boundary_index - 1].isspace()
                    )
                    if txt_norm.startswith(cur_norm):
                        # Finalization can append verification notices or
                        # checkpoint links after the provider stops streaming.
                        self.entries[self._assistant_index] = (role, text)
                    elif (
                        cur_norm != txt_norm
                        and cur_norm.endswith(txt_norm)
                        and len(txt_norm) * 4 < len(cur_norm)
                        and glued_boundary
                    ):
                        # A separate closing message streamed into the same
                        # open block with no boundary, gluing "…(exit code 5)The
                        # fix is done." into one paragraph. The final text marks
                        # the exact boundary — restore the paragraph break there.
                        # A WHITESPACE boundary is the tail-fragment-final case
                        # instead (the final restates the streamed ending, e.g.
                        # "…Finally, run the tests." + "run the tests.") and the
                        # streamed block stays untouched.
                        prefix = current[:boundary_index].rstrip()
                        if prefix:
                            self.entries[self._assistant_index] = (
                                role,
                                f"{prefix}\n\n{text}",
                            )
                    elif (
                        cur_norm != txt_norm
                        and cur_norm.endswith(txt_norm)
                        and len(txt_norm) * 4 >= len(cur_norm)
                    ):
                        # A provider-level retry restreamed the reply into the
                        # same live block: a mid-stream failure abandons
                        # generation A after its tokens are already visible,
                        # the transport retry re-runs the request, and
                        # generation B streams into the same open block — the
                        # block ends up "A + B" (wordings can differ, so the
                        # verbatim dedupe below never catches it). The final
                        # text is authoritative — it is exactly what the
                        # session history keeps — so when the block ends with
                        # it and carries an abandoned prefix, snap to the
                        # final. The >=25% length guard keeps a pathological
                        # tail-fragment final from truncating a legit block.
                        self.entries[self._assistant_index] = (role, text)
                # The response may have repeated an earlier answer in this same
                # turn. Remove its live block once complete; the earlier answer
                # remains visible, including any intervening tool or trace rows.
                _, completed = self.entries[self._assistant_index]
                previous_index = self._last_turn_assistant_index_locked(
                    before_index=self._assistant_index
                )
                if previous_index is not None:
                    previous = self.entries[previous_index][1]
                    if _normalize_visible(completed).startswith(_normalize_visible(previous)):
                        # A final can repeat a candidate and add a host notice,
                        # or restate a token-limited answer before continuing.
                        self.entries[previous_index] = (role, completed)
                        self._remove_entry_locked(self._assistant_index)
            elif text.strip():
                # A multi-step turn can re-emit the same answer after its live
                # block was already closed by an intervening tool/trace line (which
                # reset _assistant_index). Only append a fresh block when it is NOT
                # a verbatim repeat of the turn's most recent assistant block —
                # otherwise the same reply stacks up 2-3 times. Content-based and
                # provider-agnostic (see also the render-layer collapse in app.py).
                last_index = self._last_turn_assistant_index_locked()
                if last_index is not None and _normalize_visible(text).startswith(
                    _normalize_visible(self.entries[last_index][1])
                ):
                    self.entries[last_index] = ("assistant", text)
                else:
                    self.entries.append(("assistant", text))
            self._assistant_index = None
            self._response_assistant_indices.clear()
            self._status = None
        self._touch()

    def _remove_entry_locked(self, index: int) -> None:
        """Remove a duplicate without shifting reasoning metadata onto other rows."""
        self.entries.pop(index)
        for metadata in (self._reasoning_secs, self._reasoning_block_ids):
            shifted = {
                key - (key > index): value for key, value in metadata.items() if key != index
            }
            metadata.clear()
            metadata.update(shifted)
        if self._reasoning_index is not None and self._reasoning_index > index:
            self._reasoning_index -= 1

    def _last_turn_assistant_index_locked(self, *, before_index: int | None = None) -> int | None:
        """Most recent nonblank assistant entry before the user boundary."""
        end = len(self.entries) if before_index is None else before_index
        for index in range(end - 1, -1, -1):
            role, text = self.entries[index]
            if role == "user":
                return None
            if role == "assistant" and text.strip():
                return index
        return None

    # ---- transient status (working line) ----
    def set_status(self, status: str | None) -> None:
        with self._lock:
            self._status = (status or None) if status is None else (status.strip() or None)
        self._touch()

    @property
    def status(self) -> str | None:
        with self._lock:
            return self._status

    # ---- live Forge execution view ----
    def forge_begin(self, run_id: str, tasks: list[tuple[str, str, str]]) -> None:
        """Open the live Forge execution view with the initial task list.

        ``tasks`` is a list of ``(id, title, status)``; statuses then refresh as
        the swarm runs (``forge_update_statuses``) and on completion
        (``forge_finish``)."""
        with self._lock:
            self._forge = {
                "run_id": str(run_id or ""),
                "tasks": [
                    {"id": str(tid), "title": str(title), "status": str(status or "planned")}
                    for tid, title, status in tasks
                ],
                "active": None,
                "active_map": {},
                "phase": "execute",
                "message": "Starting…",
                "started": time.monotonic(),
                "done": False,
                "ok": True,
            }
        self._touch()

    def _forge_reconcile_active_map_locked(self) -> None:
        """Keep ``active_map`` in step with the current task statuses.

        Adds a per-task start stamp when a task enters a running state (so a
        status-driven run — not just an event-driven one — gets a live timer),
        and prunes any task whose status has turned terminal so a finished row
        stops spinning. Caller holds the lock."""
        if self._forge is None:
            return
        active_map: dict = self._forge["active_map"]  # type: ignore[assignment]
        for task in self._forge["tasks"]:  # type: ignore[index]
            tid = task["id"]
            status = str(task.get("status") or "")
            if forge_status_is_terminal(status):
                active_map.pop(tid, None)
            elif forge_status_bucket(status) == "running" and tid not in active_map:
                active_map[tid] = {"started": time.monotonic(), "phase": "", "message": ""}

    def forge_update_statuses(self, statuses: dict[str, str]) -> None:
        """Update task statuses (keyed by task id) from the latest plan snapshot."""
        with self._lock:
            if self._forge is None:
                return
            for task in self._forge["tasks"]:  # type: ignore[index]
                new_status = statuses.get(task["id"])
                if new_status:
                    task["status"] = str(new_status)
            self._forge_reconcile_active_map_locked()
        self._touch()

    def forge_sync_tasks(self, tasks: list[tuple[str, str, str]]) -> None:
        """Reconcile the live view with the plan on disk: update existing task
        statuses AND append any new tasks (e.g. ones plan enrichment added mid-run),
        preserving order so the table never silently drops a real task."""
        with self._lock:
            if self._forge is None:
                return
            existing = {task["id"]: task for task in self._forge["tasks"]}  # type: ignore[index]
            for tid, title, status in tasks:
                row = existing.get(str(tid))
                if row is not None:
                    if status:
                        row["status"] = str(status)
                else:
                    new_row = {
                        "id": str(tid),
                        "title": str(title),
                        "status": str(status or "planned"),
                    }
                    self._forge["tasks"].append(new_row)  # type: ignore[union-attr]
                    existing[str(tid)] = new_row
            self._forge_reconcile_active_map_locked()
        self._touch()

    def forge_set_active(self, task_id: str | None, phase: str = "", message: str = "") -> None:
        """Mark the task the swarm is currently working on, plus the phase/message
        shown on the live status line."""
        with self._lock:
            if self._forge is None:
                return
            if task_id is not None:
                tid = str(task_id)
                self._forge["active"] = tid
                # Upsert into the live worker set: a first sighting stamps its
                # start (for the per-task timer); later events only refresh the
                # phase/message. A terminal task is never (re)added — a late event
                # after completion must not resurrect a finished row.
                active_map: dict = self._forge["active_map"]  # type: ignore[assignment]
                if not forge_status_is_terminal(self._forge_task_status_locked(tid)):
                    entry = active_map.get(tid)
                    if entry is None:
                        entry = {"started": time.monotonic(), "phase": "", "message": ""}
                        active_map[tid] = entry
                    if phase:
                        entry["phase"] = str(phase)
                    if message:
                        entry["message"] = str(message)
            if phase:
                self._forge["phase"] = str(phase)
            if message:
                self._forge["message"] = str(message)
        self._touch()

    def _forge_task_status_locked(self, task_id: str) -> str:
        """Current status of ``task_id`` in the live view (''  if unknown).
        Caller holds the lock."""
        if self._forge is None:
            return ""
        for task in self._forge["tasks"]:  # type: ignore[index]
            if task["id"] == task_id:
                return str(task.get("status") or "")
        return ""

    def forge_finish(self, statuses: dict[str, str], summary: str = "", ok: bool = True) -> None:
        """Apply final task statuses and mark the run done (table stays visible).

        ``ok`` is the overall outcome (no failed/remaining tasks) — it colours the
        summary line green vs red so it never contradicts the per-task glyphs."""
        with self._lock:
            if self._forge is None:
                return
            for task in self._forge["tasks"]:  # type: ignore[index]
                new_status = statuses.get(task["id"])
                if new_status:
                    task["status"] = str(new_status)
            self._forge["active"] = None
            self._forge["active_map"] = {}  # run over: no live workers
            self._forge["done"] = True
            self._forge["ok"] = bool(ok)
            self._forge["phase"] = "done"
            if summary:
                self._forge["message"] = str(summary)
        self._touch()

    def forge_clear(self) -> None:
        with self._lock:
            self._forge = None
        self._touch()

    def forge_snapshot(self) -> dict[str, object] | None:
        """Return a copy of the live Forge view, or ``None`` when inactive."""
        with self._lock:
            if self._forge is None:
                return None
            return {
                "run_id": self._forge["run_id"],
                "tasks": [dict(task) for task in self._forge["tasks"]],  # type: ignore[union-attr]
                "active": self._forge["active"],
                "active_map": {
                    tid: dict(info)
                    for tid, info in self._forge["active_map"].items()  # type: ignore[union-attr]
                },
                "phase": self._forge["phase"],
                "message": self._forge["message"],
                "started": self._forge["started"],
                "done": self._forge["done"],
                "ok": self._forge.get("ok", True),
            }

    def clear(self) -> None:
        with self._lock:
            self.entries.clear()
            self._assistant_index = None
            self._response_assistant_indices.clear()
            self._reasoning_index = None
            self._reasoning_start = None
            self._reasoning_block_id = None
            self._reasoning_block_ids.clear()
            self._reasoning_secs.clear()
            self._status = None
            self._forge = None
        self._touch()

    def load_history(self, messages: list[dict[str, object]]) -> None:
        """Replace the transcript with a resumed conversation's history.

        Clears every transient bit (like :meth:`clear`) and repopulates the pane
        from ``messages`` (the loaded history of a ``/resume`` target) so the user
        sees the prior conversation after switching sessions. Only ``user`` and
        ``assistant`` turns with text content are shown — tool calls/results are
        omitted so the reloaded view reads as the clean conversation. Because no
        assistant block stays "open" (``_assistant_index`` is reset), every
        assistant entry renders as a completed (markdown) reply.
        """
        with self._lock:
            self.entries.clear()
            self._assistant_index = None
            self._response_assistant_indices.clear()
            self._reasoning_index = None
            self._reasoning_start = None
            self._reasoning_block_id = None
            self._reasoning_block_ids.clear()
            self._reasoning_secs.clear()
            self._status = None
            self._forge = None
            for message in messages or []:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "").strip().lower()
                if role not in ("user", "assistant"):
                    continue
                content = message.get("content")
                if not isinstance(content, str):
                    continue
                text = content.rstrip()
                if not text:
                    continue
                self.entries.append((role, text))
        self._touch()

    def reasoning_snapshot(self) -> tuple[int | None, dict[int, int]]:
        """Return ``(live_reasoning_index, {entry_index: elapsed_seconds})``.

        ``live_reasoning_index`` is the still-streaming reasoning entry (rendered
        in full, dim); the map gives the duration of each closed block so the
        renderer can collapse it to a one-line "thought for Ns" summary.
        """
        with self._lock:
            return self._reasoning_index, dict(self._reasoning_secs)

    def reasoning_block_ids(self) -> dict[int, str]:
        """Return stable identifiers for reasoning transcript entries."""

        with self._lock:
            return dict(self._reasoning_block_ids)

    def snapshot(self) -> tuple[list[Entry], str | None, int | None]:
        """Return ``(entries, status, streaming_index)`` under the lock.

        ``streaming_index`` is the index of the still-streaming assistant entry
        (``None`` when no block is open), so the renderer keeps that one plain and
        only markdown-renders completed replies — read atomically here to avoid a
        race with a worker finishing the block mid-redraw.
        """
        with self._lock:
            return list(self.entries), self._status, self._assistant_index

    def last_response_text(self) -> str:
        """Copy the latest nonempty assistant source, without rendered UI chrome."""
        from .transcript_navigation import clean_response_text

        with self._lock:
            for role, text in reversed(self.entries):
                if role == "assistant" and (clean := clean_response_text(text)):
                    return clean
        return ""


__all__ = ["TuiTranscript", "Entry"]
