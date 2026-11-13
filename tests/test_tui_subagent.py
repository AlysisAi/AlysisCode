"""Live subagent TUI identity, status, and panel behavior."""

from __future__ import annotations

from typing import Any

# --------------------------------------------------- minimal in-run identity


def _tui_surface(events: list[str | None]):
    from alysis_code.cli_impl.tui.surface import TuiSurface
    from alysis_code.cli_impl.tui.transcript import TuiTranscript

    t = TuiTranscript()
    s = TuiSurface(t, on_subagent_activity=events.append)
    return t, s


def _start_event(**over: Any) -> Any:
    from alysis_code.surface.types import SubagentStartEvent

    base: dict[str, Any] = {
        "name": "explorer",
        "mode": "readonly",
        "description": (
            "Use this agent when you need to search the codebase broadly. "
            "It reads excerpts rather than whole files, so it locates code."
        ),
    }
    base.update(over)
    return SubagentStartEvent(**base)


def _end_event(**over: Any) -> Any:
    from alysis_code.surface.types import SubagentEndEvent

    base: dict[str, Any] = {
        "name": "explorer",
        "mode": "readonly",
        "status": "success",
        "elapsed_ms": 1200,
        "steps_completed": 3,
    }
    base.update(over)
    return SubagentEndEvent(**base)


def test_subagent_start_renders_one_minimal_identity_line() -> None:
    # Entering a subagent shows stable identity only. Activity comes from the
    # child's runtime events, not a role-specific story.
    from alysis_code.subagents import built_in_subagents

    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event(description=built_in_subagents()["explorer"].description))
    lines = [text for role, text in t.entries if role == "subagent"]
    assert len(lines) == 1
    assert lines[0] == "↪ explorer · readonly"
    assert "Use this" not in lines[0]
    # The badge is pinned and the live status reports the real startup state.
    assert events == ["explorer"]
    assert t.status is not None and "explorer" in t.status
    assert "starting" in t.status


def test_isolated_subagent_has_marker_in_badge_and_status() -> None:
    events: list[str | None] = []
    t, s = _tui_surface(events)

    s.on_subagent_start(_start_event(workspace_view="isolated"))

    assert events == ["[iso] explorer"]
    assert any("↪ [iso] explorer · readonly" in text for _role, text in t.entries)
    assert t.status is not None and "[iso] explorer" in t.status
    s.on_subagent_end(_end_event(workspace_view="isolated"))
    assert events[-1] is None


def test_general_worker_has_builtin_identity_and_report_attribution() -> None:
    from alysis_code.cli_impl.tui.subagent_identity import subagent_identity

    events: list[str | None] = []
    transcript, surface = _tui_surface(events)
    surface.on_subagent_start(_start_event(name="general", mode="review"))
    assert events == ["general"]
    assert any("↪ general · review" in text for _role, text in transcript.entries)
    assert subagent_identity("general").color == "#f0883e"
    surface.on_subagent_end(_end_event(name="general", mode="review"))
    assert events[-1] is None


def test_custom_subagent_start_does_not_narrate_its_definition() -> None:
    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event(name="my-custom"))
    lines = [text for role, text in t.entries if role == "subagent"]
    assert lines == ["↪ my-custom · readonly"]


def test_subagent_badge_shows_even_at_trace_off_but_line_does_not() -> None:
    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.set_trace_level("off")
    s.on_subagent_start(_start_event())
    assert events == ["explorer"]  # identity is not trace — the badge still pins
    assert not any(role == "subagent" for role, _ in t.entries)


def test_subagent_start_without_description_keeps_the_line_short() -> None:
    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event(name="my-custom", description=""))
    lines = [text for role, text in t.entries if role == "subagent"]
    assert lines == ["↪ my-custom · readonly"]


