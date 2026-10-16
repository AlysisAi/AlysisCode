from __future__ import annotations

import threading
from time import sleep
from typing import Any

import pytest

from alysis_code.cli_impl.tui.subagent_panel import (
    ENTRY_BUFFER_CAP,
    append_bounded_entries,
    subagent_panel_rows,
)


def _text(row: list[tuple[str, str]]) -> str:
    return "".join(text for _style, text in row)


def _status() -> dict[str, object]:
    return {
        "run_id": "run-2",
        "subagent": "implementer",
        "label": "forge-persist",
        "state": "running",
        "elapsed_ms": 24_800,
        "steps_completed": 6,
        "workspace_view": "isolated",
    }


def test_subagents_picker_selects_an_active_run_for_the_panel() -> None:
    from alysis_code.cli_impl.tui.app import _subagents_picker_spec

    class _Scheduler:
        def status(self) -> dict[str, object]:
            return {
                "children": [
                    {
                        "run_id": "run-live",
                        "subagent": "explorer",
                        "label": "forge-plan",
                        "state": "running",
                        "workspace_view": "shared",
                        "elapsed_ms": 1500,
                        "steps_completed": 2,
                        "activity": "Running readonly investigation.",
                    },
                    {
                        "run_id": "run-done",
                        "subagent": "reviewer",
                        "state": "joined",
                        "workspace_view": "shared",
                        "elapsed_ms": 900,
                        "steps_completed": 1,
                        "activity": "Joined.",
                    },
                ]
            }

    panel_state = {
        "selected_run_id": "",
        "run_order": [],
        "cursors": {},
        "entries": {},
        "statuses": {},
        "lifecycles": {},
        "last_poll": None,
    }

    spec = _subagents_picker_spec(_Scheduler(), panel_state)

    assert spec is not None
    assert spec["title"] == "Active Subagents"
    assert [row["value"] for row in spec["rows"]] == ["run-live"]
    assert spec["rows"][0]["label"] == "explorer \u00b7 forge-plan"
    assert "running" in spec["rows"][0]["description"]
    spec["on_select"]("run-live")
    assert panel_state["selected_run_id"] == "run-live"
    assert panel_state["run_order"] == ["run-live"]
    assert panel_state["statuses"]["run-live"]["subagent"] == "explorer"


def test_panel_renders_status_body_and_rotation_hint() -> None:
    rows = subagent_panel_rows(
        _status(),
        [
            {"kind": "tool", "summary": "search_rg: find scheduler"},
            {"kind": "tool_result", "summary": "three matches"},
            {"kind": "assistant", "summary": "I found the poll path."},
            {"kind": "user", "summary": "Keep the layout compact."},
        ],
        width=80,
        height=8,
        position=2,
        total=3,
    )

    assert _text(rows[0]) == (
        "implementer \u00b7 forge-persist \u00b7 running \u00b7 24s \u00b7 6 steps "
        "\u00b7 isolated    view 2/3"
    )
    body = "\n".join(_text(row) for row in rows[1:-1])
    assert "\u25b8 search_rg: find scheduler" in body
    assert "\u2713 three matches" in body
    assert "a: I found the poll path." in body
    assert "u: Keep the layout compact." in body
    assert _text(rows[-1]) == "alt+left / alt+right to switch \u00b7 esc to close"


def test_panel_and_picker_render_quiet_time_after_existing_activity_interval() -> None:
    from alysis_code.cli_impl.tui.app import _subagents_picker_spec

    quiet_status = {
        **_status(),
        "last_event_age_s": 252.4,
        "activity_threshold_s": 15.0,
    }
    header = _text(
        subagent_panel_rows(
            quiet_status,
            [],
            width=120,
            height=4,
            position=1,
            total=1,
        )[0]
    )
    assert "quiet 4m 12s" in header

    class _Scheduler:
        def status(self) -> dict[str, object]:
            return {"children": [quiet_status]}

    panel_state = {
        "selected_run_id": "",
        "run_order": [],
        "cursors": {},
        "entries": {},
        "statuses": {},
        "lifecycles": {},
        "last_poll": None,
    }
    spec = _subagents_picker_spec(_Scheduler(), panel_state)
    assert spec is not None
    assert "quiet 4m 12s" in spec["rows"][0]["description"]


