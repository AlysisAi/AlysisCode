"""Tests for the TUI-native command panels & pickers.

Covers (1) the structured panel-spec builders in ``commands/chat_tui_panels.py``
with plain fake sessions, and (2) the ``run_tui`` interception contract: a
``panel_providers`` provider is called with the command argument, opens the
centered popup when it returns a spec, and falls through to the command runner
(or a picker) when it returns ``None``.
"""

from __future__ import annotations

from types import SimpleNamespace

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code.cli_impl.commands import chat_tui_panels as panels
from alysis_code.cli_impl.commands.chat_status import _chat_mode_status_label
from alysis_code.cli_impl.tui import run_tui
from alysis_code.cli_impl.tui.state import TuiState

# --------------------------------------------------------------- spec builders


class _FakeSummary:
    def by_model_rows(self):
        return [
            {
                "model": "deepseek-chat",
                "prompt_tokens": 1200,
                "completion_tokens": 300,
                "total_tokens": 1500,
                "cost_usd": 0.01,
                "known_cost_calls": 1,
            }
        ]

    def totals(self):
        return {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "total_tokens": 1500,
            "cost_usd": 0.01,
            "known_cost_calls": 1,
            "unknown_cost_calls": 0,
        }


def _values(spec) -> str:
    return " ".join(f"{k} {v}" for _name, rows in spec["sections"] for (k, v, _tone) in rows)


def _tones(spec) -> set[str]:
    return {tone for _name, rows in spec["sections"] for (_k, _v, tone) in rows}


def test_usage_panel_spec_has_per_model_and_total():
    sess = SimpleNamespace(usage_summary=_FakeSummary())
    spec = panels._chat_usage_panel_spec(session=sess)
    assert spec["title"] == "Usage"
    assert [name for name, _ in spec["sections"]] == ["Per model", "Total"]
    blob = _values(spec)
    assert "deepseek-chat" in blob
    assert "1,500" in blob  # exact total tokens, formatted
    assert "accent" in _tones(spec)  # healthy totals are green


def test_usage_panel_spec_empty_session():
    sess = SimpleNamespace(usage_summary=None)
    spec = panels._chat_usage_panel_spec(session=sess)
    assert "unavailable" in _values(spec).lower()


def test_context_panel_spec_unavailable_when_no_context_left():
    sess = SimpleNamespace(context_left=None)
    spec = panels._chat_context_panel_spec(session=sess)
    assert spec["title"] == "Context Window"
    assert "unavailable" in _values(spec).lower()


def test_context_panel_tone_matches_displayed_window_percent_not_dynamic():
    # Regression: the "left %" row must be coloured from the percent it DISPLAYS
    # (the context-window percent), not the lower dynamic-budget percent — else a
    # healthy 60%-left window gets painted red.
    ctx = SimpleNamespace(
        model_name="m",
        source="catalog",
        context_window_tokens=64000,
        context_window_remaining_tokens=38000,
        context_window_percent_left=60.0,  # healthy window
        dynamic_context_percent_left=5.0,  # but dynamic budget is low
        percent_left=60.0,
        used_input_tokens=26000,
    )
    sess = SimpleNamespace(context_left=lambda: ctx, _hud_context_cache=None)
    spec = panels._chat_context_panel_spec(session=sess)
    left_pct = [
        (v, tone) for _name, rows in spec["sections"] for (k, v, tone) in rows if k == "left %"
    ]
    assert left_pct == [("60.0%", "accent")]  # displays & colours the window %


def test_context_panel_surfaces_effective_budget_separately_from_window_percent():
    ctx = SimpleNamespace(
        model_name="m",
        source="catalog",
        context_window_tokens=64000,
        context_window_remaining_tokens=38000,
        context_window_percent_left=60.0,
        effective_input_budget=28000,
        effective_remaining_tokens=1200,
        effective_percent_left=4.285,
        startup_baseline_tokens=24000,
        dynamic_context_budget_tokens=4000,
        dynamic_context_used_tokens=2800,
        dynamic_context_remaining_tokens=1200,
        dynamic_context_percent_left=30.0,
        percent_left=60.0,
        used_input_tokens=26000,
    )
    sess = SimpleNamespace(context_left=lambda: ctx, _hud_context_cache=None)

    spec = panels._chat_context_panel_spec(session=sess)

    assert [name for name, _rows in spec["sections"]] == [
        "Provider Usage · Last Response",
        "Context",
        "Effective Input Budget",
        "Conversation Context",
    ]
    rows = {
        k: (v, tone) for _name, section_rows in spec["sections"] for (k, v, tone) in section_rows
    }
    assert rows["left %"] == ("60.0%", "accent")
    assert rows["input budget"] == ("28000", "plain")
    assert rows["budget left"] == ("1200", "plain")
    assert rows["budget left %"] == ("4.3%", "err")
    assert rows["conversation left %"] == ("30.0%", "accent")


