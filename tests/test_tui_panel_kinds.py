"""The TUI's panel kinds: document panels, the in-TUI editor, pickers that a panel
or a picker selection opens, and commands whose work runs off the UI thread."""

from __future__ import annotations

import threading
import time

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code.cli_impl.tui import run_tui
from alysis_code.cli_impl.tui.app import _render_doc_panel_rows
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.cli_impl.tui.transcript import TuiTranscript


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


def _state() -> TuiState:
    return TuiState(model_name="m", username="t")


# ------------------------------------------------------------ document panels


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


def test_body_panel_opens_and_is_not_routed_to_runner():
    # A provider returning a {title, body} spec opens the document panel natively
    # instead of routing through the command runner.
    calls: list = []

    def notes_provider(arg=""):
        return {"title": "Notes", "body": "# Hello\n\n- a\n- b"}

    _run_headless(
        _state(),
        "/notes view\rq/exit\r",
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/notes": notes_provider},
        background_turns=False,
    )
    assert all("view" not in c.lower() for c in calls)  # intercepted, not run


def test_body_panel_enter_closes_without_routing():
    # Enter on an ordinary (no-confirm) panel only closes it — it must not route the
    # opening command to the runner (regression for the split Enter/confirm handler).
    calls: list = []

    def notes_provider(arg=""):
        return {"title": "Notes", "body": "# Hello"}  # no "confirm" key

    _run_headless(
        _state(),
        "/notes view\r\r/exit\r",  # open panel, Enter closes, then exit
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/notes": notes_provider},
        background_turns=False,
    )
    assert calls == ["/exit"]  # only the real submission routed; Enter just closed


# -------------------------------------------------------------------- pickers


def test_picker_submit_opens_panel_no_extra_enter():
    # A picker whose on_select returns {"submit": "/notes view"} runs that text
    # straight through the submit pipeline → the panel opens, and the raw
    # "/notes view" is never routed to the command runner.
    calls: list = []

    def notes_panel(arg=""):
        if arg.strip().lower() == "view":
            return {"title": "Notes", "sections": [("Notes", [("first", "x", "plain")])]}
        return None  # bare /notes → picker

    def notes_picker():
        return {
            "title": "Notes",
            "rows": [
                {"label": "view", "description": "summary", "value": "view", "current": False},
            ],
            "on_select": lambda value: {"submit": f"/notes {value}"},
        }

    _run_headless(
        _state(),
        "/notes\r\rq/exit\r",  # open picker, Enter selects view, q closes panel
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/notes": notes_panel},
        picker_providers={"/notes": notes_picker},
        background_turns=False,
    )
    assert all(c.strip().lower() != "/notes view" for c in calls)  # opened panel, not run


def test_picker_submit_routes_two_part_command_to_runner():
    # A picker whose on_select returns {"submit": "/sample on"} routes the explicit
    # two-token form to the command runner when no panel claims it.
    calls: list = []

    def sample_picker():
        return {
            "title": "Sample",
            "rows": [
                {"label": "on", "description": "on", "value": "on", "current": False},
                {"label": "off", "description": "off", "value": "off", "current": True},
            ],
            "on_select": lambda value: {"submit": f"/sample {value}"},
        }

    _run_headless(
        _state(),
        "/sample\r1/exit\r",  # open picker; "1" picks "on" (off is the current row)
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        picker_providers={"/sample": sample_picker},
        background_turns=False,
    )
    assert any(c.strip().lower() == "/sample on" for c in calls)


def test_panel_picker_launch_resubmits_a_clean_command_once():
    # A panel spec carrying {"picker": ...} opens the picker directly. Selecting the
    # Launch row flags a one-shot bypass and re-submits the CLEAN command; the gate
    # consumes the flag so that re-submission reaches the runner exactly once
    # instead of re-opening the gate.
    calls: list = []
    gate_flag = {"confirmed": False}

    def job_gate(arg=""):
        if arg.strip().lower() != "start":
            return None
        if gate_flag["confirmed"]:
            gate_flag["confirmed"] = False
            return None  # the Launch re-submit falls through to the runner

        def _on_launch(_value):
            gate_flag["confirmed"] = True
            return {"submit": "/job start"}

        return {
            "picker": {
                "title": "Start the job",
                "rows": [{"value": "__launch__", "label": "Launch now", "current": True}],
                "on_select": _on_launch,
            }
        }

    _run_headless(
        _state(),
        "/job start\r\r/exit\r",  # open gate, Enter picks Launch, exit
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/job": job_gate},
        background_turns=False,
    )
    assert calls.count("/job start") == 1
    assert all("--go" not in c for c in calls)