def test_quiet_time_clears_on_new_event_through_poll_path() -> None:
    from alysis_code.cli_impl.tui.app import _poll_selected_subagent

    payloads = iter(
        [
            {
                **_status(),
                "last_event_age_s": 16.0,
                "activity_threshold_s": 15.0,
                "transcript_tail": [],
                "next_cursor": 1,
            },
            {
                **_status(),
                "last_event_age_s": 0.1,
                "activity_threshold_s": 15.0,
                "transcript_tail": [{"kind": "tool", "summary": "Read File \u00b7 new event"}],
                "next_cursor": 2,
            },
        ]
    )

    class _Scheduler:
        def view_since(self, *, run_id: str, cursor: int) -> dict[str, object]:
            _ = run_id, cursor
            return next(payloads)

    state = _panel_lifecycle_state(selected="run-2")
    scheduler = _Scheduler()
    assert _poll_selected_subagent(state, scheduler, now=1.0)
    first = _text(
        subagent_panel_rows(
            state["statuses"]["run-2"],
            state["entries"]["run-2"],
            width=120,
            height=5,
            position=1,
            total=2,
        )[0]
    )
    assert "quiet 16s" in first

    assert _poll_selected_subagent(state, scheduler, now=2.0)
    second = _text(
        subagent_panel_rows(
            state["statuses"]["run-2"],
            state["entries"]["run-2"],
            width=120,
            height=5,
            position=1,
            total=2,
        )[0]
    )
    assert "quiet" not in second


def test_panel_keeps_newest_complete_entries_within_height() -> None:
    entries = [{"kind": "assistant", "summary": f"entry {number}"} for number in range(8)]
    rows = subagent_panel_rows(_status(), entries, width=40, height=5, position=1, total=2)

    assert len(rows) == 5
    body = "\n".join(_text(row) for row in rows[1:-1])
    assert "entry 4" not in body
    assert "entry 5" in body
    assert "entry 6" in body
    assert "entry 7" in body


def test_panel_clips_entries_to_one_row_and_never_exceeds_width() -> None:
    rows = subagent_panel_rows(
        _status(),
        [{"kind": "tool", "summary": "word " * 30}],
        width=24,
        height=6,
        position=1,
        total=2,
    )

    assert len(rows) == 3
    assert all(len(_text(row)) <= 24 for row in rows)
    assert _text(rows[1]).startswith("\u25b8 word")


def test_panel_tool_entries_use_one_row_at_typical_width() -> None:
    rows = subagent_panel_rows(
        _status(),
        [{"kind": "tool", "summary": "Search Workspace \u00b7 " + "pattern " * 30}],
        width=100,
        height=10,
        position=1,
        total=1,
    )

    assert len(rows) == 3
    assert _text(rows[1]).startswith("\u25b8 Search Workspace \u00b7 pattern")
    assert len(_text(rows[1])) <= 100


def test_entry_buffer_is_bounded_to_a_few_hundred_newest_entries() -> None:
    existing = [{"kind": "tool", "summary": str(index)} for index in range(200)]
    incoming = [{"kind": "assistant", "summary": str(index)} for index in range(200, 400)]

    buffered = append_bounded_entries(existing, incoming)

    assert ENTRY_BUFFER_CAP == 240
    assert len(buffered) == ENTRY_BUFFER_CAP
    assert buffered[0]["summary"] == "160"
    assert buffered[-1]["summary"] == "399"