def test_usage_panel_unknown_cost_not_green():
    # An entirely-unknown total cost must not read as healthy green.
    class _Sum:
        def by_model_rows(self):
            return [
                {
                    "model": "m",
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "known_cost_calls": 0,
                    "unknown_cost_count": 1,
                }
            ]

        def totals(self):
            return {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "known_cost_calls": 0,
                "unknown_cost_calls": 1,
                "corrected_usage_calls": 0,
            }

    spec = panels._chat_usage_panel_spec(session=SimpleNamespace(usage_summary=_Sum()))
    cost = [
        (v, tone) for _name, rows in spec["sections"] for (k, v, tone) in rows if k == "billing"
    ]
    assert cost and cost[0][1] != "accent"  # not painted healthy-green


def test_usage_panel_labels_subscription_and_unreported_cache_fields() -> None:
    class _Sum:
        def by_model_rows(self):
            return [
                {
                    "model": "gpt-subscription",
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    "known_cost_calls": 0,
                    "unknown_cost_count": 0,
                    "subscription_calls": 2,
                    "api_usage_calls": 1,
                    "estimate_usage_calls": 1,
                    "cache_read_input_tokens": 0,
                    "cache_read_reported_calls": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_creation_reported_calls": 0,
                }
            ]

        def totals(self):
            return {
                **self.by_model_rows()[0],
                "unknown_cost_calls": 0,
                "corrected_usage_calls": 0,
            }

    spec = panels._chat_usage_panel_spec(session=SimpleNamespace(usage_summary=_Sum()))
    blob = _values(spec)
    assert "subscription" in blob
    assert "cache read 0" in blob
    assert "partial 1/2" in blob
    assert "cache write not reported" in blob
    assert "processed is cumulative across calls, not current context" in blob


def test_usage_panel_warns_when_tool_schema_shadow_budget_is_exceeded():
    class _Sum:
        def by_model_rows(self):
            return [
                {
                    "model": "m",
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "known_cost_calls": 1,
                    "unknown_cost_count": 0,
                }
            ]

        def totals(self):
            return {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "known_cost_calls": 1,
                "unknown_cost_calls": 0,
                "request_token_estimate": {
                    "tool_schema_budget_exceeded_calls": 2,
                    "tool_schema_budget_overage_tokens": 12000,
                    "tool_schema_largest_tool_tokens": 9000,
                },
            }

    spec = panels._chat_usage_panel_spec(session=SimpleNamespace(usage_summary=_Sum()))
    blob = _values(spec)
    assert "tool schema shadow budget" in blob
    assert "12,000" in blob
    assert "warn" in _tones(spec)


def test_context_panel_spec_reads_cache_and_tones_low_percent():
    ctx = SimpleNamespace(
        model_name="deepseek-chat",
        source="catalog",
        context_window_tokens=64000,
        context_window_remaining_tokens=3000,
        context_window_percent_left=5.0,
        used_input_tokens=61000,
        percent_left=5.0,
    )
    sess = SimpleNamespace(context_left=lambda: ctx, _hud_context_cache=None)
    spec = panels._chat_context_panel_spec(session=sess)
    blob = _values(spec)
    assert "deepseek-chat" in blob and "64000" in blob
    assert "err" in _tones(spec)  # <10% left → red


def test_model_info_panel_spec_resolves_metadata():
    meta = SimpleNamespace(
        model_name="deepseek-chat",
        source="catalog",
        context_window_tokens=64000,
        max_output_tokens=8192,
        supports_vision=False,
        input_cost_per_token=0.0,
        output_cost_per_token=0.0,
        field_sources={"context_window_tokens": "catalog"},
        warnings=(),
    )
    registry = SimpleNamespace(get=lambda name: meta, last_error=None)
    sess = SimpleNamespace(client=SimpleNamespace(model="deepseek-chat"), model_registry=registry)
    spec = panels._chat_model_info_panel_spec(session=sess)
    blob = _values(spec)
    assert "deepseek-chat" in blob and "64000" in blob and "8192" in blob


def test_model_info_panel_spec_missing_registry():
    sess = SimpleNamespace(client=SimpleNamespace(model="m"), model_registry=None)
    spec = panels._chat_model_info_panel_spec(session=sess)
    assert "unavailable" in _values(spec).lower()


def test_config_panel_spec_lists_tracked_models():
    meta = SimpleNamespace(
        context_window_tokens=64000, max_output_tokens=8192, source="catalog", field_sources={}
    )
    registry = SimpleNamespace(get=lambda name: meta)
    sess = SimpleNamespace(
        client=SimpleNamespace(model="deepseek-chat"),
        cfg=SimpleNamespace(model="deepseek-chat"),
        usage_summary=None,
        model_registry=registry,
    )
    spec = panels._chat_config_panel_spec(session=sess)
    assert spec["title"] == "Model Config"
    blob = _values(spec)
    assert "deepseek-chat" in blob
    assert "/config set" in blob  # manage usage line present


def test_status_mode_label_matches_tui_mode_label():
    assert _chat_mode_status_label("auto") == "fast (auto)"


