"""Tests for the Forge feature in the full-screen TUI.

Grows phase by phase. Phase 0: the FORGE footer badge + mode state. Phase 1:
the read-only forge panels (/show, /plan tasks, /plan markdown) and the document
panel renderer that backs ``/plan markdown`` (PLAN.md without a pager).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code.cli_impl.commands import chat_tui_panels as panels
from alysis_code.cli_impl.tui import run_tui
from alysis_code.cli_impl.tui.app import (
    _forge_task_visual,
    _forge_view_rows,
    _render_doc_panel_rows,
)
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript

# --------------------------------------------------------------- helpers


def _plan() -> dict:
    return {
        "project_goal": "Ship the forge TUI",
        "summary": "Port forge into the alt-screen TUI",
        "requirements": ["must not block the UI thread", "stream swarm events live"],
        "tasks": [
            {"id": "T01", "title": "FORGE badge", "status": "done", "dependencies": []},
            {
                "id": "T02",
                "title": "live swarm view",
                "status": "failed",
                "dependencies": ["T01"],
            },
            {"id": "T03", "title": "assets picker", "status": "planned", "dependencies": []},
        ],
    }


def _values(spec) -> str:
    return " ".join(f"{k} {v}" for _name, rows in spec["sections"] for (k, v, _tone) in rows)


# ----------------------------------------------------- spec builders (Phase 1)


def test_forge_status_tone_buckets():
    assert panels._forge_status_tone("done") == "accent"
    assert panels._forge_status_tone("already_satisfied") == "accent"
    assert panels._forge_status_tone("verify_failed") == "err"
    assert panels._forge_status_tone("interrupted") == "err"
    assert panels._forge_status_tone("superseded") == "dim"
    assert panels._forge_status_tone("planned") == "plain"


def test_forge_plan_panel_spec_renders_overview_and_tasks():
    paths = SimpleNamespace(run_id="run-1a2b")
    spec = panels._chat_forge_plan_panel_spec(paths=paths, plan=_plan())
    names = [name for name, _ in spec["sections"]]
    assert names[0] == "Plan"
    assert any(n.startswith("Requirements") for n in names)
    assert any(n.startswith("Tasks") for n in names)
    blob = _values(spec)
    assert "Ship the forge TUI" in blob
    assert "1 done" in blob and "1 failed" in blob
    # Each task id is a key, its status+title the value.
    assert "FORGE badge" in blob and "live swarm view" in blob
    assert "deps: T01" in blob  # dependency rendered
    # Tone: the done task is accent, the failed task is err.
    tones = {(k, tone) for _n, rows in spec["sections"] for (k, _v, tone) in rows}
    assert ("T01", "accent") in tones
    assert ("T02", "err") in tones


def test_forge_plan_panel_spec_empty_plan():
    paths = SimpleNamespace(run_id="run-x")
    spec = panels._chat_forge_plan_panel_spec(paths=paths, plan={"project_goal": "", "tasks": []})
    blob = _values(spec)
    assert "(not set)" in blob  # goal/summary default
    assert "no tasks yet" in blob


def test_forge_markdown_panel_spec_reads_plan_md(tmp_path, monkeypatch):
    plan_md = tmp_path / "PLAN.md"
    plan_md.write_text("# Plan\n\n- one\n- two\n", encoding="utf-8")
    paths = SimpleNamespace(run_id="run-9z", plan_md_path=plan_md)
    # save_plan would rewrite PLAN.md from the in-memory plan; stub it so the test
    # controls the file content directly.
    monkeypatch.setattr("alysis_code.forge.save_plan", lambda *a, **k: None)
    spec = panels._chat_forge_markdown_panel_spec(paths=paths, plan=_plan())
    assert "body" in spec
    assert "# Plan" in spec["body"]
    assert "run-9z" in spec["title"]


def test_forge_markdown_panel_spec_missing_file(tmp_path, monkeypatch):
    missing = tmp_path / "nope" / "PLAN.md"
    paths = SimpleNamespace(run_id="run-0", plan_md_path=missing)
    monkeypatch.setattr("alysis_code.forge.save_plan", lambda *a, **k: None)
    spec = panels._chat_forge_markdown_panel_spec(paths=paths, plan=_plan())
    # Falls back to an error section rather than crashing.
    assert "Failed to read" in _values(spec)


# ------------------------------------------------------- doc panel renderer


def test_doc_panel_rows_renders_markdown():
    rows = _render_doc_panel_rows("# Title\n\n- a\n- b", 60, hint="Esc close")
    text = "\n".join("".join(t for _s, t in row) for row in rows)
    assert "Title" in text
    assert "Esc close" in text
    # Every row fits the width (cursor-pin scroll math depends on it).
    assert all(sum(len(t) for _s, t in row) <= 60 for row in rows)


def test_doc_panel_rows_plain_fallback_wraps():
    long_line = "word " * 60
    rows = _render_doc_panel_rows(long_line.strip(), 40)
    assert all(sum(len(t) for _s, t in row) <= 40 for row in rows)


# ------------------------------------------------- run_tui body-panel contract


class _FakeSession:
    def __init__(self, surface) -> None:
        self.surface = surface

    def run_turn(self, text, *, cancellation_token=None):
        return 0

    def close(self):
        pass


def _runner(calls):
    def runner(session, text, width):
        calls.append(text)
        if text.strip().lower() in ("/exit", "exit"):
            return ("exit", "", None, None)
        return ("handled", "", None, None)

    return runner


def _run_headless(state, keys, **kwargs):
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return run_tui(state, owl_color=False, input=pipe, output=DummyOutput(), **kwargs)


def test_body_panel_opens_and_is_not_routed_to_runner():
    # A provider returning a {title, body} spec opens the document panel natively
    # (like /plan markdown) instead of routing through the command runner.
    state = TuiState(model_name="m", username="t", forge_mode=True)
    calls: list = []

    def md_provider(arg=""):
        return {"title": "PLAN.md", "body": "# Hello\n\n- a\n- b"}

    _run_headless(
        state,
        "/plan markdown\rq/exit\r",
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/plan": md_provider},
        background_turns=False,
    )
    assert all("markdown" not in c.lower() for c in calls)  # intercepted, not run


def test_body_panel_enter_closes_without_routing():
    # Enter on an ordinary (no-confirm) panel only closes it — it must not route the
    # opening command to the runner (regression for the split Enter/confirm handler).
    state = TuiState(model_name="m", username="t", forge_mode=True)
    calls: list = []

    def md_provider(arg=""):
        return {"title": "PLAN.md", "body": "# Hello"}  # no "confirm" key

    _run_headless(
        state,
        "/plan markdown\r\r/exit\r",  # open panel, Enter closes, then exit
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/plan": md_provider},
        background_turns=False,
    )
    assert calls == ["/exit"]  # only the real submission routed; Enter just closed


# --------------------------------------------------- /forge intro popup (Phase 5)


def test_forge_intro_panel_spec_explains_workflow_and_confirms():
    spec = panels._chat_forge_intro_panel_spec()
    assert spec["confirm"] == "/forge"  # Enter enters the Forge session
    body = spec["body"]
    assert "Forge" in body
    # The workflow + the key commands are spelled out so the popup is self-explaining.
    assert "/execute plan" in body
    assert "/show" in body and "/done" in body
    # The begin/cancel affordance is present (body and/or hint).
    assert "Enter" in body or "Enter" in spec["hint"]


def test_forge_intro_popup_confirm_routes_forge_to_runner():
    # Plain /forge opens the guidance popup (intercepted, NOT routed); Enter then
    # routes "/forge" to the command runner exactly once to enter the session.
    state = TuiState(model_name="m", username="t")
    calls: list = []

    def forge_intro(arg=""):
        if arg.strip():
            return None  # "/forge resume" → straight to the runner, no intro
        return {
            "title": "Forge",
            "body": "# Forge\n\nHow it works.\n\n- /execute plan",
            "hint": "Enter to begin",
            "confirm": "/forge",
        }

    _result, transcript = _run_headless(
        state,
        "/forge\r\r/exit\r",  # open intro popup, Enter confirms, then exit
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/forge": forge_intro},
        background_turns=False,
    )
    assert calls.count("/forge") == 1  # only the Enter-confirm routed it
    assert ("user", "/forge") in transcript  # the confirm echoed the command


def test_forge_intro_popup_cancel_does_not_enter():
    # Closing the popup with q (cancel) must NOT route "/forge" to the runner.
    state = TuiState(model_name="m", username="t")
    calls: list = []

    def forge_intro(arg=""):
        return {"title": "Forge", "body": "# Forge", "confirm": "/forge"}

    _run_headless(
        state,
        "/forge\rq/exit\r",  # open intro popup, q cancels, then exit
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/forge": forge_intro},
        background_turns=False,
    )
    assert calls == ["/exit"]  # cancel routed nothing; only the real /exit ran


def _planner_prompt() -> dict:
    # Mirror loop._tui_forge_planner_prompt: on_confirm returns a picker whose
    # pick submits the flagged /forge form.
    def _choice(value):
        submit = (
            "/forge --planner" if str(value).strip().lower() == "yes" else "/forge --no-planner"
        )
        return {"submit": submit}

    return {
        "picker": {
            "title": "Use the planner assistant?",
            "rows": [
                {"value": "yes", "label": "Yes", "description": "use planner", "current": True},
                {"value": "no", "label": "No", "description": "plan manually"},
            ],
            "on_select": _choice,
        }
    }


def _forge_intro_with_planner(arg=""):
    if arg.strip():
        return None  # "/forge --planner" etc. fall through to the runner
    return {
        "title": "Forge",
        "body": "# Forge\n\nHow it works.\n\n- /execute plan",
        "hint": "Enter to choose planner",
        "on_confirm": _planner_prompt,
    }


def test_forge_intro_enter_opens_planner_prompt_then_enters_with_planner():
    # /forge → intro popup; Enter opens the "Use the planner?" picker; selecting
    # "Yes" (the default row) submits "/forge --planner" to the runner exactly once.
    state = TuiState(model_name="m", username="t")
    calls: list = []

    _run_headless(
        state,
        # intro (Enter) → picker (Enter) → pick the default "yes" row (Enter, which
        # closes the picker) → then /exit. The picker must close BEFORE /exit is
        # typed, else the picker swallows those keys and the app never exits.
        "/forge\r\r\r/exit\r",
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/forge": _forge_intro_with_planner},
        background_turns=False,
    )
    assert "/forge --planner" in calls
    assert "/forge" not in calls  # the bare intro command was never routed
    assert "/forge --no-planner" not in calls


def test_forge_intro_planner_prompt_can_choose_no_planner():
    # Choosing the second row submits "/forge --no-planner" instead.
    state = TuiState(model_name="m", username="t")
    calls: list = []

    _run_headless(
        state,
        "/forge\r\r2/exit\r",  # intro, Enter → picker, "2" picks the "No" row, exit
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/forge": _forge_intro_with_planner},
        background_turns=False,
    )
    assert "/forge --no-planner" in calls
    assert "/forge --planner" not in calls


def test_forge_intro_planner_prompt_esc_cancels():
    # Esc on the planner picker cancels without entering Forge.
    state = TuiState(model_name="m", username="t")
    calls: list = []

    _run_headless(
        state,
        "/forge\r\r\x1b/exit\r",  # intro, Enter → picker, Esc cancels the picker, exit
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/forge": _forge_intro_with_planner},
        background_turns=False,
    )
    assert calls == ["/exit"]  # nothing forge-related routed


# --------------------------------------------------- picker submit (Phase 2)


def test_picker_submit_opens_panel_no_extra_enter():
    # A forge /plan picker whose on_select returns {"submit": "/plan tasks"} runs
    # that text straight through the submit pipeline → the panel opens, and the
    # raw "/plan tasks" is never routed to the command runner.
    state = TuiState(model_name="m", username="t", forge_mode=True)
    calls: list = []

    def plan_panel(arg=""):
        if arg.strip().lower() == "tasks":
            return {"title": "Forge Plan", "sections": [("Plan", [("goal", "x", "plain")])]}
        return None  # bare /plan → picker

    def plan_picker():
        return {
            "title": "Plan",
            "rows": [
                {"label": "tasks", "description": "summary", "value": "tasks", "current": False},
            ],
            "on_select": lambda value: {"submit": f"/plan {value}"},
        }

    _run_headless(
        state,
        "/plan\r\rq/exit\r",  # open picker, Enter selects tasks, q closes panel
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/plan": plan_panel},
        picker_providers={"/plan": plan_picker},
        background_turns=False,
    )
    assert all(c.strip().lower() != "/plan tasks" for c in calls)  # opened panel, not run


def test_picker_submit_routes_two_part_command_to_runner():
    # A forge /assistant picker whose on_select returns {"submit": "/assistant on"}
    # routes the explicit two-token form to the command runner (no /assistant panel).
    state = TuiState(model_name="m", username="t", forge_mode=True)
    calls: list = []

    def assistant_picker():
        return {
            "title": "Planner Assistant",
            "rows": [
                {"label": "on", "description": "on", "value": "on", "current": False},
                {"label": "off", "description": "off", "value": "off", "current": True},
            ],
            "on_select": lambda value: {"submit": f"/assistant {value}"},
        }

    _run_headless(
        state,
        "/assistant\r1/exit\r",  # open picker; "1" picks "on" (off is the current row)
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        picker_providers={"/assistant": assistant_picker},
        background_turns=False,
    )
    assert any(c.strip().lower() == "/assistant on" for c in calls)


# ---------------------------------------- live swarm execution (Phase 3)


def _write_plan_json(tmp_path, tasks):
    import json

    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"tasks": [dict(t) for t in tasks]}), encoding="utf-8")
    return path


def test_transcript_forge_lifecycle():
    t = TuiTranscript()
    assert t.forge_snapshot() is None
    t.forge_begin("run-1", [("T01", "first", "planned"), ("T02", "second", "planned")])
    snap = t.forge_snapshot()
    assert snap is not None and snap["run_id"] == "run-1"
    assert [task["id"] for task in snap["tasks"]] == ["T01", "T02"]
    t.forge_set_active("T01", phase="worker.lifecycle", message="running")
    assert t.forge_snapshot()["active"] == "T01"
    t.forge_update_statuses({"T01": "in_progress"})
    assert t.forge_snapshot()["tasks"][0]["status"] == "in_progress"
    t.forge_finish({"T01": "done", "T02": "failed"}, summary="1 done · 1 failed")
    snap = t.forge_snapshot()
    assert snap["done"] is True and snap["active"] is None
    assert snap["tasks"][0]["status"] == "done"
    assert snap["message"] == "1 done · 1 failed"
    # A fresh user submission ends the view.
    t.append_user("next")
    assert t.forge_snapshot() is None


def test_transcript_append_trace_coalesces():
    t = TuiTranscript()
    t.append_trace("line one")
    t.append_trace("line two")
    assert t.entries == [("trace", "line one\nline two")]
    # A non-trace entry breaks the run.
    t.append("error", "boom")
    t.append_trace("line three")
    assert t.entries[-1] == ("trace", "line three")


def test_forge_task_visual_glyphs():
    spinner = "⠹"
    assert _forge_task_visual("done", False, spinner)[0] == "✓"
    assert _forge_task_visual("verify_failed", False, spinner)[0] == "✗"
    assert _forge_task_visual("planned", False, spinner)[0] == "○"
    # The in-flight task spins regardless of a stale "planned" status.
    assert _forge_task_visual("planned", True, spinner)[0] == spinner


def test_forge_task_visual_terminal_satisfied_and_interrupted_states():
    spinner = "spin"
    done_visual = _forge_task_visual("done", False, spinner)
    failed_visual = _forge_task_visual("verify_failed", False, spinner)

    assert _forge_task_visual("already_satisfied", False, spinner) == done_visual
    assert _forge_task_visual("interrupted", False, spinner) == failed_visual
    assert _forge_task_visual("interrupted", True, spinner) == failed_visual


def test_forge_view_rows_renders_table_within_width():
    view = {
        "run_id": "run-1a2b",
        "tasks": [
            {"id": "T01", "title": "FORGE badge", "status": "done"},
            {"id": "T02", "title": "live swarm view", "status": "in_progress"},
        ],
        "active": "T02",
        "phase": "worker.lifecycle",
        "message": "Worker running",
        "started": 0.0,
        "done": False,
    }
    rows = _forge_view_rows(view, 60, "⠹", 12)
    text = "\n".join("".join(t for _s, t in row) for row in rows)
    assert "FORGE" in text and "run-1a2b" in text
    assert "FORGE badge" in text and "live swarm view" in text
    assert "12s" in text  # elapsed on the status line
    assert all(sum(len(t) for _s, t in row) <= 60 for row in rows)


def test_surface_on_swarm_event_drives_view(tmp_path):
    from types import SimpleNamespace

    plan_path = _write_plan_json(
        tmp_path,
        [
            {"id": "T01", "title": "first", "status": "planned"},
            {"id": "T02", "title": "second", "status": "planned"},
        ],
    )
    t = TuiTranscript()
    surface = TuiSurface(t)
    paths = SimpleNamespace(run_id="run-x", plan_json_path=plan_path)
    surface.begin_forge(paths)
    assert t.forge_snapshot() is not None

    event = SimpleNamespace(phase="worker.lifecycle", message="Worker running.", task_id="T01")
    surface.on_swarm_event(event)
    snap = t.forge_snapshot()
    assert snap["active"] == "T01"
    # Minimal output: a normal progress event updates the live phase line, it does
    # NOT spill a trace line into the transcript.
    assert snap["message"] == "Worker running."
    assert not any(role == "trace" for role, _text in t.entries)

    # An error-phase event DOES render as an error line (errors are surfaced).
    surface.on_swarm_event(
        SimpleNamespace(phase="worker.error", message="Worker crashed", task_id="T02")
    )
    assert any(role == "error" and "Worker crashed" in text for role, text in t.entries)

    # Simulate the swarm persisting final statuses, then finish.
    _write_plan_json(
        tmp_path,
        [
            {"id": "T01", "title": "first", "status": "done"},
            {"id": "T02", "title": "second", "status": "failed"},
        ],
    )
    surface.end_forge()
    snap = t.forge_snapshot()
    assert snap["done"] is True
    assert snap["tasks"][0]["status"] == "done"
    assert snap["tasks"][1]["status"] == "failed"


def test_surface_interrupt_forge_marks_running_tasks_and_finishes_view(tmp_path):
    import json

    plan_path = _write_plan_json(
        tmp_path,
        [
            {"id": "T01", "title": "first", "status": "done"},
            {"id": "T02", "title": "second", "status": "in_progress"},
            {"id": "T03", "title": "third", "status": "running"},
            {"id": "T04", "title": "fourth", "status": "planned"},
        ],
    )
    t = TuiTranscript()
    surface = TuiSurface(t)
    surface.begin_forge(SimpleNamespace(run_id="run-stop", plan_json_path=plan_path))
    t.forge_set_active("T02", phase="worker.lifecycle", message="running")

    surface.interrupt_forge()

    data = json.loads(plan_path.read_text(encoding="utf-8"))
    statuses = {task["id"]: task["status"] for task in data["tasks"]}
    assert statuses == {
        "T01": "done",
        "T02": "interrupted",
        "T03": "interrupted",
        "T04": "planned",
    }
    assert data["tasks"][1]["last_error"] == "Interrupted by user."
    assert data["tasks"][2]["last_error"] == "Interrupted by user."

    snap = t.forge_snapshot()
    assert snap["done"] is True
    assert snap["active"] is None
    assert snap["ok"] is False
    assert snap["message"] == "Interrupted."
    rendered_statuses = {task["id"]: task["status"] for task in snap["tasks"]}
    assert rendered_statuses["T02"] == "interrupted"
    assert rendered_statuses["T03"] == "interrupted"


def test_transcript_forge_sync_adds_new_tasks():
    # forge_sync_tasks updates existing rows AND appends tasks added mid-run (e.g.
    # by plan enrichment) — they must not silently vanish from the live table.
    t = TuiTranscript()
    t.forge_begin("r", [("T01", "first", "planned")])
    t.forge_sync_tasks([("T01", "first", "done"), ("T02", "added by enrichment", "planned")])
    snap = t.forge_snapshot()
    assert [x["id"] for x in snap["tasks"]] == ["T01", "T02"]
    assert snap["tasks"][0]["status"] == "done"
    assert snap["tasks"][1]["title"] == "added by enrichment"


def test_surface_on_swarm_event_drops_stale_and_cancelled(tmp_path):
    plan_path = _write_plan_json(tmp_path, [{"id": "T01", "title": "x", "status": "planned"}])
    t = TuiTranscript()
    surface = TuiSurface(t)

    class _Tok:
        is_cancelled = False

    tok = _Tok()
    surface.begin_forge(SimpleNamespace(run_id="run-A", plan_json_path=plan_path), tok)

    # Event from a DIFFERENT run is dropped (the live phase line never updates).
    surface.on_swarm_event(
        SimpleNamespace(phase="p", message="stale", task_id="T01", run_id="run-OTHER")
    )
    assert t.forge_snapshot()["active"] is None
    # Matching run is applied.
    surface.on_swarm_event(
        SimpleNamespace(phase="p", message="live", task_id="T01", run_id="run-A")
    )
    assert t.forge_snapshot()["active"] == "T01"
    # Once the worker token is cancelled, further events drop — the sink runs on its
    # own thread where the thread-local cancel flag is invisible, so the token guard
    # is what stops stale painting.
    tok.is_cancelled = True
    surface.on_swarm_event(
        SimpleNamespace(phase="p", message="after", task_id="T02", run_id="run-A")
    )
    assert t.forge_snapshot()["active"] == "T01"  # unchanged (T02 dropped)


def test_surface_on_swarm_event_dropped_after_view_cleared(tmp_path):
    plan_path = _write_plan_json(tmp_path, [{"id": "T01", "title": "x", "status": "planned"}])
    t = TuiTranscript()
    surface = TuiSurface(t)
    surface.begin_forge(SimpleNamespace(run_id="r", plan_json_path=plan_path), None)
    t.append_user("a new turn")  # clears the forge view
    # Even an error-phase event is dropped once the view is gone (a new turn started).
    surface.on_swarm_event(
        SimpleNamespace(phase="worker.error", message="late", task_id="T01", run_id="r")
    )
    assert not any("late" in text for _r, text in t.entries)


def test_forge_view_rows_narrow_width_within_bound():
    view = {
        "run_id": "run-with-a-fairly-long-identifier",
        "tasks": [
            {
                "id": "T100",
                "title": "a long task title that should be clipped hard",
                "status": "verify_failed",
            },
        ],
        "active": "T100",
        "phase": "worker.lifecycle",
        "message": "Worker running on something",
        "started": 0.0,
        "done": False,
    }
    for w in (20, 24, 30, 40):
        rows = _forge_view_rows(view, w, "⠹", 5)
        assert all(sum(len(t) for _s, t in row) <= w for row in rows), f"row overflow at width {w}"
    # The no-tasks placeholder also stays within a narrow width.
    empty = {
        "run_id": "r",
        "tasks": [],
        "active": None,
        "phase": "execute",
        "message": "",
        "started": 0.0,
        "done": False,
    }
    assert all(sum(len(t) for _s, t in row) <= 20 for row in _forge_view_rows(empty, 20, "⠹", 0))


def test_serialized_sink_prefers_on_swarm_event(tmp_path):
    from alysis_code.swarm_trace import (
        SerializedSwarmTraceSink,
        build_swarm_trace_event,
    )

    seen: list = []

    class _FakeSurface:
        def on_swarm_event(self, event):
            seen.append(event)

        def on_progress_update(self, message):  # should NOT be called
            seen.append(("progress", message))

    sink = SerializedSwarmTraceSink(
        artifact_path=tmp_path / "trace" / "swarm.jsonl",
        trace_level="compact",
        surface=_FakeSurface(),
    )
    sink.emit(
        build_swarm_trace_event(run_id="r", phase="worker.lifecycle", message="hi", task_id="T01")
    )
    sink.close()  # joins the consumer thread
    assert len(seen) == 1
    assert getattr(seen[0], "task_id", None) == "T01"  # structured event, not progress


def test_forge_execute_callable_dispatched_on_worker():
    # A command_runner returning ("run", …, {"_deferred_execute": cb}) makes the turn
    # machinery call cb instead of session.run_turn.
    state = TuiState(model_name="m", username="t", forge_mode=True)
    ran: list = []

    def runner(session, text, width):
        low = text.strip().lower()
        if low in ("/exit", "exit"):
            return ("exit", "", None, None)
        if low == "/execute plan":
            return (
                "run",
                "",
                "/execute plan",
                {"_deferred_execute": lambda token: ran.append(True)},
            )
        return ("handled", "", None, None)

    _run_headless(
        state,
        "/execute plan\r/exit\r",
        session_builder=_FakeSession,
        command_runner=runner,
        background_turns=False,
    )
    assert ran == [True]


def test_deferred_command_keeps_tui_responsive_to_interrupt_and_exit():
    state = TuiState(model_name="m", username="t")
    started = threading.Event()
    stopped = threading.Event()
    worker_ids: list[int] = []
    feed_errors: list[str] = []
    tui_thread_id = threading.get_ident()

    def runner(session, text, width):
        low = text.strip().lower()
        if low in ("/exit", "exit"):
            return ("exit", "", None, None)
        if low == "slow planner":

            def _deferred(token):
                worker_ids.append(threading.get_ident())
                started.set()
                while not getattr(token, "is_cancelled", False):
                    time.sleep(0.005)
                stopped.set()

            return (
                "run",
                "",
                text,
                {"_deferred_execute": _deferred},
            )
        return ("handled", "", None, None)

    with create_pipe_input() as pipe:

        def _feed() -> None:
            pipe.send_text("slow planner\r")
            if not started.wait(timeout=2):
                feed_errors.append("deferred planner did not start")
                pipe.send_text("/exit\r")
                return
            pipe.send_text("\x03")  # Ctrl+C must be handled while the job is blocked.
            if not stopped.wait(timeout=2):
                feed_errors.append("deferred planner did not observe cancellation")
            pipe.send_text("/exit\r")

        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()
        result, transcript = run_tui(
            state,
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=_FakeSession,
            command_runner=runner,
            background_turns=True,
        )
        feeder.join(timeout=2)

    assert result == "/exit"
    assert ("user", "slow planner") in transcript
    assert ("warn", "Interrupted.") in transcript
    assert worker_ids and worker_ids[0] != tui_thread_id
    assert not feeder.is_alive()
    assert feed_errors == []


def test_only_planner_backed_plain_forge_messages_are_deferred():
    from alysis_code.cli_impl.chat.loop import (
        _should_defer_forge_planner_submission,
    )

    enabled = SimpleNamespace(ui_mode="forge", assistant_enabled=True)
    disabled = SimpleNamespace(ui_mode="forge", assistant_enabled=False)
    chat = SimpleNamespace(ui_mode="chat", assistant_enabled=True)

    assert _should_defer_forge_planner_submission(forge_state=enabled, text="build an app")
    assert not _should_defer_forge_planner_submission(forge_state=enabled, text="/show")
    assert not _should_defer_forge_planner_submission(forge_state=enabled, text=":q")
    assert not _should_defer_forge_planner_submission(forge_state=enabled, text="   ")
    assert not _should_defer_forge_planner_submission(forge_state=disabled, text="build an app")
    assert not _should_defer_forge_planner_submission(forge_state=chat, text="build an app")


# ------------------------------------------------ assets picker/detail (Phase 4)


class _FakeAssetRecord:
    def __init__(self, **kw):
        self.id = kw.get("id", "A1")
        self.title = kw.get("title", "logo.png")
        self.description = kw.get("description", "")
        self.kind = kw.get("kind", "image")
        self.mime = kw.get("mime", "image/png")
        self.original_filename = kw.get("original_filename", "logo.png")
        self.size_bytes = kw.get("size_bytes", 2048)
        self.pinned = kw.get("pinned", False)
        self.deleted_at = kw.get("deleted_at", None)


class _FakeAssetSurface:
    def __init__(self, entries):
        self._entries = entries

    def list_assets(self, *, include_deleted=False):
        return self._entries

    def show_asset(self, asset_id):
        for e in self._entries:
            if e.record.id == asset_id:
                comp = SimpleNamespace(
                    data=SimpleNamespace(
                        semantic_summary="A brand logo", stated_facts=["png", "square"]
                    ),
                    detected_language="en",
                    source="vision",
                )
                return SimpleNamespace(
                    record=e.record,
                    comprehension_status=e.comprehension_status,
                    comprehension=comp,
                    versions=[1],
                    extracted_text_preview="",
                )
        from alysis_code.assets import AssetError

        raise AssetError(f"Asset not found: {asset_id}")


def _patch_asset_surface(monkeypatch, entries):
    surface = _FakeAssetSurface(entries)
    monkeypatch.setattr(
        "alysis_code.assets.surface.build_asset_surface",
        lambda *a, **k: surface,
    )
    monkeypatch.setattr("alysis_code.forge.load_plan", lambda *a, **k: {})


def test_assets_picker_spec_lists_assets(monkeypatch):
    entries = [
        SimpleNamespace(
            record=_FakeAssetRecord(id="A1", title="logo.png", pinned=True),
            comprehension_status="ready",
        ),
        SimpleNamespace(
            record=_FakeAssetRecord(id="A2", title="notes.txt", kind="text", size_bytes=512),
            comprehension_status="pending",
        ),
    ]
    _patch_asset_surface(monkeypatch, entries)
    spec = panels._chat_assets_picker_spec(cfg=object(), paths=object())
    assert spec is not None
    labels = [r["label"] for r in spec["rows"]]
    assert labels == ["A1", "A2"]
    blob = " ".join(r["description"] for r in spec["rows"])
    assert "logo.png" in blob and "ready" in blob and "★" in blob  # pinned marker
    # on_select submits "/assets <id>" to open the detail panel.
    assert spec["on_select"]("A1") == {"submit": "/assets A1"}


def test_assets_picker_spec_none_when_empty(monkeypatch):
    _patch_asset_surface(monkeypatch, [])
    assert panels._chat_assets_picker_spec(cfg=object(), paths=object()) is None


def test_asset_detail_panel_spec(monkeypatch):
    entries = [
        SimpleNamespace(
            record=_FakeAssetRecord(
                id="A1", title="logo.png", pinned=True, description="brand logo"
            ),
            comprehension_status="ready",
        )
    ]
    _patch_asset_surface(monkeypatch, entries)
    spec = panels._chat_asset_detail_panel_spec(cfg=object(), paths=object(), asset_id="A1")
    blob = _values(spec)
    assert "logo.png" in blob and "brand logo" in blob
    assert "A brand logo" in blob  # comprehension summary
    assert "png" in blob  # a stated fact


def test_asset_detail_panel_spec_unknown_id(monkeypatch):
    _patch_asset_surface(monkeypatch, [])
    spec = panels._chat_asset_detail_panel_spec(cfg=object(), paths=object(), asset_id="ZZ")
    assert "not found" in _values(spec).lower()


# ------------------------------------------------- in-TUI editor (Phase 4)


def test_editor_opens_types_and_saves():
    # A panel provider returning {"editor": {...}} opens the in-TUI editor; typing
    # then Ctrl+S calls on_save with the buffer, which on success closes + echoes.
    state = TuiState(model_name="m", username="t", forge_mode=True)
    saved: dict = {}

    def on_save(text):
        saved["text"] = text
        return (True, "Plan saved.")

    def plan_panel(arg=""):
        if arg.strip().lower() == "edit":
            return {"editor": {"title": "Edit", "text": "{}", "on_save": on_save}}
        return None

    _result, transcript = _run_headless(
        state,
        "/plan edit\rZZ\x13/exit\r",  # open editor, type ZZ, Ctrl+S, exit
        session_builder=_FakeSession,
        command_runner=_runner([]),
        panel_providers={"/plan": plan_panel},
        background_turns=False,
    )
    assert "ZZ" in saved.get("text", "")
    assert ("system", "Plan saved.") in transcript


def test_editor_invalid_save_keeps_open():
    # on_save returning (False, msg) keeps the editor open (no echo); Esc cancels.
    state = TuiState(model_name="m", username="t", forge_mode=True)
    calls: list = []

    def on_save(text):
        calls.append(text)
        return (False, "Invalid JSON")

    def plan_panel(arg=""):
        if arg.strip().lower() == "edit":
            return {"editor": {"title": "Edit", "text": "{}", "on_save": on_save}}
        return None

    _result, transcript = _run_headless(
        state,
        # open, type X, Ctrl+S (fails), Ctrl+C (closes editor), exit. (Ctrl+C is a
        # single reliable byte; a lone Esc only flushes on a real-terminal timeout,
        # so headless tests close floats with q / Ctrl+C, not Esc.)
        "/plan edit\rX\x13\x03/exit\r",
        session_builder=_FakeSession,
        command_runner=_runner([]),
        panel_providers={"/plan": plan_panel},
        background_turns=False,
    )
    assert calls and "X" in calls[0]  # save attempted
    assert all(role != "system" or "Invalid" not in text for role, text in transcript)


# ------------------------------------ honest live swarm view (Change 1)


def test_transcript_active_map_tracks_and_prunes_workers():
    # active_map tracks EVERY currently-running worker (so all spin + get a timer),
    # while ``active`` stays the single most-recent task for the phase line.
    t = TuiTranscript()
    t.forge_begin(
        "r",
        [("T01", "a", "planned"), ("T02", "b", "planned"), ("T03", "c", "planned")],
    )
    t.forge_set_active("T01", phase="worker.lifecycle", message="run")
    t.forge_set_active("T02", phase="worker.lifecycle", message="run")
    snap = t.forge_snapshot()
    assert set(snap["active_map"]) == {"T01", "T02"}  # both live workers tracked
    assert snap["active"] == "T02"  # last one drives the bottom phase line
    assert all("started" in info for info in snap["active_map"].values())
    # A terminal status prunes the finished worker from the live set.
    t.forge_update_statuses({"T01": "done"})
    assert set(t.forge_snapshot()["active_map"]) == {"T02"}
    # A status-driven running task joins the set (gets a timer) without an event.
    t.forge_sync_tasks([("T03", "c", "in_progress")])
    assert "T03" in t.forge_snapshot()["active_map"]
    # Finishing the run clears the whole live set.
    t.forge_finish({"T01": "done", "T02": "done", "T03": "done"}, summary="ok")
    assert t.forge_snapshot()["active_map"] == {}


def test_transcript_active_map_never_resurrects_terminal_task():
    # A late event for an already-finished task must not add it back as "running".
    t = TuiTranscript()
    t.forge_begin("r", [("T01", "a", "done")])
    t.forge_set_active("T01", phase="p", message="late")
    assert t.forge_snapshot()["active_map"] == {}


def test_forge_view_rows_header_gauge_and_per_task_timer():
    view = {
        "run_id": "run-1",
        "tasks": [
            {"id": "T01", "title": "alpha", "status": "done"},
            {"id": "T02", "title": "beta", "status": "in_progress"},
        ],
        "active": "T02",
        "active_map": {"T02": {"started": 0.0}},
        "phase": "worker.lifecycle",
        "message": "",
        "started": 0.0,
        "done": False,
        "ok": True,
    }
    rows = _forge_view_rows(view, 72, "⠹", 30, {"T02": 75})
    text = "\n".join("".join(t for _s, t in row) for row in rows)
    assert "1/2" in text  # header progress count
    assert "1m15s" in text  # per-task elapsed timer for the running task
    assert all(sum(len(t) for _s, t in row) <= 72 for row in rows)


def test_forge_view_rows_no_timers_without_active_map():
    # The historical 4-arg call (no task_elapsed) draws no per-row timers and never
    # overflows — the width invariant the cursor-pin scroll math relies on.
    view = {
        "run_id": "r",
        "tasks": [{"id": "T01", "title": "x " * 30, "status": "in_progress"}],
        "active": "T01",
        "phase": "execute",
        "message": "m",
        "started": 0.0,
        "done": False,
    }
    for w in (20, 30, 60, 100):
        rows = _forge_view_rows(view, w, "⠹", 9)
        assert all(sum(len(t) for _s, t in row) <= w for row in rows)


def test_forge_status_glyph_matches_task_visual():
    # The shared glyph helper must reproduce _forge_task_visual's glyph exactly, so
    # the /show panel and the live table can never disagree.
    from alysis_code.cli_impl.tui.forge_status import forge_status_glyph

    for status in (
        "done",
        "already_satisfied",
        "failed",
        "verify_failed",
        "interrupted",
        "superseded",
        "invalidated",
        "in_progress",
        "running",
        "planned",
        "todo",
        "",
    ):
        for active in (False, True):
            assert (
                forge_status_glyph(status, active=active, spinner="S")
                == _forge_task_visual(status, active, "S")[0]
            ), (status, active)


# ------------------------------------ violet identity + glyph unify (Change 3)


def test_forge_plan_panel_prefixes_status_glyph_and_carries_accent():
    paths = SimpleNamespace(run_id="run-1a2b")
    spec = panels._chat_forge_plan_panel_spec(paths=paths, plan=_plan())
    assert spec.get("accent") == "forge"  # violet frame border
    values = {k: v for _n, rows in spec["sections"] for (k, v, _t) in rows}
    assert values["T01"].startswith("✓")  # done glyph, matching the live table
    assert values["T02"].startswith("✗")  # failed glyph
    assert values["T03"].startswith("○")  # planned glyph


def test_forge_specs_carry_forge_accent():
    assert panels._chat_forge_intro_panel_spec().get("accent") == "forge"


# ---------------------------------------- launch gate + knob picker (Change 2)


def test_execute_gate_picker_launch_defers_clean_command():
    # A panel spec carrying {"picker": ...} opens the picker directly. Selecting the
    # Launch row flags a one-shot bypass and re-submits a CLEAN "/execute plan" (no
    # internal sentinel); the gate consumes the flag so that re-submission falls
    # through to the runner exactly once instead of re-opening the gate.
    state = TuiState(model_name="m", username="t", forge_mode=True)
    calls: list = []
    gate_flag = {"confirmed": False}

    def execute_gate(arg=""):
        if arg.strip().lower() != "plan":
            return None
        if gate_flag["confirmed"]:
            gate_flag["confirmed"] = False
            return None  # the Launch re-submit falls through to the runner

        def _on_launch(_value):
            gate_flag["confirmed"] = True
            return {"submit": "/execute plan"}

        return {
            "picker": {
                "title": "Launch Forge",
                "rows": [{"value": "__launch__", "label": "Launch now", "current": True}],
                "on_select": _on_launch,
            }
        }

    _run_headless(
        state,
        "/execute plan\r\r/exit\r",  # open gate, Enter picks Launch, exit
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/execute": execute_gate},
        background_turns=False,
    )
    # The first "/execute plan" opened the gate (not routed); the Launch re-submit
    # reached the runner once, as a clean "/execute plan" — no "--go" sentinel.
    assert calls.count("/execute plan") == 1
    assert all("--go" not in c for c in calls)


def test_picker_on_select_can_reopen_picker():
    # on_select returning {"picker": ...} re-opens a (rebuilt) picker — the knob
    # cycling the launch gate relies on. Cycle once, then Launch.
    state = TuiState(model_name="m", username="t")
    calls: list = []

    def gate():
        return {
            "title": "Gate",
            "rows": [
                {"value": "cycle", "label": "cycle", "current": True},
                {"value": "launch", "label": "launch"},
            ],
            "on_select": (
                lambda v: {"picker": gate()} if str(v) == "cycle" else {"submit": "/launched"}
            ),
        }

    _run_headless(
        state,
        "/gate\r12/exit\r",  # open picker; "1" cycles (re-opens); "2" launches
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        picker_providers={"/gate": gate},
        background_turns=False,
    )
    assert "/launched" in calls  # only reachable if the cycle re-opened the picker


# ---------------------------------- planner reply → assistant block (TUI polish)


def test_planner_reply_sink_param_threaded_to_forge_handler():
    # Regression: the TUI hands _handle_chat_command a forge_planner_reply_sink, and
    # it MUST forward it to _handle_forge_chat_command (where the planner branch that
    # uses it actually lives). If the inner function lacks the parameter the name is
    # undefined there and the planner turn crashes at runtime — so assert BOTH
    # functions accept it. (commands.py resolves its helpers via runtime injection,
    # so a signature check is the isolation-safe way to pin this contract.)
    import inspect

    from alysis_code.cli_impl.chat import commands as cmd

    for fn in (cmd._handle_chat_command, cmd._handle_forge_chat_command):
        params = inspect.signature(fn).parameters
        assert "forge_planner_reply_sink" in params, fn.__name__


def test_plan_meta_rows_collapse_and_expand():
    from alysis_code.cli_impl.tui.app import _plan_meta_rows

    notes = "\n".join(
        [
            "Applied planner update to plan.",
            "Planner: New task 'SEO metadata': inferred estimated_files: README",
            "Plan reconciliation: Task T02: added explicit task path hints: README",
        ]
    )
    # Collapsed: exactly one summary line — an honest headline + counts (the
    # status note is folded into the headline, not repeated), never overflows.
    collapsed = _plan_meta_rows(notes, 70, expanded=False)
    assert len(collapsed) == 1
    text = "".join(t for _s, t in collapsed[0])
    assert text.startswith("▸")
    assert "plan updated" in text
    assert "2 notes" in text
    assert all(sum(len(t) for _s, t in row) <= 70 for row in collapsed)
    # Expanded: a header + the notes grouped per touched task (titles once,
    # not repeated per note), wrapped within width.
    expanded = _plan_meta_rows(notes, 70, expanded=True)
    body = "\n".join("".join(t for _s, t in row) for row in expanded)
    assert body.startswith("▾")
    assert "2 notes" in body
    assert body.count("SEO metadata") == 1 and "T02" in body
    assert "new task" in body and "inferred estimated_files: README" in body
    assert all(sum(len(t) for _s, t in row) <= 70 for row in expanded)
    # Empty input renders nothing.
    assert _plan_meta_rows("\n  \n", 70, expanded=False) == []