def test_panel_container_is_in_root_split_between_status_and_input(monkeypatch) -> None:
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.containers import (
        ConditionalContainer,
        FloatContainer,
        HSplit,
    )
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui.app import _SUBAGENT_PANEL_HEIGHT, run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    captured: dict[str, Application] = {}
    monkeypatch.setattr(
        Application, "run", lambda app, *args, **kwargs: captured.setdefault("app", app)
    )
    with create_pipe_input() as pipe:
        run_tui(TuiState(), owl_color=False, input=pipe, output=DummyOutput())

    outer = captured["app"].layout.container
    assert isinstance(outer, FloatContainer)
    root = outer.content
    assert isinstance(root, HSplit)
    panel = root.children[2]
    assert isinstance(panel, ConditionalContainer)
    assert isinstance(panel.content, HSplit)
    dimension = panel.content.preferred_height(80, 40)
    assert dimension.preferred == _SUBAGENT_PANEL_HEIGHT
    assert dimension.max == _SUBAGENT_PANEL_HEIGHT
    assert panel.content.style == "class:frame class:tui.subpanel"
    assert root.children[1] is not panel and root.children[3] is not panel


def test_panel_container_visibility_tracks_selected_child() -> None:
    from alysis_code.cli_impl.tui.app import _subagent_panel_container

    state: dict[str, object] = {"selected_run_id": ""}
    panel = _subagent_panel_container(state)
    assert not panel.filter()

    state["selected_run_id"] = "run-1"
    assert panel.filter()

    state["selected_run_id"] = ""
    assert not panel.filter()


def test_cycle_wraps_within_children_in_spawn_order() -> None:
    from alysis_code.cli_impl.tui.app import _cycle_subagent_panel

    state: dict[str, object] = {
        "selected_run_id": "",
        "run_order": ["run-1", "run-2"],
    }
    assert _cycle_subagent_panel(state, 1) == "run-1"
    assert _cycle_subagent_panel(state, 1) == "run-2"
    assert _cycle_subagent_panel(state, 1) == "run-1"
    assert _cycle_subagent_panel(state, -1) == "run-2"
    assert _cycle_subagent_panel(state, -1) == "run-1"


def test_surface_reports_child_run_ids_in_start_order() -> None:
    from alysis_code.cli_impl.tui.surface import TuiSurface
    from alysis_code.cli_impl.tui.transcript import TuiTranscript
    from alysis_code.surface.types import SubagentStartEvent

    seen: list[str] = []
    transcript = TuiTranscript()
    surface = TuiSurface(
        transcript,
        on_subagent_run_started=lambda event: seen.append(str(event.subagent_run_id)),
    )
    for run_id in ("run-1", "run-2"):
        surface.on_subagent_start(
            SubagentStartEvent(
                name="implementer",
                mode="review",
                subagent_run_id=run_id,
                label=f"forge-{run_id[-1]}",
            )
        )

    assert seen == ["run-1", "run-2"]
    rendered = [text for kind, text in transcript.entries if kind == "subagent"]
    assert rendered == [
        "\u21aa implementer \u00b7 forge-1 \u00b7 review",
        "\u21aa implementer \u00b7 forge-2 \u00b7 review",
    ]


def test_ctrl_b_falls_through_to_cursor_left_without_children() -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui.app import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    with create_pipe_input() as pipe:
        pipe.send_text("ab\x02X\r/exit\r")
        _result, transcript = run_tui(TuiState(), owl_color=False, input=pipe, output=DummyOutput())

    assert ("user", "aXb") in transcript


def _captured_app_with_children(monkeypatch, *, persona_cycle=None):
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui.app import run_tui
    from alysis_code.cli_impl.tui.state import TuiState
    from alysis_code.surface.types import SubagentStartEvent

    class _Session:
        def run_turn(self, text: str, *, cancellation_token=None) -> int:
            return 0

    def _build(surface):
        for run_id in ("run-1", "run-2"):
            surface.on_subagent_start(
                SubagentStartEvent(
                    name=f"agent-{run_id[-1]}",
                    mode="review",
                    subagent_run_id=run_id,
                )
            )
        return _Session()

    captured: dict[str, Application] = {}
    monkeypatch.setattr(
        Application, "run", lambda app, *args, **kwargs: captured.setdefault("app", app)
    )
    with create_pipe_input() as pipe:
        run_tui(
            TuiState(),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=_build,
            persona_cycle=persona_cycle,
        )
    return captured["app"]