def test_toolbar_panel_spec_active_available():
    sess = SimpleNamespace(cfg=SimpleNamespace(toolbar_items=["mode", "model"]))
    spec = panels._chat_toolbar_panel_spec(session=sess)
    blob = _values(spec)
    assert "active" in blob and "mode" in blob
    assert "/toolbar add" in blob


def test_terminals_panel_spec_lists_processes_and_tones_status():
    summaries = [
        SimpleNamespace(
            process_id="p1", cmd="sleep 5", status="running", exit_code=None, runtime_s=2.5
        ),
        SimpleNamespace(process_id="p2", cmd="false", status="failed", exit_code=1, runtime_s=0.1),
    ]
    sess = SimpleNamespace(terminal_manager=SimpleNamespace(list=lambda: summaries))
    spec = panels._chat_terminals_panel_spec(session=sess)
    blob = _values(spec)
    assert "p1" in blob and "p2" in blob and "sleep 5" in blob
    tones = _tones(spec)
    assert "accent" in tones  # running
    assert "err" in tones  # failed


def test_terminals_panel_spec_unavailable():
    sess = SimpleNamespace(terminal_manager=None)
    spec = panels._chat_terminals_panel_spec(session=sess)
    assert "unavailable" in _values(spec).lower()


def test_skill_listing_panel_spec_lists_skills():
    skills = [
        SimpleNamespace(name="docx", description="Word docs"),
        SimpleNamespace(name="pdf", description="PDF tools"),
    ]
    sess = SimpleNamespace(
        cfg=None, skills_ordered=tuple(skills), skill_registry={}, skill_discovery_issues=()
    )
    spec = panels._chat_skill_listing_panel_spec(session=sess)
    blob = _values(spec)
    assert "docx" in blob and "pdf" in blob


def test_skill_listing_panel_spec_empty():
    sess = SimpleNamespace(
        cfg=None, skills_ordered=(), skill_registry={}, skill_discovery_issues=()
    )
    spec = panels._chat_skill_listing_panel_spec(session=sess)
    assert "no skills" in _values(spec).lower()


def test_short_subagent_desc_strips_boilerplate_and_keeps_first_clause():
    # The "Use this when you need to …: …" lead-in is stripped; only the crisp
    # first clause remains, capitalised.
    desc = (
        "Use this when you need to investigate the repository: find files, trace "
        "flows, and report what to inspect next."
    )
    assert panels._short_subagent_desc(desc) == "Investigate the repository"


def test_short_subagent_desc_catch_all_form():
    desc = "Catch-all subagent for tasks that do not fit a more specific agent."
    out = panels._short_subagent_desc(desc)
    assert out.startswith("Tasks that do not fit")
    assert len(out) <= 46


def test_short_subagent_desc_truncates_long_clause():
    desc = "Use this when you need a " + "really " * 40 + "long thing"
    out = panels._short_subagent_desc(desc, limit=46)
    assert len(out) <= 46 and out.endswith("…")


def test_short_subagent_desc_collapses_whitespace_and_handles_empty():
    assert panels._short_subagent_desc("  multi\n  line   text  ") == "Multi line text"
    assert panels._short_subagent_desc("") == ""
    assert panels._short_subagent_desc(None) == ""


def test_picker_rows_wrap_long_description_capped_and_full_width():
    # A description that does not fit on one line WRAPS onto continuation lines
    # (aligned under the description column) instead of clipping on the right,
    # and the option never grows past _PICKER_MAX_DESC_LINES lines.
    from alysis_code.cli_impl.tui.app import _PICKER_MAX_DESC_LINES, _render_picker_rows

    rows = [
        {"label": "explorer", "description": "Investigate the repository", "value": "explorer"},
        {
            "label": "reviewer",
            "description": "Strict second opinion on proposed or recent code changes",
            "value": "reviewer",
        },
    ]
    width = 50
    rendered = _render_picker_rows(rows, 1, width)
    # Every rendered line is exactly the panel width (highlight band stays aligned).
    assert all(sum(len(t) for _s, t in row) == width for row in rendered)
    # The reviewer description is fully present across wrapped lines (no "…" clip).
    joined = " ".join("".join(t for _s, t in row) for row in rendered)
    assert "changes" in joined and "…" not in joined
    # A pathological long description is capped (never unbounded).
    big = [{"label": "x", "description": "word " * 200, "value": "x"}]
    big_rows = _render_picker_rows(big, 0, width)
    desc_lines = [
        r
        for r in big_rows
        if any(s in ("class:tui.picker.desc", "class:tui.picker.seldesc") for s, _t in r)
    ]
    assert len(desc_lines) <= _PICKER_MAX_DESC_LINES


