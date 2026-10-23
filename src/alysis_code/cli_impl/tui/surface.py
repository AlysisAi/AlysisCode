"""``TuiSurface`` — renders agent events into the full-screen TUI transcript.

The agent runtime drives surfaces through the legacy ``on_*`` callbacks (same
path ``RichSurface`` uses), calling them synchronously from whatever thread runs
``session.run_turn``. In the TUI that is a worker thread, so every handler just
mutates the thread-safe :class:`TuiTranscript` (which schedules a redraw); none
of them touch prompt_toolkit widgets directly.

Message/tool ``emit_*`` events are additive duplicates of the ``on_*`` calls and
are intentionally left undefined (defining no-op versions would double-render).
Error/warning/info are EITHER/OR in the runtime, so we provide real
``emit_error``/``emit_warning``/``emit_info`` handlers -- otherwise the
runtime's capability probe would drop them. Info events are persistent
transcript entries; progress updates remain ephemeral status text.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ...llm_error_display import friendly_llm_error_message
from ...subagent_labels import subagent_identity
from ...surface.types import (
    ApprovalDecision,
    ApprovalRequest,
    PatchEvent,
    StandingApproval,
    StatusEvent,
    SubagentEndEvent,
    SubagentStartEvent,
    ToolEndEvent,
    ToolOutputEvent,
    ToolStartEvent,
    approval_review_line,
    remote_site_outcome,
)
from ...user_questions import UserQuestionBroker
from .forge_status import FORGE_RUNNING_STATES, forge_status_counts
from .tool_trace import bounded_text, patch_entry, tool_is_view_only, tool_output
from .transcript import TuiTranscript

# Per-worker-thread cancellation token. The TUI sets this at the top of each turn
# worker; once the user soft-interrupts, that worker's token flips cancelled and
# every output handler below drops its events — so an abandoned turn (still blocked
# waiting on a slow model) can never paint into the transcript after the interrupt.
# It is thread-local, so a *new* turn on a fresh worker is unaffected.
_worker_ctx = threading.local()
_TRACE_LEVELS = frozenset({"off", "compact", "full"})


def _normalize_trace_level(value: object, *, fallback: str = "compact") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _TRACE_LEVELS else fallback


def set_active_cancellation(token: object | None) -> None:
    _worker_ctx.token = token


def _worker_cancelled() -> bool:
    token = getattr(_worker_ctx, "token", None)
    return bool(token is not None and getattr(token, "is_cancelled", False))


def _tool_label(name: str) -> str:
    try:
        from ...tools.registry import tool_display_name

        label = tool_display_name(name)
        return f"{label} (view only)" if tool_is_view_only(name) else label
    except Exception:
        return name


def _tool_detail(name: str, args: object) -> str:
    """Short argument preview (search query, fetched URL, file path) so repeated
    lines of the same tool stay distinguishable in the transcript. Shell-category
    tools are excluded: full command lines can carry secrets and are not shown at
    the default trace level."""
    try:
        from ...tools.registry import get_builtin_tool_metadata, tool_input_preview

        spec = get_builtin_tool_metadata(name)
        if spec is not None and "shell" in spec.categories:
            return ""
        detail = tool_input_preview(name, args if isinstance(args, dict) else {})
    except Exception:
        return ""
    clean = " ".join(str(detail or "").split())
    if clean in {"", "-"}:
        return ""
    if len(clean) > 64:
        clean = clean[:63] + "…"
    return clean


def _format_ms(elapsed_ms: int) -> str:
    try:
        elapsed_ms = int(elapsed_ms)
    except (TypeError, ValueError):
        return ""
    if elapsed_ms < 1000:
        return f"{elapsed_ms}ms"
    return f"{elapsed_ms / 1000.0:.1f}s"


def _truncate(text: str, *, limit: int = 160) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def _unavailable_detail(meta: dict[str, object]) -> str:
    cause = str(meta.get("unavailable_reason") or "")
    return f": {_truncate(cause)}" if cause else ""


class TuiSurface:
    """Surface implementation backed by a :class:`TuiTranscript`."""

    renders_error_panel = True

    def __init__(
        self,
        transcript: TuiTranscript,
        *,
        request_approval_ui: Callable[[ApprovalRequest], ApprovalDecision] | None = None,
        on_hud_refresh: Callable[[], None] | None = None,
        on_subagent_activity: Callable[[str | None], None] | None = None,
        on_subagent_activities: Callable[[tuple[str, ...]], None] | None = None,
        on_subagent_run_started: Callable[[SubagentStartEvent], None] | None = None,
        before_visible_output: Callable[[], None] | None = None,
        on_steer_messages_restored: Callable[[list[str]], None] | None = None,
    ) -> None:
        self._t = transcript
        self.user_questions = UserQuestionBroker(
            is_cancelled=_worker_cancelled, on_timeout=self._on_question_timeout
        )
        self._approval_ui = request_approval_ui
        self._trace_level = "compact"
        # Session approval grants. The classic Rich surface has
        # kept these since the beginning; the TUI surface never had a store,
        # so the modal's "[a] always" decision was returned upstream and then
        # forgotten - every identical approval prompted again.
        self._session_scope_grants: set[str] = set()
        self._session_dir_grants: set[tuple[str, str]] = set()
        # Cumulative milliseconds spent blocked on approval prompts. The
        # turn loop snapshots this around each tool call and subtracts the
        # delta, so human decision time stops being reported as tool runtime.
        self.approval_wait_ms_total = 0
        # Argument previews captured at tool start, keyed by call id, so the
        # committed "✓ …" line can show what the tool actually worked on.
        self._tool_details: dict[str, str] = {}
        # Name of the tool whose "✓/↳" line was committed last, so consecutive
        # runs of the same tool group as continuation rows instead of repeating
        # the tool name (e.g. four "Search Web" lines).
        self._last_tool_trace_name: str | None = None
        # Live footer HUD (context/tokens/cost) refresh. The agent runtime drives
        # the surface from the SAME worker thread that runs ``run_turn``, so this
        # callback reads session state at quiescent points (tool-end / message-done)
        # with no concurrent mutation. Throttled so a tool-heavy turn does not
        # re-estimate the whole transcript on every step. Without it the footer would
        # only update once the entire (possibly long, multi-step) turn completes,
        # leaving the bottom-right numbers frozen while the agent is visibly working.
        self._hud_refresh = on_hud_refresh
        self._hud_last_refresh: float = 0.0
        # Live Forge execution context (set for the duration of /execute plan): the
        # run paths (to refresh task statuses from plan.json), a debounce stamp, the
        # worker cancellation token, and the run id (to drop stale/superseded events).
        self._forge_paths: object | None = None
        self._forge_last_refresh: float = 0.0
        self._forge_token: object | None = None
        self._forge_run_id: str = ""
        # Live subagent identity. The stack tracks concurrent subagent runs
        # (start pushes, end pops); the callback tells the app which name to pin
        # in the footer badge — None clears it. Entries are keyed by the pushing
        # THREAD as well as the name: a run's start and end always fire on the
        # same thread (the tool's run() is synchronous), while a stale end from
        # an abandoned turn arrives on a thread that no longer owns an entry —
        # so it can never pop a same-named agent the next turn just started.
        # Cleared at every turn boundary and on soft-interrupt so an interrupted
        # run can never leave a stale badge.
        self._on_subagent_activity = on_subagent_activity
        self._on_subagent_activities = on_subagent_activities
        self._on_subagent_run_started = on_subagent_run_started
        self._before_visible_output = before_visible_output
        self._on_steer_messages_restored = on_steer_messages_restored
        self._subagent_stack: list[tuple[int, str, str, str]] = []
        self._subagent_activity: dict[str, str] = {}

    def _on_question_timeout(self, result: dict) -> None:
        kept = len(result["answers"])
        detail = (
            f"Keeping {kept} confirmed answer{'s' if kept != 1 else ''} and skipping the rest."
            if kept
            else "Skipping unanswered questions."
        )
        self._t.append(
            "info",
            f"Question timer expired after {result['timeout_seconds']:g}s of inactivity. "
            f"{detail} Continuing…",
        )

    @property
    def trace_level(self) -> str:
        return self._trace_level

    @property
    def reasoning_trace_enabled(self) -> bool:
        """Provider reasoning is displayed only while trace is visible."""

        return self._trace_level != "off"

    def set_trace_level(self, level: str) -> str:
        self._trace_level = _normalize_trace_level(level, fallback=self._trace_level)
        self._t.set_trace_level(self._trace_level)
        if self._trace_level == "off":
            # A level change is display-only: close already-visible transient
            # reasoning/status without touching the model request or transcript
            # accounting state.
            self._t.end_reasoning()
            self._t.set_status(None)
        return self._trace_level

    def _maybe_refresh_hud(self) -> None:
        """Refresh the footer HUD mid-turn (throttled), so context/tokens/cost tick
        up while a multi-step turn runs instead of only at the end. Runs on the
        worker thread; the callback (the caller's end-of-turn refresher) mutates the
        shared ``TuiState`` and the next transcript redraw repaints the footer."""
        fn = self._hud_refresh
        if fn is None or _worker_cancelled():
            return
        import time

        now = time.monotonic()
        if now - self._hud_last_refresh < 0.4:
            return
        self._hud_last_refresh = now
        try:
            fn()
        except Exception:
            pass

    def _prepare_visible_output(self) -> None:
        callback = self._before_visible_output
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass

    def on_steer_messages_restored(self, messages: list[str]) -> None:
        """Reconcile TUI metadata when the core rolls drained steers back."""

        if _worker_cancelled():
            return
        callback = self._on_steer_messages_restored
        if callback is None:
            return
        try:
            callback(list(messages))
        except Exception:
            pass

    # ------------------------------------------------------------------ content
    def on_user_message(self, text: str) -> None:
        # The app echoes the user's line instantly on submit; here we just open a
        # fresh assistant block for the turn that is starting.
        self._t.begin_turn()
        # Turn boundary: drop argument previews stranded by an interrupted turn
        # (a cancelled turn's on_tool_end returns before popping its entry).
        self._tool_details.clear()
        # And drop any subagent identity stranded the same way, so the footer
        # badge can never claim a subagent from an abandoned turn is still active.
        self.clear_subagent_activity()

    def on_assistant_token(self, delta: str) -> None:
        if _worker_cancelled():
            return
        self._prepare_visible_output()
        self._t.stream_assistant(delta)

    def on_model_response_start(self) -> None:
        if _worker_cancelled():
            return
        self._t.begin_assistant_response()

    def on_reasoning_start(self, block_id: str) -> None:
        """Open one provider-call-scoped reasoning block."""

        if _worker_cancelled() or self._trace_level == "off":
            return
        self._prepare_visible_output()
        self._t.begin_reasoning(block_id)

    def on_reasoning_token(self, delta: str) -> None:
        # Provider adapters route returned reasoning text and summaries here;
        # encrypted state, signatures, and redacted blocks stay out of the display.
        if _worker_cancelled() or self._trace_level == "off":
            return
        self._prepare_visible_output()
        self._t.stream_reasoning(delta)

    def on_reasoning_end(self, block_id: str) -> None:
        if _worker_cancelled():
            return
        self._t.end_reasoning(block_id)

    def reset_streamed_assistant(self) -> None:
        # Transport retry restreams the reply; wipe the live block so the
        # abandoned generation never shows doubled (see core._on_stream_restart).
        self._t.restart_assistant()

    def on_assistant_message_done(self, text: str) -> None:
        if _worker_cancelled():
            return
        self._prepare_visible_output()
        self._t.finish_assistant(text or "")
        # A model response just landed (usage recorded) — refresh the live HUD so a
        # multi-step turn's bottom-right numbers advance, not just at the very end.
        self._maybe_refresh_hud()

    def on_progress_update(self, message: str) -> None:
        if _worker_cancelled() or self._trace_level == "off":
            return
        self._prepare_visible_output()
        self._t.set_status(_truncate(message, limit=80) or None)

    def on_status_update(self, status: StatusEvent) -> None:
        # Static workspace/model/mode line — the footer already carries this.
        return

    # ------------------------------------------------------------- Forge swarm
    def begin_forge(self, paths: object, token: object | None = None) -> None:
        """Open the live Forge execution view from the current plan on disk.

        Called by the worker just before ``run_swarm`` so the task table shows the
        initial statuses; the swarm then refreshes them through
        :meth:`on_swarm_event` and the final pass in :meth:`end_forge`. ``token`` is
        the worker's cancellation token — checked in :meth:`on_swarm_event` (which
        runs on the trace sink's OWN thread, where the thread-local
        ``_worker_cancelled`` would not see it) so a soft-interrupt stops painting."""
        self._forge_paths = paths
        self._forge_token = token
        self._forge_last_refresh = 0.0
        self._forge_run_id = str(getattr(paths, "run_id", "") or "")
        self._t.forge_begin(self._forge_run_id, list(self._read_plan_tasks()))

    def _forge_run_cancelled(self) -> bool:
        token = self._forge_token
        return bool(token is not None and getattr(token, "is_cancelled", False))

    def on_swarm_event(self, event: object) -> None:
        """Receive a structured swarm trace event (preferred by the serialized
        sink over :meth:`on_progress_update`): stream it as a trace/error line,
        highlight the in-flight task, and refresh statuses from plan.json.

        Runs on the trace sink's daemon thread, so it guards on the worker's token
        (not the thread-local cancel flag), on the view still being open (a new turn
        clears it), and on the event's run id matching — so a soft-interrupted or
        superseded run can never paint stale progress."""
        if self._forge_run_cancelled():
            return
        if self._t.forge_snapshot() is None:
            return  # the view was cleared (e.g. a new turn started)
        event_run_id = str(getattr(event, "run_id", "") or "")
        if event_run_id and self._forge_run_id and event_run_id != self._forge_run_id:
            return  # stale event from a previous run
        phase = str(getattr(event, "phase", "") or "")
        message = str(getattr(event, "message", "") or "")
        task_id = getattr(event, "task_id", None)
        task_id = str(task_id) if task_id else None
        # Keep the run CLEAN: the live task table + the single moving phase line tell
        # the story, so progress events are NOT spilled as a wall of trace lines.
        # Only errors are surfaced as persistent lines (they matter and are rare).
        if "error" in phase.lower():
            prefix = f"[{task_id}] " if task_id else ""
            self._t.append(
                "error", f"✗ {_truncate(f'{prefix}{message}'.strip() or phase, limit=200)}"
            )
        self._t.forge_set_active(task_id, phase=phase, message=message)
        self._maybe_refresh_forge_statuses()

    def end_forge(self, summary: str = "", *, paths: object | None = None) -> None:
        """Apply the final task statuses from disk (adding any tasks enrichment added
        mid-run) and freeze the view as done, with a concise outcome summary.

        ``paths`` re-arms the run paths for a late finalize: a soft-interrupt
        clears ``self._forge_paths`` (via :meth:`interrupt_forge`) even though
        the swarm cannot actually be stopped — when it then runs to its natural
        end, the worker passes the run's paths back so the frozen table shows
        the statuses the finished swarm really wrote instead of an empty view."""
        if paths is not None:
            self._forge_paths = paths
        tasks = self._read_plan_tasks()
        self._t.forge_sync_tasks(tasks)
        done, failed, remaining = forge_status_counts(status for _tid, _title, status in tasks)
        ok = failed == 0 and remaining == 0
        if not summary:
            if not tasks:
                summary = "No execution-ready tasks."
            else:
                parts = [f"{done} done"]
                if failed:
                    parts.append(f"{failed} failed")
                if remaining:
                    parts.append(f"{remaining} remaining")
                summary = "Done · " + " · ".join(parts) if ok else "Finished · " + " · ".join(parts)
        self._t.forge_finish({tid: status for tid, _title, status in tasks}, summary, ok=ok)
        self._forge_paths = None
        self._forge_token = None
        self._forge_run_id = ""

    def interrupt_forge(self, summary: str = "Interrupted.") -> None:
        """Mark any live in-progress Forge tasks interrupted and freeze the view."""
        path = getattr(self._forge_paths, "plan_json_path", None)
        updated: dict[str, str] = {}
        if path is not None:
            try:
                import json
                from datetime import datetime, timezone

                data = json.loads(path.read_text(encoding="utf-8"))
                changed = False
                for task in data.get("tasks") or []:
                    if not isinstance(task, dict):
                        continue
                    status = str(task.get("status") or "").strip().lower()
                    task_id = str(task.get("id") or "").strip()
                    if not task_id or status not in FORGE_RUNNING_STATES:
                        continue
                    task["status"] = "interrupted"
                    task["last_error"] = "Interrupted by user."
                    updated[task_id] = "interrupted"
                    changed = True
                if changed:
                    data["updated_at"] = datetime.now(timezone.utc).isoformat()
                    path.write_text(
                        json.dumps(data, indent=2, sort_keys=False) + "\n",
                        encoding="utf-8",
                    )
            except Exception:
                updated = {}
        tasks = self._read_plan_tasks()
        if tasks:
            self._t.forge_sync_tasks(tasks)
            status_map = {tid: status for tid, _title, status in tasks}
        else:
            status_map = dict(updated)
            if status_map:
                self._t.forge_update_statuses(status_map)
        self._t.forge_finish(status_map, summary, ok=False)
        self._forge_paths = None
        self._forge_token = None
        self._forge_run_id = ""

    def append_system(self, text: str) -> None:
        """Append captured handler output (warnings / summary) as one system block."""
        clean = str(text or "").rstrip()
        if clean:
            self._t.append("system", clean)

    def append_note(self, text: str, *, role: str = "system") -> None:
        """Append a single status line with an explicit role.

        Used for the ``/resume`` outcome line so the caller can pick a role that
        flips the welcome→chat pane when needed (a resumed session with no visible
        history leaves the transcript empty, where a ``system`` line stays hidden
        behind the landing screen but an ``assistant`` line surfaces it)."""
        clean = str(text or "").rstrip()
        if clean:
            self._t.append(str(role or "system"), clean)

    def replace_history(self, messages: list[dict[str, object]]) -> None:
        """Reload the transcript with a resumed conversation's history.

        Called (on the UI thread, no turn in flight) after ``/resume`` swaps the
        live session: the prior conversation's user/assistant turns replace the
        current pane so the user actually "enters" the resumed session instead of
        keeping the previous transcript. Tool calls/results are dropped by
        :meth:`TuiTranscript.load_history` so the view stays clean."""
        self._t.load_history(messages or [])

    def _maybe_refresh_forge_statuses(self) -> None:
        import time

        now = time.monotonic()
        if now - self._forge_last_refresh < 0.4:
            return
        self._forge_last_refresh = now
        tasks = self._read_plan_tasks()
        if tasks:
            # Sync (not just update) so tasks added by plan enrichment mid-run show up.
            self._t.forge_sync_tasks(tasks)

    def _read_plan_tasks(self) -> list[tuple[str, str, str]]:
        """Read ``(id, title, status)`` for each task from plan.json (best-effort).

        Reads the raw JSON rather than the validating ``load_plan`` so a mid-run
        refresh stays cheap and never raises while the swarm rewrites the file."""
        paths = self._forge_paths
        path = getattr(paths, "plan_json_path", None)
        if path is None:
            return []
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        out: list[tuple[str, str, str]] = []
        for task in data.get("tasks") or []:
            if isinstance(task, dict) and task.get("id"):
                out.append(
                    (
                        str(task["id"]),
                        str(task.get("title") or ""),
                        str(task.get("status") or "planned"),
                    )
                )
        return out

    # -------------------------------------------------------------------- tools
    def on_tool_start(self, event: ToolStartEvent) -> None:
        # No "⚙ start" line — the live activity indicator under the question shows
        # the running tool (with a spinner + elapsed); it commits to a "✓ …" line
        # on completion. Collapse any open thinking first.
        if _worker_cancelled():
            return
        self._prepare_visible_output()
        label = _tool_label(event.name)
        arg_detail = _tool_detail(event.name, event.args)
        self._t.end_reasoning()
        if self._trace_level == "off":
            self._t.set_status("Working…")
            return
        if arg_detail and event.tool_call_id:
            # Stale-entry backstop: evict oldest first, never in-flight wholesale.
            # Stored for nested calls too, so a nested FAILURE's ✗ line can say
            # which invocation failed (its success line is suppressed anyway).
            while len(self._tool_details) > 256:
                self._tool_details.pop(next(iter(self._tool_details)))
            self._tool_details[event.tool_call_id] = arg_detail
        if event.subagent_name and self._trace_level != "full":
            # A nested subagent's tool call: the entry line already said who is
            # working and on what, so its step-by-step chatter stays out of the
            # transcript — the live status alone shows what the agent is doing
            # right now. Trace "full" opts back into the committed lines.
            detail = f" · {arg_detail}" if arg_detail else ""
            display_name = self._current_subagent_display_name(str(event.subagent_name))
            followed_label = self._current_subagent_follow_label(str(event.subagent_name))
            activity = _truncate(f"{label}{detail}", limit=64)
            self._subagent_activity[display_name] = activity
            self._t.set_status(_truncate(f"↪ {followed_label} ▸ {activity}…", limit=80))
            return
        if self._trace_level == "full":
            detail = f" · {arg_detail}" if arg_detail else ""
            who = f"{event.subagent_name} ▸ " if event.subagent_name else ""
            self._t.append("trace", f"▸ {who}{label}{detail}")
            if isinstance(event.args, dict):
                command = event.args.get("cmd") or event.args.get("command")
                if isinstance(command, str) and command:
                    self._t.append("tool_output", bounded_text(f"$ {command}"))
            self._last_tool_trace_name = None
        # Keep the transient status short so it stays on one row on narrow
        # terminals; the committed "✓" line carries the fuller preview.
        status_detail = arg_detail if len(arg_detail) <= 40 else arg_detail[:39] + "…"
        self._t.set_status(f"{label} · {status_detail}…" if status_detail else f"{label}…")

    def on_tool_output(self, event: ToolOutputEvent) -> None:
        if _worker_cancelled() or self._trace_level != "full":
            return
        detail = tool_output(event.chunk)
        if detail:
            self._prepare_visible_output()
            who = f"{event.subagent_name} ▸ " if event.subagent_name else ""
            heading = f"{who}{_tool_label(event.name)} output"
            if tool_is_view_only(event.name):
                label = _tool_label(event.name).removesuffix(" (view only)")
                heading = f"VIEWING ONLY · {who}{label} · no file changes"
            self._t.append("tool_output", f"{heading}\n{detail}")

    def on_tool_end(self, event: ToolEndEvent) -> None:
        # Pop before the cancellation check so an interrupted turn cannot strand
        # the entry (and a reused call id can never show a stale preview).
        arg_detail = self._tool_details.pop(event.tool_call_id, "")
        if _worker_cancelled():
            return
        self._prepare_visible_output()
        label = _tool_label(event.name)
        if arg_detail:
            label = f"{label} · {arg_detail}"
        if event.subagent_name and self._trace_level == "full":
            label = f"{event.subagent_name} ▸ {label}"
        elapsed = _format_ms(event.elapsed_ms)
        suffix = f" ({elapsed})" if elapsed else ""
        # Disclose approval-wait separately instead of billing it to the tool.
        approval_wait_ms = int(getattr(event, "approval_wait_ms", 0) or 0)
        if approval_wait_ms > 0:
            waited = _format_ms(approval_wait_ms)
            suffix = f" ({elapsed} · approval {waited})" if elapsed else f" (approval {waited})"
        meta = event.meta if isinstance(event.meta, dict) else {}
        # A withdrawn optional tool reports "done" to the model but never ran.
        unavailable = event.status == "done" and bool(meta.get("tool_unavailable"))
        # A page the remote site refused, stalled, or dropped is an outcome of that
        # source, not an agent failure: a neutral "◦" trace line, never a red ✗.
        site_reason = remote_site_outcome(meta) if event.status != "done" else ""
        if event.subagent_name and self._trace_level != "full":
            # Nested subagent steps stay out of the transcript (minimal view) —
            # except failures and withdrawn tools, which are real signal and keep
            # their line, attributed to the agent that hit them. Successes and
            # remote-site outcomes just roll the live status until the ↩ end line
            # closes the run.
            display_name = self._current_subagent_display_name(str(event.subagent_name))
            followed_label = self._current_subagent_follow_label(str(event.subagent_name))
            if unavailable:
                self._t.append(
                    "warn",
                    f"⚠ {display_name} ▸ {label} unavailable{suffix}{_unavailable_detail(meta)}",
                )
            elif event.status != "done" and not site_reason:
                verdict = "approval declined" if meta.get("approval_declined") else "failed"
                err = str(meta.get("error") or "")
                detail = f": {_truncate(err)}" if err else ""
                self._t.append("error", f"✗ {display_name} ▸ {label} {verdict}{suffix}{detail}")
            if self._trace_level == "off":
                # Off is quiet everywhere else — no named identity in the status
                # (mirrors on_subagent_start's off branch and the pre-feature
                # None-after-every-tool-end behavior).
                self._t.set_status(None)
            else:
                if unavailable:
                    outcome = "unavailable"
                elif event.status == "done":
                    outcome = "complete"
                else:
                    outcome = site_reason or "failed"
                activity = _truncate(f"{label} · {outcome}", limit=64)
                self._subagent_activity[display_name] = activity
                self._t.set_status(f"↪ {followed_label} · {activity}…")
            self._maybe_refresh_hud()
            return
        if unavailable:
            self._t.append("warn", f"⚠ {label} unavailable{suffix}{_unavailable_detail(meta)}")
        elif event.status == "done":
            if self._trace_level != "off":
                # Consecutive successes of the SAME tool group under one header:
                #   ✓ Search Web · current date today (1.5s)
                #     ↳ today's date (352ms)
                # The adjacency check keeps grouping honest — anything else appended
                # in between (a message, an error, another tool) restarts a full line.
                entries = self._t.entries
                grouped = (
                    arg_detail != ""
                    and self._last_tool_trace_name == event.name
                    and bool(entries)
                    and entries[-1][0] == "trace"
                    and (entries[-1][1].startswith("✓") or entries[-1][1].startswith("  ↳"))
                )
                if grouped:
                    read_only = " (view only)" if tool_is_view_only(event.name) else ""
                    self._t.append("trace", f"  ↳ {arg_detail}{read_only}{suffix}")
                else:
                    self._t.append("trace", f"✓ {label}{suffix}")
                self._last_tool_trace_name = event.name
        elif meta.get("approval_declined"):
            err = str(meta.get("error") or "")
            detail = f": {_truncate(err)}" if err else ""
            self._t.append("error", f"✗ {label} approval declined{suffix}{detail}")
        elif site_reason:
            if self._trace_level != "off":
                self._t.append("trace", f"◦ {label} · {site_reason}{suffix}")
        else:
            err = str(meta.get("error") or "")
            detail = f": {_truncate(err)}" if err else ""
            self._t.append("error", f"✗ {label} failed{suffix}{detail}")
        self._t.set_status(None)
        # Between steps (no concurrent message mutation on this worker thread) is a
        # safe point to advance the footer HUD as the turn progresses.
        self._maybe_refresh_hud()

    # --------------------------------------------------------------- subagents
    def _sync_subagent_badge(self) -> None:
        """Tell the app which live subagents to pin in the footer."""
        names = tuple(
            self._subagent_display_name(name, label, workspace_view)
            for _thread_id, name, label, workspace_view in self._subagent_stack
        )
        fn = self._on_subagent_activity
        if fn is not None:
            try:
                fn(names[-1] if names else None)
            except Exception:
                pass
        all_fn = self._on_subagent_activities
        if all_fn is not None:
            try:
                all_fn(names)
            except Exception:
                pass

    @staticmethod
    def _subagent_display_name(name: str, label: str, workspace_view: str) -> str:
        marker = "[iso] " if workspace_view in {"isolated", "pinned"} else ""
        return f"{marker}{subagent_identity(name, label)}"

    def _current_subagent_display_name(self, name: str) -> str:
        thread_id = threading.get_ident()
        for owner_thread, candidate, label, workspace_view in reversed(self._subagent_stack):
            if owner_thread == thread_id and candidate == name:
                return self._subagent_display_name(candidate, label, workspace_view)
        return name

    @staticmethod
    def _subagent_follow_label(name: str, label: str, workspace_view: str) -> str:
        marker = "[iso] " if workspace_view in {"isolated", "pinned"} else ""
        if label:
            return f"{marker}{name} · following {label}"
        return f"following {marker}{name}"

    def _current_subagent_follow_label(self, name: str) -> str:
        thread_id = threading.get_ident()
        for owner_thread, candidate, label, workspace_view in reversed(self._subagent_stack):
            if owner_thread == thread_id and candidate == name:
                return self._subagent_follow_label(candidate, label, workspace_view)
        return name

    def clear_subagent_activity(self) -> None:
        """Forget every live subagent and clear the footer badge.

        Called at turn boundaries and on soft-interrupt. After this, the
        abandoned runs' late end events find no matching stack entry and are
        dropped whole — they cannot pop, restyle, or re-pin anything the next
        turn owns."""
        if self._subagent_stack:
            self._subagent_stack.clear()
            self._sync_subagent_badge()
        self._subagent_activity.clear()

    def on_subagent_start(self, event: SubagentStartEvent) -> None:
        if _worker_cancelled():
            return
        self._prepare_visible_output()
        run_started = self._on_subagent_run_started
        if run_started is not None and event.subagent_run_id:
            try:
                run_started(event)
            except Exception:
                pass
        # The badge is identity, not trace — it shows at every trace level.
        name = str(event.name)
        label = str(event.label or "")
        workspace_view = str(event.workspace_view or "shared")
        self._subagent_stack.append((threading.get_ident(), name, label, workspace_view))
        self._sync_subagent_badge()
        display_name = self._subagent_display_name(name, label, workspace_view)
        followed_label = self._subagent_follow_label(name, label, workspace_view)
        self._subagent_activity[display_name] = "starting"
        if self._trace_level == "off":
            return
        # The spawn line carries stable identity only. Live activity is derived
        # from the child's actual tool events below, never from canned role text.
        self._t.append("subagent", f"↪ {display_name} · {event.mode}")
        self._t.set_status(f"↪ {followed_label} · starting…")

    def on_subagent_end(self, event: SubagentEndEvent) -> None:
        # A run's start and end fire on the same thread, so the end may only
        # unwind an entry this thread pushed. An end with no matching entry is
        # stale — an abandoned turn's agent finishing late, or a start this
        # surface never recorded — and must not touch the live turn's badge,
        # status, or transcript at all.
        entry = (
            threading.get_ident(),
            str(event.name),
            str(event.label or ""),
            str(event.workspace_view or "shared"),
        )
        matched = False
        for index in range(len(self._subagent_stack) - 1, -1, -1):
            if self._subagent_stack[index] == entry:
                del self._subagent_stack[index]
                matched = True
                break
        if not matched:
            return
        name = entry[1]
        display_name = self._subagent_display_name(name, entry[2], entry[3])
        if all(
            self._subagent_display_name(stacked_name, label, workspace_view) != display_name
            for _tid, stacked_name, label, workspace_view in self._subagent_stack
        ):
            self._subagent_activity.pop(display_name, None)
        # Unwind the badge even for a cancelled worker — the matched pop only
        # ever removes this thread's own entry, so re-syncing here can never
        # resurrect a name the interrupt already dropped (clear_subagent_activity
        # emptied the stack, making this pop unreachable for abandoned runs).
        self._sync_subagent_badge()
        if not self._subagent_stack:
            # The last live agent is gone — clear the activity line at every
            # trace level so an "↪ name · …" status can never outlive its run.
            self._t.set_status(None)
        if _worker_cancelled() or self._trace_level == "off":
            return
        self._prepare_visible_output()
        status = "finished" if event.status == "success" else event.status
        self._t.append(
            "trace",
            f"↩ {display_name} · {status} · {event.steps_completed} steps",
        )
        if self._subagent_stack:
            # Roll the activity line to a survivor so the status is never
            # attributed to an agent whose ↩ line just printed.
            _owner, top_name, top_label, top_view = self._subagent_stack[-1]
            top = self._subagent_display_name(top_name, top_label, top_view)
            followed_label = self._subagent_follow_label(
                top_name,
                top_label,
                top_view,
            )
            activity = self._subagent_activity.get(top, "starting")
            self._t.set_status(f"↪ {followed_label} · {activity}…")

    def on_patch_generated(self, event: PatchEvent) -> None:
        if _worker_cancelled() or self._trace_level == "off":
            return
        if event.subagent_name and self._trace_level != "full":
            return
        self._prepare_visible_output()
        self._t.append("patch", patch_entry(event))

    # ---------------------------------------------------------------- diagnostics
    def on_error(self, err: str) -> None:
        if _worker_cancelled():
            return
        self._prepare_visible_output()
        self._t.append("error", _truncate(friendly_llm_error_message(err), limit=480) or "error")
        self._t.set_status(None)

    def on_warning(self, warning: str) -> None:
        if _worker_cancelled():
            return
        self._prepare_visible_output()
        self._t.append("warn", _truncate(str(warning), limit=480) or "warning")

    # ------------------------------------------------------------------ approval
    def emit_approval_reviewed(
        self,
        outcome: str,
        subject: str,
        reason: str = "",
        *,
        risk: str = "",
        approval_kind: str = "",
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        if _worker_cancelled():
            return
        self._prepare_visible_output()
        line = _truncate(approval_review_line(outcome, subject, reason), limit=480)
        self._t.append("warn" if outcome == "deny" else "info", line)

    def standing_approval(self, request: ApprovalRequest) -> StandingApproval | None:
        """An earlier "[a] always" / "[d] always for this folder" already answers ``request``."""

        from ...approval_scope import (
            approval_session_scope_for_request,
            request_matches_dir_grants,
        )

        scope_info = approval_session_scope_for_request(request)
        if scope_info.key is not None and scope_info.key in self._session_scope_grants:
            return StandingApproval("allow", "you allowed this earlier in this session")
        if request_matches_dir_grants(request, self._session_dir_grants):
            return StandingApproval("allow", "you allowed this folder earlier in this session")
        return None

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        # Reaching this method already means the execution mode decided this
        # action needs a human: ``fullaccess`` returns before requesting approval
        # and ``readonly`` raises. So the surface never second-guesses the gate —
        # it prompts, or fails closed when it cannot. (There used to be an
        # auto-approve short-circuit here; it let a Shift+Tab silently turn
        # ``review`` into ``fullaccess`` while the footer still read "safe".)
        if _worker_cancelled():
            # The user interrupted this (now-abandoned) turn; deny without prompting.
            return ApprovalDecision(allow=False)
        # Session grants: an earlier "[a] always" (exact scope) or "[d]
        # always for this folder" decision auto-approves matching requests
        # without re-prompting. Sensitive approvals never match (their scope is
        # withheld and dir grants exclude allow_for_session_disabled requests).
        from ...approval_scope import (
            approval_dir_grant_candidate,
            approval_session_scope_for_request,
            request_matches_dir_grants,
        )

        scope_info = approval_session_scope_for_request(request)
        if scope_info.key is not None and scope_info.key in self._session_scope_grants:
            return ApprovalDecision(allow=True, allow_for_session=True)
        if request_matches_dir_grants(request, self._session_dir_grants):
            return ApprovalDecision(allow=True, allow_for_session=True)
        if self._approval_ui is not None:
            wait_started = time.perf_counter()
            try:
                decision = self._approval_ui(request)
            except Exception:
                decision = None
            finally:
                self.approval_wait_ms_total += int((time.perf_counter() - wait_started) * 1000)
            if isinstance(decision, ApprovalDecision):
                if decision.allow and getattr(decision, "allow_for_session_dir", False):
                    directory = approval_dir_grant_candidate(request)
                    if directory is not None:
                        self._session_dir_grants.add((str(request.kind), directory))
                        self._t.append(
                            "trace",
                            f"· always allowing {request.kind} under {directory} this session",
                        )
                        return ApprovalDecision(allow=True, allow_for_session=True)
                    # No coverable directory: honor the allow, once.
                    return ApprovalDecision(allow=True)
                if decision.allow and decision.allow_for_session:
                    if scope_info.supported and scope_info.key is not None:
                        self._session_scope_grants.add(scope_info.key)
                        return decision
                    # "Always" cannot stick to this approval (no safe exact
                    # scope). Downgrade to a one-time allow instead of leaking
                    # allow_for_session=True into guards that reject it.
                    self._t.append(
                        "warn",
                        scope_info.warning
                        or "Always is unavailable for this approval; allowed once.",
                    )
                    return ApprovalDecision(allow=True)
                return decision
        # No interactive approver reachable → fail closed.
        self._t.append(
            "warn",
            f"Denied {request.kind}: approval required "
            "(this mode gates the action, but no approval UI is available).",
        )
        return ApprovalDecision(allow=False)

    # ----------------------------------------------------------------- emit path
    # The agent dual-emits: message/tool events go through additive ``emit_*``
    # helpers (skipped when absent) AND the legacy ``on_*`` calls we implement
    # above — so we deliberately do NOT define ``emit_message_*`` /
    # ``emit_tool_call_*`` (that would double-render). Error/warning/info, by
    # contrast, are EITHER/OR: the runtime prefers a real (non-Noop) ``emit_*``
    # and only falls back to ``on_*`` otherwise. We therefore define real
    # delegating handlers so those never get silently dropped.
    #
    # Note: do not add a catch-all ``__getattr__`` here — a synthesized no-op
    # ``emit_error``/``emit_warning`` looks "real" to the runtime's capability
    # probe and would swallow every error/warning in the TUI.
    def emit_error(
        self,
        code: str,
        message: str,
        recoverable: bool,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        prefix = f"{code}: " if code else ""
        suffix = "" if recoverable else " (not recoverable)"
        self.on_error(f"{prefix}{message}{suffix}")

    def emit_warning(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.on_warning(str(message))

    def emit_info(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        if _worker_cancelled():
            return
        self._prepare_visible_output()
        self._t.append("info", _truncate(str(message), limit=480) or "info")


__all__ = ["TuiSurface", "set_active_cancellation"]