def _feed(app, *keys) -> None:
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.key_binding.key_processor import KeyPress, _Flush

    with set_app(app):
        app.timeoutlen = None
        app.key_processor.feed_multiple([KeyPress(key) for key in keys])
        # Model the terminal timeout that completes a standalone Escape key.
        app.key_processor.feed(_Flush)
        app.key_processor.process_keys()


def test_alt_right_alt_left_cycle_and_escape_closes_panel(monkeypatch) -> None:
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout.containers import FloatContainer, HSplit

    app = _captured_app_with_children(monkeypatch)
    outer = app.layout.container
    assert isinstance(outer, FloatContainer)
    assert isinstance(outer.content, HSplit)
    panel = outer.content.children[2]
    assert not panel.filter()

    _feed(app, Keys.Escape, Keys.Right)
    assert panel.filter()
    _feed(app, Keys.Escape, Keys.Right)
    assert panel.filter()
    _feed(app, Keys.Escape, Keys.Right)
    assert panel.filter()

    _feed(app, Keys.Escape, Keys.Left)
    assert panel.filter()
    _feed(app, Keys.Escape)
    assert not panel.filter()


def test_escape_closes_selected_child_without_interrupting_turn() -> None:
    import threading
    import time

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui.app import run_tui
    from alysis_code.cli_impl.tui.state import TuiState
    from alysis_code.surface.types import SubagentStartEvent

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    observed_cancelled: list[bool] = []

    class _Session:
        def __init__(self, surface) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token=None) -> int:
            self.surface.on_subagent_start(
                SubagentStartEvent(
                    name="implementer",
                    mode="review",
                    subagent_run_id="run-live",
                )
            )
            started.set()
            release.wait(timeout=2.0)
            observed_cancelled.append(bool(cancellation_token.is_cancelled))
            finished.set()
            return 0

    with create_pipe_input() as pipe:

        def _send_keys() -> None:
            pipe.send_text("go\r")
            assert started.wait(timeout=2.0)
            pipe.send_text("\x1b[1;3C")
            time.sleep(0.05)
            pipe.send_text("\x1b")
            time.sleep(0.1)
            release.set()
            assert finished.wait(timeout=2.0)
            time.sleep(0.05)
            pipe.send_text("/exit\r")

        sender = threading.Thread(target=_send_keys)
        sender.start()
        run_tui(
            TuiState(),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=lambda surface: _Session(surface),
        )
        sender.join(timeout=2.0)

    assert observed_cancelled == [False]


def test_ctrl_n_keeps_completion_navigation_when_children_exist(monkeypatch) -> None:
    from prompt_toolkit.completion import Completion
    from prompt_toolkit.keys import Keys

    app = _captured_app_with_children(monkeypatch)
    control = app.layout.current_control
    control.buffer.text = "/"
    control.buffer.cursor_position = 1
    control.buffer._set_completions(
        [
            Completion("/alpha", start_position=-1),
            Completion("/beta", start_position=-1),
        ]
    )
    control.buffer.go_to_completion(0)
    before = control.buffer.complete_state.current_completion.text

    _feed(app, Keys.ControlN)

    after = control.buffer.complete_state.current_completion.text
    assert (before, after) == ("/alpha", "/beta")
    panel = app.layout.container.content.children[2]
    assert not panel.filter()


def test_tab_still_cycles_persona_with_children(monkeypatch) -> None:
    from prompt_toolkit.keys import Keys

    calls: list[int] = []
    app = _captured_app_with_children(
        monkeypatch,
        persona_cycle=lambda: calls.append(1) or [],
    )

    _feed(app, Keys.Tab)

    assert calls == [1]