def test_picker_rows_tag_override_replaces_current_tag():
    # A row's "tag" key overrides the "(current)" tag text: "(default)" renders
    # in the tag slot, and an explicit empty tag suppresses the tag even on a
    # row flagged current (the Forge launch gate relies on both — its focused
    # row must not echo a redundant "(current)" next to the highlight band).
    from alysis_code.cli_impl.tui.app import _render_picker_rows

    rows = [
        {
            "label": "Launch now",
            "description": "Build 2 task(s).",
            "value": "go",
            "current": True,
            "tag": "",
        },
        {
            "label": "Verify: warn",
            "description": "Failed checks are reported.",
            "value": "verify",
            "tag": "(default)",
        },
        {
            "label": "Scope: strict",
            "description": "Own files only.",
            "value": "scope",
            "current": True,
        },
    ]
    rendered = _render_picker_rows(rows, 0, 60)
    joined = " ".join("".join(t for _s, t in row) for row in rendered)
    assert "(default)" in joined
    # A row without a "tag" key keeps the legacy "(current)" behaviour; the
    # launch row's explicit empty tag suppressed its own.
    assert joined.count("(current)") == 1


def test_picker_tag_never_orphans_onto_its_own_line():
    # A tag that does not fit after the last description line pulls the last
    # word of the description down with it, so the continuation reads as a
    # natural wrap instead of a lone, misaligned "(default)" line.
    from alysis_code.cli_impl.tui.app import _render_picker_rows

    # width 25, label "L" → desc column at 8, desc width 17: "alpha beta gamma"
    # (16) fits on one line, but not together with " (default)" (10).
    rows = [{"label": "L", "description": "alpha beta gamma", "value": "x", "tag": "(default)"}]
    rendered = _render_picker_rows(rows, 0, 25)
    lines = ["".join(t for _s, t in row) for row in rendered]
    assert any("gamma (default)" in line for line in lines)
    assert not any(line.strip() == "(default)" for line in lines)


def test_picker_hint_wraps_and_honours_linebreaks_no_clip():
    # A long, two-line hint must wrap (never clip on the right) and keep both
    # segments fully intact.
    from alysis_code.cli_impl.tui.app import _render_picker_rows

    rows = [{"label": "explorer", "description": "investigate", "value": "explorer"}]
    hint = "↑↓ select · Enter to spawn · Esc cancel\nauto-delegate off · configure before restart"
    width = 50
    rendered = _render_picker_rows(rows, 0, width, hint)
    assert all(sum(len(t) for _s, t in row) == width for row in rendered)
    joined = " ".join("".join(t for _s, t in row) for row in rendered)
    # Both the keybinding line and the full status note survive (nothing truncated).
    assert "Esc cancel" in joined
    assert "configure before restart" in joined


# ------------------------------------------------- float geometry (width drift)


def test_float_chrome_constants_match_prompt_toolkit():
    # _FRAME_COLS / _SCROLLBAR_COLS are what every popup's content width is
    # derived from, so pin them against what prompt_toolkit ACTUALLY paints
    # rather than trusting the arithmetic: a Frame costs 2 content columns, and a
    # ScrollbarMargin costs 1 more. If an upgrade changes either, the popups
    # would silently start re-wrapping again — fail here instead.
    from prompt_toolkit.layout.containers import Float, FloatContainer, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.margins import ScrollbarMargin
    from prompt_toolkit.widgets import Frame

    from alysis_code.cli_impl.tui.app import _FRAME_COLS, _SCROLLBAR_COLS

    outer = 60
    plain = Window(FormattedTextControl(lambda: []))
    scrolling = Window(FormattedTextControl(lambda: []), right_margins=[ScrollbarMargin()])
    root = FloatContainer(
        content=Window(),
        floats=[
            Float(content=Frame(plain), width=outer, height=10, top=0, left=0),
            Float(content=Frame(scrolling), width=outer, height=10, top=12, left=0),
        ],
    )
    _render_container(root, cols=100, rows=40)

    assert plain.render_info is not None and scrolling.render_info is not None
    assert plain.render_info.window_width == outer - _FRAME_COLS
    assert scrolling.render_info.window_width == outer - _FRAME_COLS - _SCROLLBAR_COLS


def test_every_float_content_width_derives_from_its_own_float():
    # The bug this pins: the picker's rows were built at the HELP panel's width.
    # The popups are deliberately different widths, so each one's content width
    # must come from its own outer width minus its own chrome — borrowing a
    # sibling's formula is wrong at every terminal size.
    from alysis_code.cli_impl.tui.app import (
        _FRAME_COLS,
        _SCROLLBAR_COLS,
        _approval_content_width_for,
        _approval_width_for,
        _help_content_width_for,
        _help_panel_width_for,
        _picker_content_width_for,
        _picker_width_for,
        _transcript_content_width_for,
    )

    for cols in range(40, 220):
        assert _picker_content_width_for(cols) == _picker_width_for(cols) - _FRAME_COLS
        assert _approval_content_width_for(cols) == _approval_width_for(cols) - _FRAME_COLS
        # The help panel is the only framed float that also scrolls.
        assert (
            _help_content_width_for(cols)
            == _help_panel_width_for(cols) - _FRAME_COLS - _SCROLLBAR_COLS
        )
        # The transcript is unframed but scrolls.
        assert _transcript_content_width_for(cols) == cols - _SCROLLBAR_COLS


