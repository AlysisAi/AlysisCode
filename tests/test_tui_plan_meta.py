"""Tests for the structured Forge plan-update aside (``tui/plan_meta.py``).

Covers the whole pipeline the TUI relies on: the Rich console capture contract
(bar-prefixed meta/warning lines), logical-note reconstruction (including
legacy narrow captures that wrapped notes), status-headline folding, per-task
grouping, the renderer's row-width invariant, and the source contracts tying
the parser's literals to the repo's emit sites.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from rich.console import Console

from alysis_code.cli_impl.commands.cli_common import (
    _print_forge_meta,
    _print_forge_warning_messages,
)
from alysis_code.cli_impl.tui.plan_meta import (
    parse_plan_meta,
    plan_meta_rows,
)

_TITLE = "Add basic local verification instructions for the static site"
_DEPS = "Scaffold one-page Driftwood Coffee static site with warm minimal styling"


def _capture_console(width: int, *, soft_wrap: bool = False) -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=False,
        no_color=True,
        highlight=False,
        soft_wrap=soft_wrap,
        width=width,
    )
    return console, buf


def _emit_screenshot_scenario(console: Console) -> None:
    """The real planner-turn emissions behind the 'dense wall' screenshot."""
    _print_forge_meta(console=console, message="Applied planner update to plan.")
    _print_forge_warning_messages(
        console=console,
        label="Planner",
        warnings=[
            f"Dropped unknown dependencies for new task '{_TITLE}': {_DEPS}",
            f"New task '{_TITLE}': inferred estimated_files from task text: index.html",
            f"New task '{_TITLE}': expanded write_scope to include estimated_files: index.html",
        ],
    )


def _flat(rows: list[list[tuple[str, str]]]) -> str:
    return "\n".join("".join(t for _s, t in row) for row in rows)


def _row_text(row: list[tuple[str, str]]) -> str:
    return "".join(t for _s, t in row)


def test_bar_capture_roundtrip_parses_logical_notes():
    console, buf = _capture_console(width=4096)
    _emit_screenshot_scenario(console)
    view = parse_plan_meta(buf.getvalue())
    # The status line becomes the headline; the 3 warnings stay as notes.
    assert view.headline == "plan updated"
    assert len(view.notes) == 3
    assert all(note.kind == "warn" and note.label == "Planner" for note in view.notes)
    assert all(note.subject == _TITLE for note in view.notes)
    assert view.notes[0].detail == f"dropped unknown dependencies: {_DEPS}"
    assert view.notes[1].detail == "inferred estimated_files from task text: index.html"
    # All three notes group under ONE task entry.
    assert [kind for kind, _e in view.entries] == ["group"]
    group = view.entries[0][1]
    assert group.subject == _TITLE and group.subject_kind == "new"
    assert len(group.details) == 3


def test_narrow_capture_rejoins_wrapped_notes():
    # A legacy narrow capture wraps long notes into bar-less continuation
    # lines; the parser must reassemble the same logical notes as a wide one.
    wide_console, wide_buf = _capture_console(width=4096)
    narrow_console, narrow_buf = _capture_console(width=60)
    _emit_screenshot_scenario(wide_console)
    _emit_screenshot_scenario(narrow_console)
    narrow_lines = narrow_buf.getvalue().strip().split("\n")
    wide_lines = wide_buf.getvalue().strip().split("\n")
    # Witness that wrapping really happened: extra physical lines exist and
    # they are bar-less continuations (the branch under test).
    assert len(narrow_lines) > len(wide_lines)
    assert any(not line.lstrip().startswith("│") for line in narrow_lines)
    assert parse_plan_meta(narrow_buf.getvalue()) == parse_plan_meta(wide_buf.getvalue())


def test_giant_unbroken_token_survives_capture_verbatim():
    # Mirrors the loop.py capture console (soft_wrap=True, width=4096): a
    # provider error containing one unbroken >4096-char token must not be
    # character-folded and rejoined with an injected space.
    token = "error-blob-" + "y" * 5000
    console, buf = _capture_console(width=4096, soft_wrap=True)
    _print_forge_warning_messages(
        console=console, label="Planner", warnings=[f"Final planner error: {token}"]
    )
    view = parse_plan_meta(buf.getvalue())
    assert view.headline == "planner error"
    assert len(view.notes) == 1
    assert token in view.notes[0].detail


def test_status_headline_folding():
    assert parse_plan_meta("│ Applied planner update to plan.").headline == "plan updated"
    assert parse_plan_meta("│ Applied planner update to plan.").notes == []
    noop = parse_plan_meta("│ Planner update contained no applicable changes.")
    assert noop.headline == "plan unchanged" and noop.notes == []
    error = parse_plan_meta(
        "│ Planner: Planner request failed after 2 transient retries.\n"
        "│ Planner: Final planner error: upstream 500"
    )
    assert error.headline == "planner error" and len(error.notes) == 2
    plain = parse_plan_meta("│ Captured requirement note.")
    assert plain.headline == "planner notes" and len(plain.notes) == 1


def test_ignored_status_keeps_the_reason_as_a_note():
    ignored = parse_plan_meta(
        "│ Planner update ignored because this message did not look "
        "planning-related; plan unchanged."
    )
    assert ignored.headline == "plan unchanged"
    # The actionable reason survives as an info note (minus the redundant
    # "; plan unchanged." tail the headline already states).
    assert len(ignored.notes) == 1
    assert ignored.notes[0].kind == "info"
    assert "planning-related" in ignored.notes[0].detail
    assert not ignored.notes[0].detail.endswith("plan unchanged.")


def test_rule_lines_are_filtered_out():
    assert plan_meta_rows("────────", 70, expanded=False) == []
    view = parse_plan_meta("────────\n│ Captured requirement note.\n│ ────────")
    assert [note.detail for note in view.notes] == ["Captured requirement note."]


def test_subject_extraction_variants():
    view = parse_plan_meta(
        "\n".join(
            [
                "│ Planner: Dropped unknown dependencies for updated task 'T03': T99",
                "│ Planner: Dropped unknown dependencies for synthesized follow-up task 'T09': T98",
                "│ Planner: Updated task 'Polish hero': narrowed write_scope to index.html",
                "│ Plan reconciliation: Task T02: inferred estimated_files from task text: a.md",
                "│ Planner: request recovered after 1 transient retry.",
            ]
        )
    )
    by_subject = {note.subject: note for note in view.notes}
    assert by_subject["T03"].subject_kind == "updated"
    assert by_subject["T09"].subject_kind == "follow-up"
    assert by_subject["Polish hero"].subject_kind == "updated"
    assert by_subject["T02"].subject_kind == "task"
    assert by_subject["T02"].label == "Plan reconciliation"
    # The free-form warning stays flat (no subject) and keeps its label.
    flat_notes = [note for note in view.notes if not note.subject]
    assert len(flat_notes) == 1 and flat_notes[0].label == "Planner"
    assert [kind for kind, _e in view.entries].count("group") == 4


def test_subject_extraction_survives_awkward_titles():
    # Apostrophes in a title must not break grouping (an apostrophe alone is
    # not a "': " boundary, so the lazy capture spans it).
    apostrophe = parse_plan_meta(
        "│ Planner: New task 'Improve user's onboarding': "
        "inferred estimated_files from task text: docs/x.md"
    )
    assert apostrophe.notes[0].subject == "Improve user's onboarding"
    assert apostrophe.notes[0].subject_kind == "new"
    # A pathological title containing a literal "': " truncates at its FIRST
    # boundary — graceful (still grouped, tail kept in the detail). The
    # alternative (greedy last-boundary) would let echoed junk in the detail
    # swallow the subject, which is the worse failure.
    quote_colon = parse_plan_meta(
        "│ Planner: New task 'Fix foo': bar handler': "
        "inferred estimated_files from task text: src/a.py"
    )
    assert quote_colon.notes[0].subject == "Fix foo"
    assert quote_colon.notes[0].detail == (
        "bar handler': inferred estimated_files from task text: src/a.py"
    )


def test_subject_boundary_prefers_title_over_echoed_junk():
    # The emit grammar closes the title's quote at the FIRST "': " boundary;
    # everything after is verbatim model output that can contain quotes and
    # colons. These pin the lazy captures for ALL subject regexes — each case
    # mis-parses under a greedy variant.
    dropped = parse_plan_meta(
        "│ Planner: Dropped unknown dependencies for new task 'Wire auth': add tests for 'auth'"
    )
    assert dropped.notes[0].subject == "Wire auth"
    assert dropped.notes[0].detail == "dropped unknown dependencies: add tests for 'auth'"
    new_junk = parse_plan_meta(
        "│ Planner: New task 'T': dropped invalid estimated_files entries: 'src/foo.py': parser"
    )
    assert new_junk.notes[0].subject == "T"
    assert new_junk.notes[0].detail == (
        "dropped invalid estimated_files entries: 'src/foo.py': parser"
    )
    updated_junk = parse_plan_meta("│ Planner: Updated task 'T2': moved scope: 'a.py': junk")
    assert updated_junk.notes[0].subject == "T2"
    assert updated_junk.notes[0].detail == "moved scope: 'a.py': junk"
    quoted_task_junk = parse_plan_meta(
        "│ Plan reconciliation: Task 'T3': added hints: 'b.py': junk"
    )
    assert quoted_task_junk.notes[0].subject == "T3"
    assert quoted_task_junk.notes[0].detail == "added hints: 'b.py': junk"


def test_dropped_target_kind_forward_compat():
    # tasks_add maps to "new"; an unrecognized future target phrase degrades
    # to a generic grouped "task" entry rather than falling flat.
    tasks_add = parse_plan_meta(
        "│ Planner: Dropped unknown dependencies for planner tasks_add 'X': y"
    )
    assert tasks_add.notes[0].subject_kind == "new"
    future = parse_plan_meta(
        "│ Planner: Dropped unknown dependencies for some future target 'X': y"
    )
    assert future.notes[0].subject == "X" and future.notes[0].subject_kind == "task"
    assert [kind for kind, _e in future.entries] == ["group"]


def test_protected_dependency_variants_group_with_their_task():
    view = parse_plan_meta(
        "\n".join(
            [
                "│ Planner: Dropped unknown dependencies for synthesized follow-up "
                "task 'core-1-followup-1': ghost",
                "│ Planner: Dropped protected non-done dependencies for synthesized "
                "follow-up task 'core-1-followup-1': core-0",
                "│ Planner: Dropped protected non-done dependencies for planner "
                "tasks_update 'T07': T01",
            ]
        )
    )
    kinds = [kind for kind, _e in view.entries]
    assert kinds == ["group", "group"]
    follow_up = view.entries[0][1]
    assert follow_up.subject == "core-1-followup-1" and follow_up.subject_kind == "follow-up"
    assert [n.detail for n in follow_up.details] == [
        "dropped unknown dependencies: ghost",
        "dropped protected non-done dependencies: core-0",
    ]
    update = view.entries[1][1]
    assert update.subject == "T07" and update.subject_kind == "updated"


def test_generic_task_group_upgrades_to_updated():
    text = (
        "│ Plan reconciliation: Task T02: inferred estimated_files from task text: a.md\n"
        "│ Planner: Updated task 'T02': narrowed write_scope to a.md"
    )
    view = parse_plan_meta(text)
    groups = [entry for kind, entry in view.entries if kind == "group"]
    assert len(groups) == 1
    assert groups[0].subject == "T02" and groups[0].subject_kind == "updated"
    assert "~ updated task · T02" in _flat(plan_meta_rows(text, 100, expanded=True))


def test_expanded_rows_dedupe_task_titles_and_style_warnings():
    console, buf = _capture_console(width=4096)
    _emit_screenshot_scenario(console)
    rows = plan_meta_rows(buf.getvalue(), 100, expanded=True)
    body = _flat(rows)
    # The long task title appears exactly ONCE (the old flat dump repeated it
    # in every warning — the main source of the wall-of-text).
    assert body.count(_TITLE) == 1
    assert body.startswith("▾")
    assert "plan updated" in body and "3 notes" in body
    assert "new task" in body
    # Warnings carry the amber ⚠ mark and the warn style class.
    warn_rows = [row for row in rows if any("⚠" in t for _s, t in row)]
    assert len(warn_rows) == 3
    assert all(any(s == "class:tui.planmeta.warn" for s, _t in row) for row in warn_rows)
    # The task title row uses the subject style.
    assert any(
        any(s == "class:tui.planmeta.subject" and _TITLE.startswith(t.strip()) for s, t in row)
        for row in rows
    )


def test_rows_never_exceed_width():
    console, buf = _capture_console(width=4096)
    _emit_screenshot_scenario(console)
    _print_forge_warning_messages(
        console=console,
        label="Planner",
        warnings=["unbreakable-token-" + "z" * 150],
    )
    text = buf.getvalue()
    for width in (12, 20, 24, 40, 70, 132):
        collapsed = plan_meta_rows(text, width, expanded=False)
        expanded = plan_meta_rows(text, width, expanded=True)
        assert len(collapsed) == 1
        effective = max(20, width)  # the renderer clamps ultra-narrow panels
        for row in collapsed + expanded:
            assert sum(len(t) for _s, t in row) <= effective
    # The over-long token is hard-broken across rows with EVERY character
    # preserved ('z' appears nowhere else in the scenario).
    flat = _flat(plan_meta_rows(text, 40, expanded=True))
    assert "unbreakable-token-" in flat.replace("\n", "")
    assert flat.count("z") == 150


def test_collapsed_row_summarizes_and_offers_expand():
    console, buf = _capture_console(width=4096)
    _emit_screenshot_scenario(console)
    row = plan_meta_rows(buf.getvalue(), 120, expanded=False)[0]
    text = _row_text(row)
    assert text.startswith("▸")
    assert "plan updated" in text
    assert "3 notes" in text and "1 new task" in text and "3 warnings" in text
    assert "click to expand" in text
    # A no-op planner turn is honest: "plan unchanged", no fake note count.
    noop = plan_meta_rows("│ Planner update contained no applicable changes.", 80, expanded=False)
    noop_text = _row_text(noop[0])
    assert "plan unchanged" in noop_text
    assert "note" not in noop_text
    # A single free-form note still reads verbatim when collapsed.
    single = plan_meta_rows(
        "│ Planner assistant is unavailable because config is missing.", 80, expanded=False
    )
    assert "unavailable" in _row_text(single[0])


def test_collapsed_row_shows_updated_task_chip():
    rows = plan_meta_rows(
        "│ Applied planner update to plan.\n"
        "│ Planner: Updated task 'Polish hero': narrowed write_scope to index.html",
        100,
        expanded=False,
    )
    assert " · 1 updated" in _row_text(rows[0])


def test_collapsed_row_sheds_chips_cleanly_on_narrow_panels():
    console, buf = _capture_console(width=4096)
    _emit_screenshot_scenario(console)
    # Width 60: the candidate segments total 70 cols, so exactly the lowest-
    # priority chip (' · 1 new task') sheds — no mid-chip ellipsis clipping.
    text = _row_text(plan_meta_rows(buf.getvalue(), 60, expanded=False)[0])
    assert "plan updated" in text and "click to expand" in text
    assert "new task" not in text
    assert "…" not in text
    # Width 34: hint and note-count shed too; warnings (highest-priority chip)
    # survive alongside the headline.
    narrow = _row_text(plan_meta_rows(buf.getvalue(), 34, expanded=False)[0])
    assert narrow == "▸ plan updated · 3 warnings"


def test_collapsed_single_note_clips_with_expand_hint():
    note = (
        "│ Planner assistant is unavailable because the configured provider "
        "profile has no reachable model endpoint right now."
    )
    clipped = plan_meta_rows(note, 60, expanded=False)
    assert len(clipped) == 1
    text = _row_text(clipped[0])
    assert len(text) <= 60
    assert text.endswith("click to expand")
    # Below the hint threshold the row still fits, just without the hint.
    tiny = _row_text(plan_meta_rows(note, 25, expanded=False)[0])
    assert len(tiny) <= 25
    assert "click to expand" not in tiny


def test_plan_meta_rows_are_copyable_and_strip_chrome():
    from alysis_code.cli_impl.tui.app import (
        _COPYABLE_TRANSCRIPT_ROLES,
        _strip_transcript_copy_chrome,
    )

    # Plan-aside rows (lead AND wrap continuations) participate in mouse
    # selection / Ctrl+C like the rest of the transcript.
    assert "planmeta" in _COPYABLE_TRANSCRIPT_ROLES
    assert "planmetacont" in _COPYABLE_TRANSCRIPT_ROLES
    # Copies drop the glyph decoration and the expand hint but KEEP the
    # indentation (it carries the note hierarchy).
    strip = lambda t: _strip_transcript_copy_chrome(t, role="planmeta")  # noqa: E731
    assert strip("▾ plan updated · 3 notes") == "plan updated · 3 notes"
    assert (
        strip("▸ plan updated · 3 notes · 3 warnings  ·  click to expand")
        == "plan updated · 3 notes · 3 warnings"
    )
    assert strip(f"  + new task · {_TITLE}") == f"  new task · {_TITLE}"
    assert strip("  ~ updated task · T02") == "  updated task · T02"
    assert strip("    ⚠ inferred estimated_files from task text: index.html") == (
        "    inferred estimated_files from task text: index.html"
    )
    assert strip("  · Applied planner update to plan.") == "  Applied planner update to plan."
    assert strip("dropped unknown dependencies") == "dropped unknown dependencies"
    # A sweep released MID-hint still drops the trailing hint fragment.
    assert strip("▸ plan updated · 2 notes  ·  cl") == "plan updated · 2 notes"
    # A hint-only sweep copies nothing; a bare glyph-column sweep is a
    # deliberate glyph copy and passes through.
    assert strip("  ·  click to expand") == ""
    assert strip("▸") == "▸"
    # A sweep that starts mid-row begins with note CONTENT — glyph-like
    # characters there are kept (only the hint is chrome).
    assert (
        _strip_transcript_copy_chrome("~ 40k rows/s sustained", role="planmeta", starts_row=False)
        == "~ 40k rows/s sustained"
    )
    # Wrap-continuation rows pass through completely untouched.
    cont = lambda t: _strip_transcript_copy_chrome(t, role="planmetacont")  # noqa: E731
    assert cont("      static site") == "      static site"
    assert cont("      + bb and cc ~") == "      + bb and cc ~"


def test_wrapped_continuation_rows_copy_content_verbatim():
    from prompt_toolkit.data_structures import Point

    from alysis_code.cli_impl.tui.app import _selected_text
    from alysis_code.cli_impl.tui.plan_meta import plan_meta_rows_with_kinds

    # A note containing bare '+', '~', '·' tokens wrapped on a narrow panel:
    # continuation rows may START with one of those characters, and the copy
    # must keep them (they are content, not chrome).
    text = "│ Planner: Task 'Refactor': rename ops aa + bb and cc ~ dd and ee · ff across modules"
    pairs = plan_meta_rows_with_kinds(text, 20, expanded=True)
    kinds = [kind for _row, kind in pairs]
    assert "planmetacont" in kinds  # the narrow panel really wrapped the note
    rows = ["".join(t for _s, t in row) for row, _kind in pairs]
    copied = _selected_text(
        rows,
        Point(x=0, y=0),
        Point(x=max(0, len(rows[-1]) - 1), y=len(rows) - 1),
        row_roles=kinds,
    )
    assert "aa + bb and cc ~ dd and ee · ff across modules" in " ".join(copied.split())


def test_partial_sweep_never_leaks_hint_fragments():
    from prompt_toolkit.data_structures import Point

    from alysis_code.cli_impl.tui.app import _selected_text

    row = "▸ plan updated · 2 notes · 1 warning  ·  click to expand"
    # Release the sweep two characters into the hint text.
    partial = _selected_text(
        [row],
        Point(x=0, y=0),
        Point(x=row.index("click") + 1, y=0),
        row_roles=["planmeta"],
    )
    assert partial == "plan updated · 2 notes · 1 warning"
    assert "cl" not in partial.split()[-1]


def test_plan_meta_mouse_click_vs_drag_contract():
    from prompt_toolkit.data_structures import Point

    from alysis_code.cli_impl.tui.app import (
        _plan_meta_click_toggles,
        _plan_meta_press_hit,
    )

    roles = ["user", "spacer", "planmeta", "planmetacont"]
    assert _plan_meta_press_hit(roles, 2)
    assert _plan_meta_press_hit(roles, 3)  # wrap continuations count as the aside
    assert not _plan_meta_press_hit(roles, 0)
    assert not _plan_meta_press_hit(roles, 99)  # out of bounds fails closed
    assert not _plan_meta_press_hit([], 0)
    press = Point(x=3, y=2)
    # A plain click on the aside toggles it.
    assert _plan_meta_click_toggles(pressed_planmeta=True, anchor=press, release=press, selected="")
    # A drag that swept text is a selection — never a toggle.
    assert not _plan_meta_click_toggles(
        pressed_planmeta=True, anchor=press, release=Point(x=9, y=2), selected="plan"
    )
    # A drag whose sweep strips to empty (blank margin / chrome rows) is
    # still a drag — the pointer moved, so no toggle.
    assert not _plan_meta_click_toggles(
        pressed_planmeta=True, anchor=press, release=Point(x=3, y=5), selected=""
    )
    # A click that did not start on the aside never toggles.
    assert not _plan_meta_click_toggles(
        pressed_planmeta=False, anchor=press, release=press, selected=""
    )


def test_wrap_segments_clips_oversized_lead():
    from alysis_code.cli_impl.tui.plan_meta import _wrap_segments

    rows = _wrap_segments([("s", "X" * 30)], [("d", "alpha beta")], 10, cont_indent=4)
    assert all(sum(len(t) for _s, t in row) <= 10 for row in rows)
    assert "alpha" in "".join(t for row in rows for _s, t in row)


def test_collapsed_single_note_boundaries():
    note = (
        "│ Planner assistant is unavailable because the configured provider "
        "profile has no reachable model endpoint right now."
    )
    detail = parse_plan_meta(note).notes[0].detail
    exact = 2 + len(detail)
    # Exact fit renders verbatim with no hint...
    verbatim = _row_text(plan_meta_rows(note, exact, expanded=False)[0])
    assert verbatim == f"▸ {detail}"
    # ...one column narrower clips and appends the expand hint.
    clipped = _row_text(plan_meta_rows(note, exact - 1, expanded=False)[0])
    assert clipped.endswith("click to expand") and len(clipped) <= exact - 1
    # Hint threshold: present at width 32, dropped at 31.
    at32 = _row_text(plan_meta_rows(note, 32, expanded=False)[0])
    assert at32.endswith("click to expand") and len(at32) <= 32
    at31 = _row_text(plan_meta_rows(note, 31, expanded=False)[0])
    assert "click to expand" not in at31 and len(at31) <= 31


def test_selected_text_copies_plan_meta_block_cleanly():
    from prompt_toolkit.data_structures import Point

    from alysis_code.cli_impl.tui.app import _selected_text
    from alysis_code.cli_impl.tui.plan_meta import plan_meta_rows_with_kinds

    console, buf = _capture_console(width=4096)
    _emit_screenshot_scenario(console)
    pairs = plan_meta_rows_with_kinds(buf.getvalue(), 100, expanded=True)
    rows = ["".join(t for _s, t in row) for row, _kind in pairs]
    copied = _selected_text(
        rows,
        Point(x=0, y=0),
        Point(x=max(0, len(rows[-1]) - 1), y=len(rows) - 1),
        row_roles=[kind for _row, kind in pairs],
    )
    lines = copied.split("\n")
    assert lines[0] == "plan updated · 3 notes"
    assert lines[1] == f"  new task · {_TITLE}"
    assert lines[2].startswith("    dropped unknown dependencies:")
    # No UI glyphs leak into the clipboard text.
    assert not any(g in copied for g in ("▾", "▸", "⚠", "+ new", "click to expand"))
    assert copied.count(_TITLE) == 1


def test_forge_emit_sites_match_parser_contract():
    """The parser keys on literals owned by this repo — keep them in sync."""
    import alysis_code.forge_planner_engine as forge_planner_engine
    import alysis_code.plan_assistant as plan_assistant
    import alysis_code.plan_reconciliation as plan_reconciliation
    import alysis_code.task_readiness as task_readiness

    source = Path(forge_planner_engine.__file__).read_text(encoding="utf-8")
    # Status lines the headline fold matches by prefix.
    assert "Applied planner update to plan." in source
    assert "Planner update contained no applicable changes." in source
    assert "Planner update ignored because" in source
    assert "Final planner error:" in source
    # Warning-group labels used on the planner turn path.
    labels = set(re.findall(r"ForgePlannerWarning\(\s*\"([^\"]+)\"", source))
    assert labels <= {"Planner", "Plan reconciliation"}
    assert "Planner" in labels
    # Subject-prefix literals the grouping regexes key on, at their emit sites.
    assistant_source = Path(plan_assistant.__file__).read_text(encoding="utf-8")
    assert "Dropped unknown dependencies for new task '" in assistant_source
    assert "Dropped unknown dependencies for updated task '" in assistant_source
    assert "Dropped unknown dependencies for synthesized follow-up task '" in assistant_source
    assert "Dropped protected non-done dependencies for " in assistant_source
    assert "planner tasks_update '" in assistant_source
    assert "warning_prefix=f\"New task '" in assistant_source
    assert "warning_prefix=f\"Updated task '" in assistant_source
    readiness_source = Path(task_readiness.__file__).read_text(encoding="utf-8")
    assert ": inferred estimated_files from task text: " in readiness_source
    assert ": expanded write_scope to include estimated_files: " in readiness_source
    reconciliation_source = Path(plan_reconciliation.__file__).read_text(encoding="utf-8")
    assert "Task {task_id}: inferred estimated_files from task text: " in reconciliation_source
    # The console bar prefix the note reconstruction keys on.
    console, buf = _capture_console(width=200)
    _print_forge_meta(console=console, message="probe")
    assert buf.getvalue().startswith("│ probe")
    # The planner capture console in loop.py must stay unwrapped: soft_wrap
    # keeps giant unbroken tokens (provider error blobs) on one physical line
    # so the rejoin never injects a space mid-token. Pin the kwargs at the
    # construction site feeding the planmeta aside, not just in this test's
    # mirror console.
    import alysis_code.cli_impl.chat.loop as chat_loop

    loop_source = Path(chat_loop.__file__).read_text(encoding="utf-8")
    planner_block = loop_source.split("def _tui_make_forge_planner_execute", 1)[1].split(
        "def _tui_make_subagent_execute", 1
    )[0]
    assert 'role="planmeta"' in planner_block
    assert "soft_wrap=True" in planner_block
    assert "width=4096" in planner_block