def test_picker_on_select_can_reopen_picker():
    # on_select returning {"picker": ...} re-opens a (rebuilt) picker. Cycle once,
    # then Launch.
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
        _state(),
        "/gate\r12/exit\r",  # open picker; "1" cycles (re-opens); "2" launches
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        picker_providers={"/gate": gate},
        background_turns=False,
    )
    assert "/launched" in calls  # only reachable if the cycle re-opened the picker


# --------------------------------------------------------------------- editor


def test_editor_opens_types_and_saves():
    # A panel provider returning {"editor": {...}} opens the in-TUI editor; typing
    # then Ctrl+S calls on_save with the buffer, which on success closes + echoes.
    saved: dict = {}

    def on_save(text):
        saved["text"] = text
        return (True, "Notes saved.")

    def notes_panel(arg=""):
        if arg.strip().lower() == "edit":
            return {"editor": {"title": "Edit", "text": "{}", "on_save": on_save}}
        return None

    _result, transcript = _run_headless(
        _state(),
        "/notes edit\rZZ\x13/exit\r",  # open editor, type ZZ, Ctrl+S, exit
        session_builder=_FakeSession,
        command_runner=_runner([]),
        panel_providers={"/notes": notes_panel},
        background_turns=False,
    )
    assert "ZZ" in saved.get("text", "")
    assert ("system", "Notes saved.") in transcript


def test_editor_invalid_save_keeps_open():
    # on_save returning (False, msg) keeps the editor open (no echo); Ctrl+C closes.
    calls: list = []

    def on_save(text):
        calls.append(text)
        return (False, "Invalid JSON")

    def notes_panel(arg=""):
        if arg.strip().lower() == "edit":
            return {"editor": {"title": "Edit", "text": "{}", "on_save": on_save}}
        return None

    _result, transcript = _run_headless(
        _state(),
        # open, type X, Ctrl+S (fails), Ctrl+C (closes editor), exit. (Ctrl+C is a
        # single reliable byte; a lone Esc only flushes on a real-terminal timeout,
        # so headless tests close floats with q / Ctrl+C, not Esc.)
        "/notes edit\rX\x13\x03/exit\r",
        session_builder=_FakeSession,
        command_runner=_runner([]),
        panel_providers={"/notes": notes_panel},
        background_turns=False,
    )
    assert calls and "X" in calls[0]  # save attempted
    assert all(role != "system" or "Invalid" not in text for role, text in transcript)


# ------------------------------------------------------------ deferred work


def test_deferred_execute_callable_runs_instead_of_a_turn():
    # A command_runner returning ("run", …, {"_deferred_execute": cb}) makes the turn
    # machinery call cb instead of session.run_turn.
    ran: list = []

    def runner(session, text, width):
        low = text.strip().lower()
        if low in ("/exit", "exit"):
            return ("exit", "", None, None)
        if low == "/job start":
            return (
                "run",
                "",
                "/job start",
                {"_deferred_execute": lambda token: ran.append(True)},
            )
        return ("handled", "", None, None)

    _run_headless(
        _state(),
        "/job start\r/exit\r",
        session_builder=_FakeSession,
        command_runner=runner,
        background_turns=False,
    )
    assert ran == [True]


def test_deferred_command_keeps_tui_responsive_to_interrupt_and_exit():
    started = threading.Event()
    stopped = threading.Event()
    worker_ids: list[int] = []
    feed_errors: list[str] = []
    tui_thread_id = threading.get_ident()

    def runner(session, text, width):
        low = text.strip().lower()
        if low in ("/exit", "exit"):
            return ("exit", "", None, None)
        if low == "slow job":

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
            pipe.send_text("slow job\r")
            if not started.wait(timeout=2):
                feed_errors.append("deferred job did not start")
                pipe.send_text("/exit\r")
                return
            pipe.send_text("\x03")  # Ctrl+C must be handled while the job is blocked.
            if not stopped.wait(timeout=2):
                feed_errors.append("deferred job did not observe cancellation")
            pipe.send_text("/exit\r")

        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()
        result, transcript = run_tui(
            _state(),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=_FakeSession,
            command_runner=runner,
            background_turns=True,
        )
        feeder.join(timeout=2)

    assert result == "/exit"
    assert ("user", "slow job") in transcript
    assert ("warn", "Interrupted.") in transcript
    assert worker_ids and worker_ids[0] != tui_thread_id
    assert not feeder.is_alive()
    assert feed_errors == []


# ----------------------------------------------------------------- transcript


def test_transcript_append_trace_coalesces():
    t = TuiTranscript()
    t.append_trace("line one")
    t.append_trace("line two")
    assert t.entries == [("trace", "line one\nline two")]
    # A non-trace entry breaks the run.
    t.append("error", "boom")
    t.append_trace("line three")
    assert t.entries[-1] == ("trace", "line three")