def test_float_bodies_build_rows_at_their_own_content_width(monkeypatch):
    # The wiring, not just the arithmetic: each float's body function must ask its
    # row builder for ITS OWN content width. This is where the bug actually lived
    # — the geometry helpers can all be right while the body still calls the wrong
    # one. Drives the real body callables out of the real layout.
    from alysis_code.cli_impl.tui import app as tui_app

    cases = [
        ("class:tui.picker", "_render_picker_rows", 2, tui_app._picker_content_width_for),
        ("class:tui.help", "_help_rows_for_sections", 1, tui_app._help_content_width_for),
    ]
    for style, builder_name, width_arg, expected_width in cases:
        for cols in (60, 80, 90, 91, 95, 100, 160):
            seen: list[int] = []
            real = getattr(tui_app, builder_name)

            def spy(*a, _real=real, _seen=seen, _i=width_arg, **k):
                _seen.append(a[_i])
                return _real(*a, **k)

            monkeypatch.setattr(tui_app, builder_name, spy)
            app = _captured_app(cols)
            control = _float_body_control(app, style)
            with _set_app(app):
                control.text()  # the float's real body function
            monkeypatch.undo()

            assert seen, f"{style} body never built rows"
            assert seen[-1] == expected_width(cols), (
                f"{style} at {cols} cols built rows {seen[-1] - expected_width(cols):+d} "
                f"columns off its own window — prompt_toolkit would re-wrap them"
            )


def test_forge_planner_picker_renders_clean_at_95_columns():
    # The reported break, pinned. At 95 columns the picker window is 74 wide, but
    # the rows were built at the help panel's 79 — so "…together before" ran 5
    # columns past the edge and prompt_toolkit re-wrapped it as "…together b" /
    # "efore" at column 0, while the doubled screen rows pushed the hint line out
    # of the box entirely.
    from alysis_code.cli_impl.tui.app import (
        _picker_content_width_for,
        _render_picker_rows,
    )

    rows = [
        {
            "label": "Yes — use the planner assistant",
            "description": "Draft and refine the plan together before building.",
            "value": "yes",
            "current": True,
        },
        {
            "label": "No — plan it myself",
            "description": "Enter Forge and shape the plan with /goal and /task.",
            "value": "no",
        },
    ]
    hint = "↑↓ move · Enter select · Esc cancel"
    width = _picker_content_width_for(95)
    rendered = _render_picker_rows(rows, 0, width, hint)

    assert all(sum(len(t) for _s, t in row) == width for row in rendered)
    lines = ["".join(t for _s, t in row) for row in rendered]
    # The description wraps on a word boundary, under the description column —
    # never split mid-word ("together b" / "efore") against the left edge.
    assert not any(line.startswith("efore") for line in lines)
    assert any("before building. (current)" in line for line in lines)
    # The hint is still inside the box (it was clipped off by the overflow).
    assert any("Esc cancel" in line for line in lines)


def test_panel_builders_emit_exact_width_rows_at_every_width():
    # The panel builders floored the RIGHT column (`max(10, width - cmd_w - gap)`)
    # instead of shrinking the label column, so on a narrow panel they emitted
    # rows wider than the window — which re-wrap to column 0 — even when handed a
    # correct width. The /help hint was likewise appended raw at 42 columns.
    from alysis_code.cli_impl.tui.app import (
        _help_rows_for_sections,
        _render_kv_panel_rows,
    )

    sections = [
        ("Getting Started", [("/help", "commands & config"), ("/status", "session details")]),
        ("Execution", [("/a-very-long-command-name-that-overflows", "change execution mode")]),
    ]
    kv = [("Session", [("a-very-long-key-name-that-overflows", "value one", "accent")])]
    for width in range(20, 200):
        for rows in (_help_rows_for_sections(sections, width), _render_kv_panel_rows(kv, width)):
            assert all(sum(len(t) for _s, t in row) == width for row in rows), width


def test_help_panel_keeps_descriptions_readable_on_a_narrow_panel():
    # Fitting the row is not enough: one very long command must not consume the
    # whole panel and shred the descriptions a letter per line. The command column
    # is capped at half the panel and the command clipped into it.
    from alysis_code.cli_impl.tui.app import _help_rows_for_sections

    sections = [("Execution", [("/a-very-long-command-name-that-overflows", "change mode")])]
    width = 35
    lines = ["".join(t for _s, t in row) for row in _help_rows_for_sections(sections, width)]
    assert any("…" in line for line in lines)  # the command gave way, clipped
    assert any("change mode" in line for line in lines)  # the description stayed whole