def _run_spawn_tip_turns(keys: str, *, children_per_turn: int) -> list[tuple[str, str]]:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui.app import run_tui
    from alysis_code.cli_impl.tui.state import TuiState
    from alysis_code.surface.types import SubagentEndEvent, SubagentStartEvent

    class _Session:
        def __init__(self, surface) -> None:
            self.surface = surface
            self.turn = 0

        def run_turn(self, text: str, *, cancellation_token=None) -> int:
            self.turn += 1
            events = []
            for index in range(children_per_turn):
                event = SubagentStartEvent(
                    name=f"agent-{index}",
                    mode="review",
                    subagent_run_id=f"run-{self.turn}-{index}",
                )
                events.append(event)
                self.surface.on_subagent_start(event)
            for event in reversed(events):
                self.surface.on_subagent_end(
                    SubagentEndEvent(
                        name=event.name,
                        mode=event.mode,
                        status="success",
                        elapsed_ms=1,
                        steps_completed=1,
                        subagent_run_id=event.subagent_run_id,
                    )
                )
            return 0

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        _result, transcript = run_tui(
            TuiState(),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=lambda surface: _Session(surface),
            background_turns=False,
        )
    return transcript


def test_parallel_child_spawns_print_one_tip_per_turn() -> None:
    transcript = _run_spawn_tip_turns("first\r/exit\r", children_per_turn=4)

    assert transcript.count(("info", "tip: alt+right to follow subagent work")) == 1


def test_spawn_tip_resets_for_the_next_turn() -> None:
    transcript = _run_spawn_tip_turns("first\rsecond\r/exit\r", children_per_turn=1)

    assert transcript.count(("info", "tip: alt+right to follow subagent work")) == 2


def test_spawn_tip_is_suppressed_when_panel_is_already_open() -> None:
    transcript = _run_spawn_tip_turns("first\r\x1b[1;3Csecond\r/exit\r", children_per_turn=1)

    assert transcript.count(("info", "tip: alt+right to follow subagent work")) == 1


def _panel_lifecycle_state(*, selected: str = "") -> dict[str, object]:
    return {
        "selected_run_id": selected,
        "run_order": ["run-1", "run-2"],
        "cursors": {"run-1": 12, "run-2": 4},
        "entries": {
            "run-1": [{"kind": "tool", "summary": "one"}],
            "run-2": [{"kind": "assistant", "summary": "two"}],
        },
        "statuses": {
            "run-1": {"subagent": "implementer", "state": "joined"},
            "run-2": {"subagent": "reviewer", "state": "running"},
        },
        "lifecycles": {
            "run-1": {
                "state": "joined",
                "collected": True,
                "outcome": "finished",
            },
            "run-2": {"state": "running", "collected": False},
        },
        "last_poll": None,
    }


def test_terminal_child_never_left_by_operator_is_not_evicted() -> None:
    from alysis_code.cli_impl.tui.app import _evict_subagent_panel_runs

    state = _panel_lifecycle_state(selected="run-2")
    evicted = _evict_subagent_panel_runs(state, departed_run_id="run-2")

    assert evicted == []
    assert state["run_order"] == ["run-1", "run-2"]
    assert "run-1" in state["entries"]


def test_selected_finished_child_survives_until_navigation_then_is_evicted() -> None:
    from alysis_code.cli_impl.tui.app import (
        _cycle_subagent_panel,
        _evict_subagent_panel_runs,
        _subagent_panel_view_position,
    )

    state = _panel_lifecycle_state(selected="run-1")
    assert _evict_subagent_panel_runs(state, departed_run_id="") == []
    assert "run-1" in state["entries"]

    assert _cycle_subagent_panel(state, 1) == "run-2"
    assert _evict_subagent_panel_runs(state, departed_run_id="run-1") == [
        {
            "run_id": "run-1",
            "subagent": "implementer",
            "outcome": "finished",
        }
    ]
    assert "run-1" not in state["entries"]
    assert state["run_order"] == ["run-2"]
    assert _subagent_panel_view_position(state, "run-2") == (1, 1)
    assert _cycle_subagent_panel(state, 1) == "run-2"