def test_nested_tool_steps_stay_out_of_the_transcript() -> None:
    # The nested run's step-by-step ✓ chatter is what made entering a subagent
    # read as a flood; at the default trace level it lives only in the live
    # status line (named, so the user knows who is working).
    from alysis_code.surface.types import ToolEndEvent, ToolStartEvent

    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event())
    before = list(t.entries)
    s.on_tool_start(
        ToolStartEvent(
            tool_call_id="sub:1",
            name="web_search",
            args={"query": "auth flow"},
            step=1,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    assert t.status is not None
    assert "explorer" in t.status and "auth flow" in t.status
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id="sub:1",
            name="web_search",
            status="done",
            elapsed_ms=10,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    assert t.entries == before  # no ✓ line committed for the nested success
    assert t.status is not None and "explorer" in t.status
    assert "Search Web" in t.status and "complete" in t.status
    assert "charting the codebase" not in t.status


def test_nested_tool_failures_keep_their_error_line_with_attribution() -> None:
    from alysis_code.surface.types import ToolEndEvent, ToolStartEvent

    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event())
    s.on_tool_start(
        ToolStartEvent(
            tool_call_id="sub:2",
            name="web_search",
            args={"query": "auth flow"},
            step=1,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id="sub:2",
            name="web_search",
            status="error",
            elapsed_ms=10,
            meta={"error": "network unreachable"},
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    errors = [text for role, text in t.entries if role == "error"]
    assert len(errors) == 1
    assert errors[0].startswith("✗ explorer ▸ ")
    assert "network unreachable" in errors[0]
    # The argument preview survives so the user can tell WHICH invocation
    # failed when the agent retries the same tool with different arguments.
    assert "auth flow" in errors[0]


def test_nested_remote_site_outcome_stays_out_of_the_transcript() -> None:
    # A site that refused the subagent's fetch is not a failure: like a nested
    # success it only updates the live status (with what the site did), and
    # never paints a red ✗ line.
    from alysis_code.surface.types import ToolEndEvent, ToolStartEvent

    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event())
    s.on_tool_start(
        ToolStartEvent(
            tool_call_id="sub:fetch",
            name="web_fetch",
            args={"url": "https://www.wsj.com/"},
            step=1,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id="sub:fetch",
            name="web_fetch",
            status="failed",
            elapsed_ms=10,
            meta={
                "error": "HTTP error 401 while fetching 'https://www.wsj.com/': …",
                "blocked_by_remote_site": True,
                "remote_site_reason": "site requires sign-in",
            },
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    assert not any(role in ("error", "warn", "trace") for role, _ in t.entries)
    assert t.status is not None
    assert "site requires sign-in" in t.status
    assert "failed" not in t.status


def test_trace_off_keeps_nested_activity_quiet() -> None:
    # /trace off means a quiet surface: nested tool events show the generic
    # "Working…" (start) and clear (end) — never a named "↪ …" status line —
    # and the subagent's end never strands a leftover status.
    from alysis_code.surface.types import ToolEndEvent, ToolStartEvent

    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.set_trace_level("off")
    s.on_subagent_start(_start_event())
    s.on_tool_start(
        ToolStartEvent(
            tool_call_id="sub:off",
            name="web_search",
            args={"query": "auth flow"},
            step=1,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    assert t.status == "Working…"  # generic, not "↪ explorer · …"
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id="sub:off",
            name="web_search",
            status="done",
            elapsed_ms=10,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    assert t.status is None
    s.on_subagent_end(_end_event())
    assert t.status is None  # nothing stranded after the run
    assert not any(role in ("subagent", "trace") for role, _ in t.entries)


def test_full_trace_level_opts_back_into_nested_detail() -> None:
    from alysis_code.surface.types import ToolEndEvent, ToolStartEvent

    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.set_trace_level("full")
    s.on_subagent_start(_start_event())
    s.on_tool_start(
        ToolStartEvent(
            tool_call_id="sub:3",
            name="web_search",
            args={"query": "auth flow"},
            step=1,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id="sub:3",
            name="web_search",
            status="done",
            elapsed_ms=10,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    assert any(role == "trace" and text.startswith("✓") for role, text in t.entries)


def test_tui_subagent_identity_precedes_first_nested_tool_trace() -> None:
    from alysis_code.surface.types import ToolStartEvent

    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.set_trace_level("full")
    s.on_subagent_start(_start_event())
    s.on_tool_start(
        ToolStartEvent(
            tool_call_id="sub:ordered",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )

    visible_roles = [role for role, _text in t.entries if role in {"subagent", "trace"}]
    assert visible_roles[:2] == ["subagent", "trace"]


def test_subagent_end_clears_badge_and_appends_finish_line() -> None:
    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event())
    s.on_subagent_end(_end_event())
    assert events == ["explorer", None]
    assert any(
        role == "trace" and text.startswith("↩ explorer · finished · 3 steps")
        for role, text in t.entries
    )
    assert t.status is None


def test_incomplete_subagent_end_keeps_truthful_terminal_label() -> None:
    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event(workspace_view="isolated"))
    s.on_subagent_end(
        _end_event(
            status="incomplete",
            workspace_view="isolated",
            error="Stopped at the child step ceiling; the retained run can be resumed.",
        )
    )

    assert events == ["[iso] explorer", None]
    assert any(
        role == "trace" and text.startswith("↩ [iso] explorer · incomplete · 3 steps")
        for role, text in t.entries
    )


def test_new_turn_clears_a_stranded_subagent_badge() -> None:
    # An interrupted run may never deliver its end event; the next submission
    # must not inherit its badge.
    events: list[str | None] = []
    _t, s = _tui_surface(events)
    s.on_subagent_start(_start_event())
    s.on_user_message("next question")
    assert events == ["explorer", None]


def test_footer_count_badge_handles_zero_and_one_subagent() -> None:
    from alysis_code.cli_impl.tui.footer import footer_fragments
    from alysis_code.cli_impl.tui.state import TuiState

    def _plain(fragments) -> str:
        return "".join(text for _style, text in fragments)

    assert TuiState().active_subagent == ""
    idle = _plain(footer_fragments(TuiState(model_name="m", username="u"), width=120))
    assert "\u21aa" not in idle
    busy_state = TuiState(model_name="m", username="u", active_subagent="explorer")
    busy = _plain(footer_fragments(busy_state, width=120))
    assert "\u21aa 1 subagent" in busy


def test_footer_count_badge_uses_the_existing_fixed_style() -> None:
    from alysis_code.cli_impl.tui.footer import footer_fragments
    from alysis_code.cli_impl.tui.state import TuiState

    fragments = footer_fragments(TuiState(model_name="m", active_subagent="explorer"), width=120)
    badge_style = next(style for style, text in fragments if "\u21aa" in text)

    assert badge_style == "class:tui.footer.subagent"


def test_footer_shows_one_plural_count_for_concurrent_subagents() -> None:
    from alysis_code.cli_impl.tui.footer import footer_fragments
    from alysis_code.cli_impl.tui.state import TuiState

    fragments = footer_fragments(
        TuiState(
            model_name="m",
            active_subagent="reviewer",
            active_subagents=("explorer", "debugger", "reviewer"),
        ),
        width=160,
    )
    badges = [(style, text) for style, text in fragments if "\u21aa" in text]

    assert badges == [("class:tui.footer.subagent", "\u21aa 3 subagents")]


def test_footer_with_six_subagents_never_overflows_eighty_columns() -> None:
    from alysis_code.cli_impl.tui.footer import footer_fragments
    from alysis_code.cli_impl.tui.state import TuiState

    fragments = footer_fragments(
        TuiState(
            model_name="test-model",
            username="tester",
            workspace="~/workspace",
            branch="feat/subagents",
            active_subagents=tuple(f"agent-{index}" for index in range(6)),
        ),
        width=80,
    )
    rendered = "".join(text for _style, text in fragments)

    assert "\u21aa 6 subagents" in rendered
    assert all(len(line) <= 80 for line in rendered.splitlines())


def test_surface_reports_all_concurrent_subagent_names_additively() -> None:
    from alysis_code.cli_impl.tui.surface import TuiSurface
    from alysis_code.cli_impl.tui.transcript import TuiTranscript

    legacy_events: list[str | None] = []
    concurrent_events: list[tuple[str, ...]] = []
    surface = TuiSurface(
        TuiTranscript(),
        on_subagent_activity=legacy_events.append,
        on_subagent_activities=concurrent_events.append,
    )
    surface.on_subagent_start(_start_event(name="explorer"))
    surface.on_subagent_start(_start_event(name="debugger"))
    surface.on_subagent_end(_end_event(name="explorer"))
    surface.on_subagent_end(_end_event(name="debugger"))

    assert legacy_events == ["explorer", "debugger", "debugger", None]
    assert concurrent_events == [
        ("explorer",),
        ("explorer", "debugger"),
        ("debugger",),
        (),
    ]


def test_subagent_activity_elapsed_uses_child_start_not_parent_turn_start() -> None:
    from alysis_code.cli_impl.tui.app import (
        _activity_elapsed_seconds,
        _activity_rows,
        _sync_subagent_started_at,
    )

    started_at: dict[str, float] = {}
    _sync_subagent_started_at(started_at, ("code-reviewer",), now=900.0)

    assert (
        _activity_elapsed_seconds(
            turn_started=2.0,
            cancellation_started=0.0,
            active_subagent="code-reviewer",
            subagent_started_at=started_at,
            now=923.0,
        )
        == 23
    )
    assert (
        _activity_elapsed_seconds(
            turn_started=2.0,
            cancellation_started=0.0,
            active_subagent="",
            subagent_started_at=started_at,
            now=923.0,
        )
        == 921
    )
    assert _activity_rows(
        "x",
        "explorer \u00b7 Search Workspace",
        23,
        elapsed_is_run_time=True,
    )[0][-1][1].endswith("\u00b7 run time 23s")


def test_cancellation_activity_uses_its_own_clock() -> None:
    from alysis_code.cli_impl.tui.app import _activity_elapsed_seconds, _activity_rows

    elapsed = _activity_elapsed_seconds(
        turn_started=2.0,
        cancellation_started=918.5,
        active_subagent="explorer",
        subagent_started_at={"explorer": 800.0},
        now=923.0,
    )

    assert elapsed == 4
    assert _activity_rows(
        "x",
        "Cancelling current turn…",
        elapsed,
        elapsed_is_run_time=False,
    )[0][-1][1].endswith(" 4s")


def test_concurrent_subagents_keep_the_badge_and_status_honest() -> None:
    # A parallel readonly batch really does run several subagents against the
    # SAME surface (turn/core.py prelaunch path), so the badge must survive one
    # of them finishing: pop is by name, the badge falls back to whoever is
    # still running, and the status only clears when the last one ends.
    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event(name="explorer"))
    s.on_subagent_start(_start_event(name="debugger"))
    assert events == ["explorer", "debugger"]
    # The FIRST agent finishes while the second is still working: the badge
    # re-pins to the survivor (not None, not the finished name) and the live
    # status is not wiped mid-run.
    s.on_subagent_end(_end_event(name="explorer"))
    assert events[-1] == "debugger"
    # The activity line rolls to the survivor — never left attributed to the
    # agent whose ↩ line just printed.
    assert t.status is not None and "debugger" in t.status
    # The last agent finishes: badge clears, status clears.
    s.on_subagent_end(_end_event(name="debugger"))
    assert events[-1] is None
    assert t.status is None


def test_outer_activity_names_the_followed_task_label() -> None:
    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event(name="explorer", label="explore-plan"))
    s.on_subagent_start(_start_event(name="debugger", label="explore-verify"))

    assert t.status is not None
    assert "following explore-verify" in t.status
    assert "explore-plan" not in t.status


def test_end_event_with_no_matching_start_is_dropped_whole() -> None:
    # A stray end (a failure path that already reported, or an abandoned turn's
    # agent finishing late) must not pop someone else's badge, print a phantom
    # ↩ line, or touch the status.
    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event(name="explorer"))
    s.on_subagent_end(_end_event(name="debugger"))
    assert events == ["explorer"]  # no badge re-sync for the stray end
    assert not any("↩ debugger" in text for _r, text in t.entries)


def test_stale_end_from_another_thread_cannot_pop_a_live_same_named_agent() -> None:
    # After an interrupt the abandoned turn's pool threads may deliver end
    # events for the SAME name the next turn is running. A run's start and end
    # share a thread, so a foreign thread's end must find no entry and be
    # dropped — the live agent keeps its badge, status word, and ↩-less
    # transcript.
    import threading

    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event(name="explorer"))
    stale = threading.Thread(target=lambda: s.on_subagent_end(_end_event(name="explorer")))
    stale.start()
    stale.join()
    assert events == ["explorer"]  # badge untouched (no None, no re-pin)
    assert not any(text.startswith("↩ explorer") for _r, text in t.entries)
    # The live run can still end normally on its own thread.
    s.on_subagent_end(_end_event(name="explorer"))
    assert events == ["explorer", None]


def test_clear_subagent_activity_makes_later_ends_silent() -> None:
    # The soft-interrupt path: the app clears the surface's live-subagent state;
    # the abandoned run's late end must then be a no-op instead of re-pinning a
    # sibling into an idle footer.
    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event(name="explorer"))
    s.on_subagent_start(_start_event(name="debugger"))
    s.clear_subagent_activity()
    assert events[-1] is None
    before = list(t.entries)
    s.on_subagent_end(_end_event(name="debugger"))
    assert events[-1] is None  # no sibling re-pin after the clear
    assert t.entries == before  # no phantom ↩ line either


def test_nested_approval_declined_is_reported_as_the_users_decision() -> None:
    # Declining an approval inside a subagent is the USER's call — it must read
    # "approval declined", never be misreported as a tool failure.
    from alysis_code.surface.types import ToolEndEvent

    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event())
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id="sub:4",
            name="shell_run",
            status="failed",
            elapsed_ms=10,
            meta={"approval_declined": True, "error": "approval declined by user"},
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    errors = [text for role, text in t.entries if role == "error"]
    assert len(errors) == 1
    assert errors[0].startswith("✗ explorer ▸ ")
    assert "approval declined" in errors[0]
    assert "failed" not in errors[0]


def test_headless_run_tui_pins_the_badge_and_the_turn_end_backstop_clears_it() -> None:
    # The unit tests above hand TuiSurface a fake callback; this drives the REAL
    # run_tui wiring: on_subagent_activity → state.active_subagent while the
    # turn runs, and the worker's turn-end backstop clearing it even when the
    # end event never arrives (interrupted/crashed nested run).
    from alysis_code.cli_impl.tui import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    state = TuiState(model_name="test-model", username="t")
    seen_mid_turn: list[str] = []
    seen_mid_turn_all: list[tuple[str, ...]] = []

    class _SpawningSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            self.surface.on_user_message(text)
            self.surface.on_subagent_start(_start_event())
            # The badge must be live in the shared state WHILE the subagent
            # works — this is the real wiring, no hand-made callback.
            seen_mid_turn.append(state.active_subagent)
            seen_mid_turn_all.append(state.active_subagents)
            # No on_subagent_end: simulate a nested run that never reports back.
            self.surface.on_assistant_message_done("done")
            return 0

        def close(self) -> None:
            return None

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text("spawn something\r/exit\r")
        run_tui(
            state,
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=lambda surface: _SpawningSession(surface),
            background_turns=False,
        )

    assert seen_mid_turn == ["explorer"]
    assert seen_mid_turn_all == [("explorer",)]
    assert state.active_subagent == ""  # turn-end backstop cleared the orphan
    assert state.active_subagents == ()


# ------------------------------------------------------- per-agent identity


def test_builtin_subagent_identities_are_distinct() -> None:
    from alysis_code.cli_impl.tui.subagent_identity import subagent_identity
    from alysis_code.subagents import built_in_subagents

    names = sorted(built_in_subagents())
    identities = [subagent_identity(name) for name in names]
    # Every built-in wears its own accent; role-specific narration is absent.
    assert len({ident.color for ident in identities}) == len(names)
    assert all(not hasattr(ident, "tagline") for ident in identities)


def test_subagent_identity_resolves_aliases_and_is_deterministic_for_customs() -> None:
    from alysis_code.cli_impl.tui.subagent_identity import subagent_identity

    # "explore" is an alias of "explorer" — same identity, not a custom one.
    assert subagent_identity("explore") == subagent_identity("explorer")
    assert subagent_identity(" Explorer ") == subagent_identity("explorer")
    # A custom agent gets the SAME colour every time (name-derived, not random).
    custom = subagent_identity("my-custom")
    assert custom == subagent_identity("my-custom")
    assert custom.color.startswith("#")
    # Nonsense input must never raise.
    assert subagent_identity("").color.startswith("#")


def test_identity_accents_never_impersonate_fixed_marks() -> None:
    # Violet is Forge, green the chat accent, and cyan the brand. No subagent
    # may wear any of them, or a running agent would read as a mode indicator.
    from alysis_code.cli_impl.tui.subagent_identity import (
        _BUILTIN_IDENTITIES,
        _FALLBACK_COLORS,
    )

    reserved_colors = {"#bc8cff", "#3fb950", "#56b6c2"}
    for name, ident in _BUILTIN_IDENTITIES.items():
        assert ident.color.lower() not in reserved_colors, name
    for color in _FALLBACK_COLORS:
        assert color.lower() not in reserved_colors


def test_footer_badge_styles_are_valid_prompt_toolkit_styles() -> None:
    # A malformed accent would crash only the live TUI (tests read plain text);
    # parse every builtin's badge style plus a custom one through the real
    # prompt_toolkit style machinery.
    from prompt_toolkit.styles import Style

    from alysis_code.cli_impl.tui.footer import footer_fragments
    from alysis_code.cli_impl.tui.state import TuiState
    from alysis_code.cli_impl.tui.subagent_identity import _BUILTIN_IDENTITIES

    style = Style([])
    for name in [*_BUILTIN_IDENTITIES, "my-custom"]:
        fragments = footer_fragments(TuiState(model_name="m", active_subagent=name), width=120)
        badge_style = next(s for s, text in fragments if "↪" in text)
        style.get_attrs_for_style_str(badge_style)  # must not raise


def test_shadowed_builtin_spawn_line_keeps_stable_identity_only() -> None:
    from alysis_code.surface.types import ToolEndEvent

    events: list[str | None] = []
    t, s = _tui_surface(events)
    s.on_subagent_start(_start_event(name="explorer", description="Audits license headers only."))
    lines = [text for role, text in t.entries if role == "subagent"]
    assert lines == ["↪ explorer · readonly"]
    assert "Audits license headers only" not in lines[0]
    assert t.status is not None and "charting the codebase" not in t.status
    # Between steps, the actual last tool outcome remains visible instead of a
    # role tagline flashing back into place.
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id="sub:9",
            name="fs_read",
            status="done",
            elapsed_ms=5,
            subagent_name="explorer",
            subagent_mode="readonly",
            nesting_depth=1,
        )
    )
    assert t.status is not None and "charting the codebase" not in t.status
    assert "Read File" in t.status and "complete" in t.status


def test_live_subagent_surface_wears_no_per_agent_symbol() -> None:
    # A subagent is identified by its NAME, never by a mark of its own. The
    # shared ↪/↩ marks (a nested run started / ended) are the only symbols any
    # subagent surface may carry; a per-agent glyph creeping back into the
    # live run surface is the regression this pins.
    from alysis_code.subagents import built_in_subagents

    banned = set("▣✧◎✦◉❖△◆◇")
    registry = built_in_subagents()

    rendered: list[str] = []
    for name in [*registry, "my-custom"]:
        events: list[str | None] = []
        t, s = _tui_surface(events)
        s.on_subagent_start(_start_event(name=name))
        rendered.extend(text for role, text in t.entries if role == "subagent")

    assert rendered
    for text in rendered:
        assert not (set(text) & banned), text