def test_approval_modal_always_shows_its_key_legend():
    # The modal does not scroll, so a long command used to push [y]/[a]/[n] past
    # the window height: the user was asked to consent with no visible way to
    # answer. The body is budgeted so the legend always survives.
    from alysis_code.cli_impl.tui.app import (
        _FRAME_COLS,
        _approval_body_budget,
        _approval_content_width_for,
        _render_approval_rows,
    )

    request = SimpleNamespace(
        kind="shell_run",
        files=[],
        reason="Model requested a shell command.",
        command="git log --all "
        + "--grep=some-fairly-long-search-pattern " * 20
        + "&& rm -rf ./build",
    )
    for screen_rows in (16, 20, 24, 30, 40, 60):
        width = _approval_content_width_for(100)
        rows = _render_approval_rows(
            request, width, max_body_lines=_approval_body_budget(screen_rows)
        )
        assert all(sum(len(t) for _s, t in row) == width for row in rows)
        # Only the first window_height rows are reachable (no scrolling).
        window_height = max(6, min(len(rows) + _FRAME_COLS, screen_rows - 4)) - _FRAME_COLS
        visible = rows[:window_height]
        assert any(
            any(style == "class:tui.approve.key.yes" for style, _t in row) for row in visible
        ), f"[y]/[a]/[n] legend fell outside the modal at {screen_rows} screen rows"


def test_approval_modal_elides_the_middle_not_the_tail_of_a_command():
    # A consent prompt must not hide the end of the command: the dangerous part is
    # as likely to be a suffix ("… && rm -rf /") as a prefix. Both ends survive and
    # the drop is stated.
    from alysis_code.cli_impl.tui.app import _render_approval_rows

    request = SimpleNamespace(
        kind="shell_run",
        files=[],
        reason="",
        command="echo start " + "--padding-argument-to-force-wrapping " * 30 + "&& rm -rf /",
    )
    lines = [
        "".join(t for _s, t in row) for row in _render_approval_rows(request, 60, max_body_lines=8)
    ]
    joined = " ".join(lines)
    assert "echo start" in joined  # head kept
    assert "rm -rf /" in joined  # tail kept — the part that matters most
    assert any("more lines hidden" in line for line in lines)  # and the drop is admitted


def test_kv_panel_renderer_clips_long_keys_and_headers_to_width():
    # The cursor-pin scroll math requires every emitted row to be EXACTLY the
    # panel width; a key/header longer than its column must be clipped, never
    # allowed to overflow (which would wrap on screen and desync the scrollbar).
    from alysis_code.cli_impl.tui.app import _render_kv_panel_rows

    width = 40
    sections = [
        (
            "A section header that is far too long to fit in the panel width at all",
            [
                ("a_very_long_key_name_exceeding_the_cap", "v", "plain"),
                ("short", "x" * 200, "plain"),
            ],
        )
    ]
    rows = _render_kv_panel_rows(sections, width, hint="h" * 200)
    assert all(sum(len(t) for _s, t in row) == width for row in rows)


# ----------------------------------------------------- run_tui interception


class _FakeSession:
    def __init__(self, surface) -> None:
        self.surface = surface

    def run_turn(self, text, *, cancellation_token=None):
        self.surface.on_user_message(text)
        self.surface.on_assistant_message_done(f"Echo: {text}")
        return 0

    def close(self):
        pass


def _runner(calls):
    def runner(session, text, width):
        calls.append(text)
        low = text.strip().lower()
        if low in ("/exit", "exit"):
            return ("exit", "", None, None)
        return ("handled", f"ran:{text}", None, None)

    return runner


def _run_headless(state, keys, **kwargs):
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return run_tui(state, owl_color=False, input=pipe, output=DummyOutput(), **kwargs)


def test_subagents_empty_window_reports_plain_message() -> None:
    class _Scheduler:
        def status(self):
            return {"children": []}

        def set_lifecycle_listener(self, _listener):
            return None

    class _Session(_FakeSession):
        def __init__(self, surface) -> None:
            super().__init__(surface)
            self.child_scheduler = _Scheduler()

    _result, transcript = _run_headless(
        TuiState(model_name="m", username="t"),
        "/subagents\r/exit\r",
        session_builder=_Session,
        command_runner=_runner([]),
        background_turns=False,
    )

    assert ("system", "no active subagents") in transcript


def _captured_app(cols: int, rows: int = 40):
    """Build the real TUI app at a fixed terminal size WITHOUT running it.

    ``run_tui`` ends in ``Application.run()``; stubbing that out hands back the
    fully-wired app so a test can drive individual pieces of the real layout.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.data_structures import Size

    class _SizedOutput(DummyOutput):
        def get_size(self) -> Size:
            return Size(rows=rows, columns=cols)

    captured: dict = {}
    real_run = Application.run
    Application.run = lambda self, *a, **k: captured.__setitem__("app", self)
    try:
        with create_pipe_input() as pipe:
            run_tui(
                TuiState(model_name="m", username="t"),
                owl_color=False,
                input=pipe,
                output=_SizedOutput(),
                session_builder=_FakeSession,
                command_runner=_runner([]),
                background_turns=False,
            )
    finally:
        Application.run = real_run
    assert "app" in captured, "run_tui did not build an Application"
    return captured["app"]


def _set_app(app):
    """get_app() inside the width helpers must resolve to THIS app's output."""
    from prompt_toolkit.application.current import set_app

    return set_app(app)