def test_cycle_away_from_running_child_evicts_nothing() -> None:
    from alysis_code.cli_impl.tui.app import (
        _cycle_subagent_panel,
        _evict_subagent_panel_runs,
    )

    state = _panel_lifecycle_state(selected="run-2")
    assert _cycle_subagent_panel(state, 1) == "run-1"
    assert _evict_subagent_panel_runs(state, departed_run_id="run-2") == []
    assert state["run_order"] == ["run-1", "run-2"]


def test_escape_from_uncollected_terminal_child_evicts_only_its_pane() -> None:
    from alysis_code.cli_impl.tui.app import _evict_subagent_panel_runs

    state = _panel_lifecycle_state(selected="run-1")
    state["lifecycles"]["run-1"]["collected"] = False
    state["selected_run_id"] = ""

    assert _evict_subagent_panel_runs(state, departed_run_id="run-1") == [
        {
            "run_id": "run-1",
            "subagent": "implementer",
            "outcome": "finished",
        }
    ]
    assert state["selected_run_id"] == ""
    assert state["run_order"] == ["run-2"]


def test_cancelled_child_uses_the_same_terminal_eviction_terms() -> None:
    from alysis_code.cli_impl.tui.app import _evict_subagent_panel_runs

    state = _panel_lifecycle_state()
    state["lifecycles"]["run-1"] = {
        "state": "cancelled",
        "collected": True,
        "outcome": "cancelled",
    }

    assert _evict_subagent_panel_runs(state, departed_run_id="run-1") == [
        {
            "run_id": "run-1",
            "subagent": "implementer",
            "outcome": "cancelled",
        }
    ]


def test_polling_is_incremental_selected_only_and_stops_when_closed() -> None:
    import inspect

    from alysis_code.cli_impl.tui.app import _poll_selected_subagent

    assert "events_snapshot" not in inspect.getsource(_poll_selected_subagent)

    class _Scheduler:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def view_since(self, *, run_id: str, cursor: int) -> dict[str, object]:
            self.calls.append((run_id, cursor))
            return {
                "run_id": run_id,
                "subagent": "reviewer",
                "state": "running",
                "elapsed_ms": 900,
                "steps_completed": 2,
                "workspace_view": "shared",
                "transcript_tail": [{"kind": "tool", "summary": "search: src"}],
                "next_cursor": 7,
            }

    scheduler = _Scheduler()
    state = _panel_lifecycle_state(selected="run-2")
    assert _poll_selected_subagent(state, scheduler, now=1.0)
    assert not _poll_selected_subagent(state, scheduler, now=1.2)
    state["selected_run_id"] = ""
    assert not _poll_selected_subagent(state, scheduler, now=2.0)

    assert scheduler.calls == [("run-2", 4)]
    assert state["cursors"]["run-2"] == 7
    assert state["entries"]["run-2"][-1] == {
        "kind": "tool",
        "summary": "search: src",
    }