def _float_body_control(app, style: str):
    """The control driving the float whose body Window carries ``style``."""
    from prompt_toolkit.layout.containers import Window

    for container in app.layout.walk():
        if isinstance(container, Window) and container.style == style:
            return container.content
    raise AssertionError(f"no window styled {style!r} in the layout")


def _render_container(container, *, cols: int, rows: int) -> None:
    """Really render ``container`` at a fixed terminal size.

    Populates each Window's ``render_info``, so a test can read the width
    prompt_toolkit actually paints into rather than trusting arithmetic.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.layout import Layout

    class _SizedOutput(DummyOutput):
        def get_size(self) -> Size:
            return Size(rows=rows, columns=cols)

    with create_pipe_input() as pipe:
        app: Application = Application(
            layout=Layout(container), input=pipe, output=_SizedOutput(), full_screen=True
        )
        with set_app(app):
            app.renderer.render(app, app.layout)


def test_panel_provider_called_with_arg_and_opens_on_bare():
    # Bare "/usage" → provider returns a spec → panel opens, NOT routed to runner.
    state = TuiState(model_name="m", username="t")
    calls: list = []
    seen_args: list = []

    def usage_provider(arg=""):
        seen_args.append(arg)
        if arg.strip():
            return None
        return {"title": "Usage", "sections": [("Total", [("tokens", "0", "accent")])]}

    _run_headless(
        state,
        "/usage\rq/exit\r",
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/usage": usage_provider},
        background_turns=False,
    )
    assert seen_args[0] == ""  # provider received empty arg for bare command
    assert all(c.strip().lower() != "/usage" for c in calls)  # not routed to runner


def test_panel_provider_returns_none_falls_through_to_runner():
    # "/usage hud on" → provider returns None → falls through to the command runner.
    state = TuiState(model_name="m", username="t")
    calls: list = []

    def usage_provider(arg=""):
        if arg.strip():
            return None
        return {"title": "Usage", "sections": [("Total", [("tokens", "0", "accent")])]}

    _run_headless(
        state,
        "/usage hud on\r/exit\r",
        session_builder=_FakeSession,
        command_runner=_runner(calls),
        panel_providers={"/usage": usage_provider},
        background_turns=False,
    )
    assert any(c.strip().lower() == "/usage hud on" for c in calls)


def test_trace_picker_digit_selects_and_applies():
    # Bare "/trace" opens the picker; pressing a digit applies via on_select.
    state = TuiState(model_name="m", username="t")
    picked = {"value": None}

    def on_select(value):
        picked["value"] = value
        return [("system", f"trace -> {value}")]

    def trace_picker():
        return {
            "title": "Reasoning Trace",
            "rows": [
                {"label": "off", "description": "d", "value": "off", "current": False},
                {"label": "compact", "description": "d", "value": "compact", "current": True},
                {"label": "full", "description": "d", "value": "full", "current": False},
            ],
            "on_select": on_select,
        }

    _result, transcript = _run_headless(
        state,
        "/trace\r3/exit\r",  # open picker, press "3" (full), then exit
        session_builder=_FakeSession,
        command_runner=_runner([]),
        picker_providers={"/trace": trace_picker},
        background_turns=False,
    )
    assert picked["value"] == "full"
    assert ("system", "trace -> full") in transcript


def test_stream_picker_defaults_on_and_can_select_off():
    state = TuiState(model_name="m", username="t")
    picked = {"value": None}

    def on_select(value):
        picked["value"] = value
        return [("system", f"stream -> {value}")]

    def stream_picker():
        return {
            "title": "Streaming",
            "rows": [
                {
                    "label": "on",
                    "description": "Render model output live.",
                    "value": "on",
                    "current": True,
                },
                {
                    "label": "off",
                    "description": "Buffer output until complete.",
                    "value": "off",
                    "current": False,
                },
            ],
            "on_select": on_select,
        }

    _result, transcript = _run_headless(
        state,
        "/stream\r2/exit\r",
        session_builder=_FakeSession,
        command_runner=_runner([]),
        picker_providers={"/stream": stream_picker},
        background_turns=False,
    )
    assert picked["value"] == "off"
    assert ("system", "stream -> off") in transcript
    assert ("user", "/stream") not in transcript


# ------------------------------------------------------------- /resume picker


def test_resume_picker_spec_builds_rows_from_sessions(tmp_path):
    from alysis_code.cli_impl.commands.chat_resume_helpers import (
        _chat_resume_picker_spec,
    )
    from alysis_code.session_store import SessionInfo

    newer = tmp_path / "sess-newer.jsonl"
    older = tmp_path / "sess-older.jsonl"
    newer.write_text("", encoding="utf-8")
    older.write_text("", encoding="utf-8")
    sessions = [
        SessionInfo(session_id="sess-newer", path=newer, mtime=2000.0),
        SessionInfo(session_id="sess-older", path=older, mtime=1000.0),
    ]
    spec = _chat_resume_picker_spec(sessions=sessions)
    assert spec is not None
    assert spec["title"] == "Resume Session"
    values = [row["value"] for row in spec["rows"]]
    assert values == ["sess-newer", "sess-older"]
    # Every row carries a label + a (preview) description for the picker renderer.
    assert all(row.get("label") and "description" in row for row in spec["rows"])


def test_resume_picker_spec_empty_returns_none():
    from alysis_code.cli_impl.commands.chat_resume_helpers import (
        _chat_resume_picker_spec,
    )

    assert _chat_resume_picker_spec(sessions=[]) is None


def test_resume_picker_reloads_transcript_and_confirms():
    # Bare "/resume" opens the picker; Enter selects the row and the on_select
    # (mirroring loop.py) reloads the prior conversation via the surface and
    # returns a confirmation that is appended below it.
    state = TuiState(model_name="m", username="t")
    box: dict = {}

    def builder(surface):
        box["surface"] = surface
        return _FakeSession(surface)

    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "tool", "tool_call_id": "1", "content": "noise"},
    ]

    def on_select(value):
        box["surface"].replace_history(history)
        return [("system", f"Resumed session: {value} (1 turn loaded).")]

    def resume_picker():
        return {
            "title": "Resume Session",
            "rows": [
                {
                    "label": "5 min ago",
                    "description": "first question",
                    "value": "sess-1",
                    "current": False,
                }
            ],
            "on_select": on_select,
        }

    _result, transcript = _run_headless(
        state,
        "/resume\r\r/exit\r",  # open picker, Enter selects row 0, then exit
        session_builder=builder,
        command_runner=_runner([]),
        picker_providers={"/resume": resume_picker},
        background_turns=False,
    )
    assert ("user", "first question") in transcript
    assert ("assistant", "first answer") in transcript
    # tool turns are dropped from the reloaded view
    assert all(role != "tool" for role, _t in transcript)
    assert any(role == "system" and "Resumed session: sess-1" in text for role, text in transcript)


def test_resume_picker_contentless_session_shows_assistant_note():
    # A resumed session whose history has nothing displayable clears the pane to
    # empty; the outcome must use the assistant role so it flips the welcome→chat
    # pane and the confirmation is actually seen (mirrors loop._tui_resume_apply).
    state = TuiState(model_name="m", username="t")
    box: dict = {}

    def builder(surface):
        box["surface"] = surface
        return _FakeSession(surface)

    def on_select(value):
        box["surface"].replace_history([])  # content-less → nothing visible
        box["surface"].append_note(f"Resumed session: {value} (0 turns loaded).", role="assistant")
        return None

    def resume_picker():
        return {
            "title": "Resume Session",
            "rows": [{"label": "now", "description": "d", "value": "sess-x", "current": False}],
            "on_select": on_select,
        }

    _result, transcript = _run_headless(
        state,
        "/resume\r\r/exit\r",
        session_builder=builder,
        command_runner=_runner([]),
        picker_providers={"/resume": resume_picker},
        background_turns=False,
    )
    # The reload cleared the pane, so the assistant-role note is the first entry.
    assert transcript
    assert transcript[0] == ("assistant", "Resumed session: sess-x (0 turns loaded).")


def test_picker_long_list_navigates_to_far_row():
    # A long list (more rows than fit) must stay navigable: arrowing down past the
    # visible window still moves + selects the far row (cursor-follow scrolling).
    state = TuiState(model_name="m", username="t")
    picked = {"value": None}
    rows = [
        {"label": f"t{i}", "description": "d", "value": f"s{i}", "current": False}
        for i in range(30)
    ]

    def on_select(value):
        picked["value"] = value
        return [("system", f"resumed {value}")]

    def picker():
        return {"title": "Resume Session", "rows": rows, "on_select": on_select}

    _run_headless(
        state,
        "/resume\r" + ("j" * 25) + "\r/exit\r",  # open, down 25, select, exit
        session_builder=_FakeSession,
        command_runner=_runner([]),
        picker_providers={"/resume": picker},
        background_turns=False,
    )
    assert picked["value"] == "s25"


def test_usage_panel_marks_derived_uncached_input_and_hides_empty_cache_bits() -> None:
    class _Sum:
        def by_model_rows(self):
            return [
                {
                    "model": "gpt-derived",
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    "known_cost_calls": 0,
                    "unknown_cost_count": 0,
                    "api_usage_calls": 1,
                    "estimate_usage_calls": 0,
                    "input_tokens_uncached": 60,
                    "input_tokens_uncached_reported_calls": 0,
                    "input_tokens_uncached_derived_calls": 1,
                    "cache_read_input_tokens": 0,
                    "cache_read_reported_calls": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_creation_reported_calls": 0,
                }
            ]

        def totals(self):
            return {
                **self.by_model_rows()[0],
                "unknown_cost_calls": 0,
                "corrected_usage_calls": 0,
            }

    spec = panels._chat_usage_panel_spec(session=SimpleNamespace(usage_summary=_Sum()))
    blob = _values(spec)

    assert "input uncached 60 (derived)" in blob
    # A provider that reports no cache figures at all should not repeat
    # "not reported" on every per-model row.
    assert "cache r:not reported" not in blob