def test_runtime_panel_elapsed_freezes_across_polls_and_evict_readd(
    tmp_path,
    monkeypatch,
) -> None:
    from alysis_code import agent_loop
    from alysis_code.agent import session as agent_session
    from alysis_code.agent_loop import build_tools
    from alysis_code.cli_impl.tui.app import (
        _evict_subagent_panel_runs,
        _poll_selected_subagent,
    )
    from alysis_code.config import AppConfig
    from alysis_code.llm.openai_compat import LLMResponse
    from alysis_code.subagents import SubagentDefinition

    class _Store:
        session_id = "panel-parent"

        def configure_web_fetch_trusted_domains(self, domains: Any) -> tuple[str, ...]:
            return tuple(domains or ())

        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def append(self, event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, payload))

    class _FinalClient:
        model = "test-model"
        temperature = 0.0

        def chat(self, **_kwargs: Any) -> LLMResponse:
            return LLMResponse(content="Panel child complete.", tool_calls=[], raw={})

    def _create_child(**kwargs: Any) -> Any:
        kwargs["session_log_dir_override"] = tmp_path / "child-sessions"
        child = agent_session.create_session(**kwargs)
        child.client = _FinalClient()
        return child

    monkeypatch.setattr(agent_loop, "create_session", _create_child)
    tools = build_tools(
        root=tmp_path,
        console=None,
        surface=None,
        store=_Store(),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        api_key="test-key",
        max_steps=4,
        subagents_enabled=True,
        subagent_depth=0,
        subagent_registry={
            "explorer": SubagentDefinition(
                name="explorer",
                description="runtime panel explorer",
                system_prompt="Return a final report.",
                mode="readonly",
            )
        },
    )
    spawned = tools["subagent_spawn"].run({"name": "explorer", "task": "Return a final report."})
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    run_id = spawned["run_id"]
    try:
        joined = tools["subagent_wait"].run({"run_id": run_id, "timeout_s": 2.0})
        assert joined["wait_pending"] is False
        panel_state: dict[str, Any] = {
            "selected_run_id": run_id,
            "run_order": [run_id],
            "cursors": {run_id: 0},
            "entries": {run_id: []},
            "statuses": {},
            "lifecycles": {
                run_id: {
                    "run_id": run_id,
                    "subagent": "explorer",
                    "state": "joined",
                    "collected": True,
                    "outcome": "finished",
                }
            },
            "poll_failures": {},
            "last_poll": None,
        }

        assert _poll_selected_subagent(panel_state, scheduler, now=1.0)
        first_header = _text(
            subagent_panel_rows(
                panel_state["statuses"][run_id],
                panel_state["entries"][run_id],
                width=80,
                height=8,
                position=1,
                total=1,
            )[0]
        )
        sleep(0.03)
        assert _poll_selected_subagent(panel_state, scheduler, now=2.0)
        second_header = _text(
            subagent_panel_rows(
                panel_state["statuses"][run_id],
                panel_state["entries"][run_id],
                width=80,
                height=8,
                position=1,
                total=1,
            )[0]
        )
        assert second_header == first_header

        panel_state["selected_run_id"] = ""
        assert (
            _evict_subagent_panel_runs(
                panel_state,
                departed_run_id=run_id,
            )[0]["run_id"]
            == run_id
        )
        assert scheduler.status(run_id=run_id)["children"][0]["run_id"] == run_id
        panel_state["selected_run_id"] = run_id
        panel_state["run_order"].append(run_id)
        panel_state["cursors"][run_id] = 0
        panel_state["entries"][run_id] = []
        panel_state["last_poll"] = None
        assert _poll_selected_subagent(panel_state, scheduler, now=3.0)
        readded_header = _text(
            subagent_panel_rows(
                panel_state["statuses"][run_id],
                panel_state["entries"][run_id],
                width=80,
                height=8,
                position=1,
                total=1,
            )[0]
        )
        assert readded_header == first_header
    finally:
        scheduler.shutdown(cancel_pending=True)


@pytest.mark.parametrize(
    ("child_outcome", "expected_status"),
    [
        ("success", "finished"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("incomplete", "incomplete"),
    ],
)
def test_navigation_keeps_one_formatted_terminal_child_outcome(
    tmp_path,
    monkeypatch,
    child_outcome: str,
    expected_status: str,
) -> None:
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.output import DummyOutput

    from alysis_code import agent_loop
    from alysis_code.agent_loop import ToolDef, build_tools
    from alysis_code.cli_impl.tui.app import run_tui
    from alysis_code.cli_impl.tui.state import TuiState
    from alysis_code.config import AppConfig
    from alysis_code.subagents import SubagentDefinition

    release_child = threading.Event()
    child_started = threading.Event()

    class _Store:
        session_id = "main-session"
        artifact_persistence_enabled = False
        sessions_dir = tmp_path / "sessions"

        def configure_web_fetch_trusted_domains(self, domains: Any) -> tuple[str, ...]:
            return tuple(domains or ())

        def append(self, _event_type: str, _payload: dict[str, Any]) -> None:
            return

    class _ChildStore:
        session_id = "child-session"

        def __init__(self) -> None:
            if child_outcome == "incomplete":
                self._events = [
                    {
                        "type": "forced_final_summary_fallback",
                        "payload": {"termination_kind": "step_budget_exhausted"},
                    },
                    {
                        "type": "final",
                        "payload": {
                            "content": "Internal stop report.",
                            "internal_fallback": True,
                        },
                    },
                ]
            else:
                self._events = [{"type": "final", "payload": {"content": "Child report."}}]

        def events_snapshot(self) -> list[dict[str, Any]]:
            return list(self._events)

        def events_since(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
            return self._events[cursor:], len(self._events)

    class _Usage:
        def totals(self) -> dict[str, int]:
            return {}

        def records(self) -> list[Any]:
            return []

    class _ChildSession:
        def __init__(self) -> None:
            self.tools = {
                "fs_read": ToolDef(
                    name="fs_read",
                    description="read",
                    parameters={"type": "object", "properties": {}, "required": []},
                    run=lambda _args: {"ok": True},
                )
            }
            self.tool_list = [tool.as_openai_tool() for tool in self.tools.values()]
            self.messages = [{"role": "assistant", "content": "Child report."}]
            self.store = _ChildStore()
            self.usage_summary = _Usage()

        def run_turn(self, _task: str, *, cancellation_token=None) -> int:
            child_started.set()
            while not release_child.wait(timeout=0.01):
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
            return 1 if child_outcome == "failed" else 0

        def close(self) -> None:
            return

    monkeypatch.setattr(agent_loop, "create_session", lambda **_kwargs: _ChildSession())
    runtime: dict[str, Any] = {}

    class _Session:
        def __init__(self, child_scheduler) -> None:
            self.child_scheduler = child_scheduler

        def run_turn(self, text: str, *, cancellation_token=None) -> int:
            return 0

    def _build(surface):
        tools = build_tools(
            root=tmp_path,
            console=None,
            surface=surface,
            store=_Store(),
            mode="auto",
            yes=True,
            cfg=AppConfig(model="test-model"),
            api_key="test-key",
            max_steps=8,
            subagents_enabled=True,
            subagent_registry={
                "explorer": SubagentDefinition(
                    name="explorer",
                    description="runtime lifecycle explorer",
                    system_prompt="Inspect the repository.",
                    mode="readonly",
                    allow_tools=("fs_read",),
                )
            },
        )
        scheduler = tools["subagent_run"].run.__self__.child_scheduler
        runtime.update(tools=tools, scheduler=scheduler)
        return _Session(scheduler)

    def _exercise(app, *args, **kwargs):
        spawned = runtime["tools"]["subagent_spawn"].run(
            {"name": "explorer", "task": "Inspect the repository."}
        )
        assert "error" not in spawned, spawned
        assert child_started.wait(timeout=2.0)
        _feed(app, Keys.Escape, Keys.Right)
        run_id = spawned["run_id"]
        if child_outcome == "cancelled":
            runtime["scheduler"].cancel(run_id=run_id, wait_for_running=True)
        else:
            release_child.set()
            collected = runtime["scheduler"].collect(run_id=run_id, timeout_s=2.0)
            assert not collected["pending_run_ids"], collected
        _feed(app, Keys.Escape, Keys.Right)

    try:
        monkeypatch.setattr(Application, "run", _exercise)
        with create_pipe_input() as pipe:
            _result, transcript = run_tui(
                TuiState(),
                owl_color=False,
                input=pipe,
                output=DummyOutput(),
                session_builder=_build,
            )
    finally:
        release_child.set()
        scheduler = runtime.get("scheduler")
        if scheduler is not None:
            scheduler.shutdown(cancel_pending=True)

    lifecycle_rows = [
        text
        for role, text in transcript
        if role == "trace" and text.startswith("\u21a9 explorer \u00b7 Inspect the repository.")
    ]
    assert len(lifecycle_rows) == 1
    assert f"\u00b7 {expected_status} \u00b7" in lifecycle_rows[0]
    assert ("info", f"explorer {expected_status}") not in transcript
    assert not any("/subagent" in text for _role, text in transcript)
