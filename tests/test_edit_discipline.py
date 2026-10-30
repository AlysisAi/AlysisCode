"""Tests for edit discipline: rewrite guard, thrash guard, scratch report.

Runnable two ways:

    python3 tests/test_edit_discipline.py     # standalone, stdlib only
    pytest tests/test_edit_discipline.py

The module under test is loaded directly from its file path so that importing
it never executes ``alysis_code/__init__`` or any of the package's
dependency-heavy import chain. That keeps these tests runnable in a bare
interpreter with no third-party packages installed.

The fixtures are shaped after the two production failures this PR exists for:
the Terminal-Bench ``filter-js-from-html`` rewrite (semantically right, byte
wrong) and the ``gcode-to-text`` thrash (45 actions, 52 scratch files, no
answer).
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "src" / "alysis_code" / "edit_discipline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_edit_discipline", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because the module defines dataclasses, and
    # ``dataclasses`` resolves annotations via ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ed = _load_module()


# The filter-js-from-html shape: the agent's output is the same document, but
# re-serialized. Indentation stripped, attributes reordered, `<br>` written as
# `<br/>`, `&copy;` decoded. Every one of those is invisible to a semantic test
# and fatal to `test_clean_html_unchanged`.
_HTML_ORIGINAL = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Report</title>
  </head>
  <body>
    <div class="wrap" id="main">
      <p>Line one<br>Line two</p>
      <a href="/docs" class="link" target="_blank">Docs</a>
    </div>
    <footer>&copy; 2026 Example Inc.</footer>
  </body>
</html>
"""

_HTML_RESERIALIZED = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Report</title>
</head>
<body>
<div id="main" class="wrap">
<p>Line one<br/>Line two</p>
<a class="link" href="/docs" target="_blank">Docs</a>
</div>
<footer>© 2026 Example Inc.</footer>
</body>
</html>
"""

# The same document with one script tag actually removed -- the edit the task
# asked for, done without collateral reformatting.
_HTML_TARGETED_BEFORE = _HTML_ORIGINAL.replace(
    "    <footer>", '    <script src="track.js"></script>\n    <footer>'
)


def _numbered_lines(count: int, prefix: str = "line") -> list[str]:
    return [f"{prefix} {index}" for index in range(count)]


class ChangedLineFractionTests(unittest.TestCase):
    def test_identical_content_changes_nothing(self) -> None:
        text = "\n".join(_numbered_lines(20))
        self.assertEqual(ed.changed_line_fraction(text, text), 0.0)

    def test_empty_inputs_are_zero(self) -> None:
        self.assertEqual(ed.changed_line_fraction("", ""), 0.0)

    def test_emptying_a_file_changes_everything(self) -> None:
        self.assertEqual(ed.changed_line_fraction("a\nb\nc", ""), 1.0)

    def test_denominator_is_the_longer_side(self) -> None:
        # 10 lines kept, 10 appended: half the resulting file is new.
        old = "\n".join(_numbered_lines(10))
        new = "\n".join(_numbered_lines(10) + [f"extra {i}" for i in range(10)])
        self.assertAlmostEqual(ed.changed_line_fraction(old, new), 0.5)

    def test_single_line_edit_in_a_large_file_is_small(self) -> None:
        lines = _numbered_lines(200)
        old = "\n".join(lines)
        lines[7] = "line 7 = patched"
        new = "\n".join(lines)
        self.assertAlmostEqual(ed.changed_line_fraction(old, new), 1 / 200)


class ThresholdEdgeTests(unittest.TestCase):
    """The 80% boundary, from both sides, on a 100-line file."""

    def setUp(self) -> None:
        self.old = "\n".join(_numbered_lines(100))

    def _rewrite_first(self, count: int) -> str:
        lines = [f"CHANGED {index}" for index in range(count)]
        lines.extend(_numbered_lines(100)[count:])
        return "\n".join(lines)

    def test_exactly_eighty_percent_does_not_warn(self) -> None:
        new = self._rewrite_first(80)
        self.assertEqual(ed.changed_line_fraction(self.old, new), 0.80)
        assessment = ed.assess_full_file_rewrite(path="a.txt", original=self.old, updated=new)
        self.assertEqual(assessment.changed_percent, 80)
        self.assertFalse(assessment.should_warn)
        self.assertIsNone(assessment.warning_text())

    def test_eighty_one_percent_warns(self) -> None:
        new = self._rewrite_first(81)
        self.assertEqual(ed.changed_line_fraction(self.old, new), 0.81)
        assessment = ed.assess_full_file_rewrite(path="a.txt", original=self.old, updated=new)
        self.assertEqual(assessment.changed_percent, 81)
        self.assertTrue(assessment.should_warn)
        self.assertFalse(assessment.serialization_only)

    def test_seventy_nine_percent_does_not_warn(self) -> None:
        new = self._rewrite_first(79)
        assessment = ed.assess_full_file_rewrite(path="a.txt", original=self.old, updated=new)
        self.assertEqual(assessment.changed_percent, 79)
        self.assertFalse(assessment.should_warn)

    def test_a_rewrite_that_changes_nothing_never_warns(self) -> None:
        assessment = ed.assess_full_file_rewrite(path="a.txt", original=self.old, updated=self.old)
        self.assertFalse(assessment.should_warn)


class SerializationOnlyTests(unittest.TestCase):
    def test_filter_js_scenario_is_serialization_only(self) -> None:
        self.assertNotEqual(_HTML_ORIGINAL, _HTML_RESERIALIZED)
        self.assertTrue(ed.is_serialization_only(_HTML_ORIGINAL, _HTML_RESERIALIZED))

    def test_filter_js_scenario_warns_even_below_the_line_threshold(self) -> None:
        assessment = ed.assess_full_file_rewrite(
            path="clean/page.html", original=_HTML_ORIGINAL, updated=_HTML_RESERIALIZED
        )
        self.assertTrue(assessment.serialization_only)
        self.assertTrue(assessment.should_warn)
        # The point of carrying two signals: this rewrite is caught by
        # serialization-only, not by the line fraction.
        self.assertLessEqual(assessment.changed_fraction, ed.REWRITE_LINE_FRACTION_THRESHOLD)

    def test_identical_content_is_not_serialization_only(self) -> None:
        self.assertFalse(ed.is_serialization_only(_HTML_ORIGINAL, _HTML_ORIGINAL))

    def test_a_real_removal_is_not_serialization_only(self) -> None:
        self.assertFalse(ed.is_serialization_only(_HTML_TARGETED_BEFORE, _HTML_ORIGINAL))

    def test_genuine_small_edit_produces_no_warning(self) -> None:
        assessment = ed.assess_full_file_rewrite(
            path="clean/page.html",
            original=_HTML_TARGETED_BEFORE,
            updated=_HTML_ORIGINAL,
        )
        self.assertFalse(assessment.serialization_only)
        self.assertFalse(assessment.should_warn)
        self.assertIsNone(assessment.warning_text())

    def test_attribute_order_alone(self) -> None:
        self.assertTrue(
            ed.is_serialization_only('<a href="x" class="y">t</a>', '<a class="y" href="x">t</a>')
        )

    def test_self_closing_spelling_alone(self) -> None:
        self.assertTrue(ed.is_serialization_only("<p>a<br>b</p>", "<p>a<br/>b</p>"))

    def test_non_structural_entity_alone(self) -> None:
        self.assertTrue(ed.is_serialization_only("<p>&copy; x</p>", "<p>© x</p>"))

    def test_structural_entities_are_not_collapsed(self) -> None:
        # `&lt;p&gt;` is escaped text; `<p>` is an element. Treating them as the
        # same would be a wrong claim about the document.
        self.assertFalse(ed.is_serialization_only("<div>&lt;p&gt;</div>", "<div><p></div>"))

    def test_attribute_value_change_is_not_serialization_only(self) -> None:
        self.assertFalse(ed.is_serialization_only('<a href="x">t</a>', '<a href="CHANGED">t</a>'))

    def test_reindented_python_is_serialization_only(self) -> None:
        old = "def f():\n    return 1\n"
        new = "def f():\n\treturn 1\n"
        self.assertTrue(ed.is_serialization_only(old, new))

    def test_text_change_is_not_serialization_only(self) -> None:
        self.assertFalse(ed.is_serialization_only("<p>one</p>", "<p>two</p>"))


class RewriteGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = ed.RewriteGuard()
        self.old = "\n".join(_numbered_lines(100))
        self.new = "\n".join(f"CHANGED {index}" for index in range(100))

    def test_warning_text_is_exact(self) -> None:
        warning = self.guard.warn_for_write(
            path="clean/page.html", original=self.old, updated=self.new
        )
        self.assertEqual(
            warning,
            "warning: full-file rewrite of clean/page.html: 100% of lines changed. "
            "If the task requires preserving formatting of untouched regions, "
            "prefer a targeted edit.",
        )

    def test_fires_at_most_once_per_file_per_run(self) -> None:
        first = self.guard.warn_for_write(path="a.html", original=self.old, updated=self.new)
        second = self.guard.warn_for_write(path="a.html", original=self.old, updated=self.new)
        third = self.guard.warn_for_write(path="a.html", original=self.new, updated=self.old)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNone(third)
        self.assertEqual(self.guard.warned_paths(), ("a.html",))

    def test_each_file_gets_its_own_warning(self) -> None:
        self.assertIsNotNone(
            self.guard.warn_for_write(path="a.html", original=self.old, updated=self.new)
        )
        self.assertIsNotNone(
            self.guard.warn_for_write(path="b.html", original=self.old, updated=self.new)
        )

    def test_creating_a_new_file_never_warns(self) -> None:
        self.assertIsNone(self.guard.warn_for_write(path="new.py", original="", updated=self.new))

    def test_small_edit_never_warns_and_never_consumes_the_slot(self) -> None:
        lines = _numbered_lines(100)
        lines[3] = "line 3 = patched"
        patched = "\n".join(lines)
        self.assertIsNone(
            self.guard.warn_for_write(path="a.html", original=self.old, updated=patched)
        )
        self.assertEqual(self.guard.warned_paths(), ())
        # A later wholesale rewrite of the same file still warns.
        self.assertIsNotNone(
            self.guard.warn_for_write(path="a.html", original=self.old, updated=self.new)
        )

    def test_tracked_paths_are_bounded(self) -> None:
        guard = ed.RewriteGuard(max_paths=4)
        for index in range(20):
            guard.warn_for_write(path=f"f{index}.html", original=self.old, updated=self.new)
        self.assertEqual(len(guard.warned_paths()), 4)


class ActionFamilyTests(unittest.TestCase):
    def test_numbered_variants_collapse_to_one_family(self) -> None:
        families = {
            ed.action_family("fs_write", name)
            for name in ("analyze_final.py", "analyze_final2.py", "analyze_final3.py")
        }
        self.assertEqual(families, {"fs_write:analyze_final"})

    def test_directories_are_preserved(self) -> None:
        self.assertEqual(
            ed.action_family("fs_write", "src/analyze_final3.py"),
            "fs_write:src/analyze_final",
        )
        self.assertNotEqual(
            ed.action_family("fs_write", "src/a2.py"),
            ed.action_family("fs_write", "tests/a2.py"),
        )

    def test_windows_separators_normalize(self) -> None:
        self.assertEqual(
            ed.action_family("fs_write", "src\\analyze_final2.py"),
            ed.action_family("fs_write", "src/analyze_final.py"),
        )

    def test_tool_is_part_of_the_family(self) -> None:
        self.assertNotEqual(
            ed.action_family("fs_write", "a.py"), ed.action_family("shell_run", "a.py")
        )

    def test_unrelated_paths_stay_apart(self) -> None:
        self.assertNotEqual(
            ed.action_family("fs_write", "analyze_final.py"),
            ed.action_family("fs_write", "summarize_final.py"),
        )

    def test_year_like_names_are_not_stripped(self) -> None:
        self.assertEqual(ed.path_family("2024"), "2024")

    def test_command_family_reduces_path_arguments(self) -> None:
        self.assertEqual(
            ed.action_family("shell_run", "python analyze_final2.py --check"),
            ed.action_family("shell_run", "python analyze_final3.py --check"),
        )

    def test_command_family_collapses_whitespace(self) -> None:
        self.assertEqual(
            ed.action_family("shell_run", "pytest  -q   tests"),
            ed.action_family("shell_run", "pytest -q tests"),
        )

    def test_different_commands_stay_apart(self) -> None:
        self.assertNotEqual(
            ed.action_family("shell_run", "pytest -q"),
            ed.action_family("shell_run", "pytest -x"),
        )


class ThrashCounterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.counter = ed.ThrashCounter()

    def _fail(self, target: str, times: int, tool: str = "fs_write") -> list[str]:
        notices = []
        for _ in range(times):
            notice = self.counter.record(tool=tool, target=target, failed=True)
            if notice is not None:
                notices.append(notice)
        return notices

    def test_seven_failures_are_silent(self) -> None:
        self.assertEqual(self._fail("analyze_final.py", 7), [])

    def test_eighth_failure_fires_with_exact_text(self) -> None:
        notices = self._fail("analyze_final.py", 8)
        self.assertEqual(
            notices,
            [
                "Progress check: 8 similar attempts on fs_write:analyze_final without a "
                "passing result. Synthesize what you know into a final answer or a "
                "concrete blocker report now."
            ],
        )

    def test_variants_of_one_name_reach_the_threshold_together(self) -> None:
        names = [
            "analyze_final.py",
            "analyze_final2.py",
            "analyze_final3.py",
            "analyze_final4.py",
            "analyze_final5.py",
            "analyze_final6.py",
            "analyze_final7.py",
            "analyze_final8.py",
        ]
        notices = [
            notice
            for name in names
            if (notice := self.counter.record(tool="fs_write", target=name, failed=True))
        ]
        self.assertEqual(len(notices), 1)
        self.assertIn("8 similar attempts on fs_write:analyze_final", notices[0])

    def test_fires_at_most_once_per_family(self) -> None:
        self.assertEqual(len(self._fail("analyze_final.py", 20)), 1)

    def test_fires_at_most_twice_per_run(self) -> None:
        self.assertEqual(len(self._fail("a.py", 8)), 1)
        self.assertEqual(len(self._fail("b.py", 8)), 1)
        self.assertEqual(len(self._fail("c.py", 8)), 0)
        self.assertEqual(self.counter.notices_sent, 2)

    def test_a_passing_result_resets_the_family(self) -> None:
        self._fail("a.py", 7)
        self.assertIsNone(self.counter.record(tool="fs_write", target="a.py", failed=False))
        self.assertEqual(self.counter.failure_count("fs_write:a"), 0)
        self.assertEqual(self._fail("a.py", 7), [])
        self.assertEqual(len(self._fail("a.py", 1)), 1)

    def test_success_does_not_consume_a_notice(self) -> None:
        self.counter.record(tool="fs_write", target="a.py", failed=False)
        self.assertEqual(self.counter.notices_sent, 0)

    def test_families_are_counted_independently(self) -> None:
        self._fail("a.py", 7)
        self._fail("b.py", 7)
        self.assertEqual(self.counter.failure_count("fs_write:a"), 7)
        self.assertEqual(self.counter.failure_count("fs_write:b"), 7)

    def test_empty_target_is_ignored(self) -> None:
        self.assertIsNone(self.counter.record(tool="", target="", failed=True))

    def test_tracked_families_are_bounded(self) -> None:
        counter = ed.ThrashCounter(max_families=4)
        for index in range(50):
            counter.record(tool="fs_write", target=f"f{index}x.py", failed=True)
        self.assertLessEqual(len(counter._failures), 4)


# The gcode-to-text working set, as observed: 52 scratch files left in the tree
# alongside the files the task actually cared about.
_GCODE_CREATED = [
    "analyze_final.py",
    "analyze_final2.py",
    "analyze_final3.py",
    "analysis3_output.txt",
    "analyze_gcode.py",
    "analysis_output.txt",
    "analysis_output2.txt",
    "scratch_notes.md",
    "tmp_parse.py",
    "parse2.py",
    "parse.py",
    "solution.py",
    "output.txt",
    "README.md",
]


class ScratchMatcherTests(unittest.TestCase):
    def test_gcode_working_set(self) -> None:
        found = ed.find_scratch_files(_GCODE_CREATED)
        self.assertEqual(
            found,
            (
                "analyze_final.py",
                "analyze_final2.py",
                "analyze_final3.py",
                "analysis3_output.txt",
                "analyze_gcode.py",
                "analysis_output.txt",
                "analysis_output2.txt",
                "scratch_notes.md",
                "tmp_parse.py",
                "parse2.py",
            ),
        )

    def test_deliverables_are_not_reported(self) -> None:
        found = ed.find_scratch_files(_GCODE_CREATED)
        for keeper in ("solution.py", "output.txt", "README.md", "parse.py"):
            self.assertNotIn(keeper, found)

    def test_numbered_file_without_a_sibling_is_kept(self) -> None:
        self.assertEqual(ed.find_scratch_files(["utils2.py"]), ())

    def test_numbered_file_with_a_created_sibling_is_scratch(self) -> None:
        self.assertEqual(ed.find_scratch_files(["utils.py", "utils2.py"]), ("utils2.py",))

    def test_sibling_may_be_a_pre_existing_file(self) -> None:
        self.assertEqual(
            ed.find_scratch_files(["src/utils2.py"], existing_paths=["src/utils.py"]),
            ("src/utils2.py",),
        )

    def test_sibling_search_is_directory_scoped(self) -> None:
        self.assertEqual(
            ed.find_scratch_files(["src/utils2.py"], existing_paths=["other/utils.py"]),
            (),
        )

    def test_underscore_numbered_variant(self) -> None:
        self.assertEqual(ed.find_scratch_files(["report.md", "report_2.md"]), ("report_2.md",))

    def test_referenced_paths_are_never_reported(self) -> None:
        self.assertEqual(
            ed.find_scratch_files(_GCODE_CREATED, referenced_paths=["analyze_final.py"])[0],
            "analyze_final2.py",
        )

    def test_scratch_words_need_a_boundary(self) -> None:
        self.assertFalse(ed.looks_like_scratch_name("template_engine.py"))
        self.assertFalse(ed.looks_like_scratch_name("contemporary.md"))
        self.assertTrue(ed.looks_like_scratch_name("tmp_x.py"))
        self.assertTrue(ed.looks_like_scratch_name("src_scratch-1.py"))

    def test_ordinary_source_names_are_never_scratch(self) -> None:
        keepers = [
            "src/main.py",
            "tests/test_parser.py",
            "docs/index.html",
            "Makefile",
            "pyproject.toml",
        ]
        self.assertEqual(ed.find_scratch_files(keepers), ())

    def test_summary_line_is_exact(self) -> None:
        self.assertEqual(
            ed.scratch_summary_line(("a.py", "b.py")),
            "Scratch files left in tree: a.py, b.py",
        )

    def test_summary_line_is_none_for_a_clean_tree(self) -> None:
        self.assertIsNone(ed.scratch_summary_line(()))

    def test_summary_line_is_bounded(self) -> None:
        line = ed.scratch_summary_line(tuple(f"f{i}.py" for i in range(25)), limit=10)
        self.assertIn("... (+15 more)", line)


class EditDisciplineStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = ed.EditDisciplineState()

    def test_created_paths_keep_order_and_deduplicate(self) -> None:
        for path in ("b.py", "a.py", "b.py"):
            self.state.record_created(path)
        self.assertEqual(self.state.created_paths(), ("b.py", "a.py"))

    def test_created_paths_are_bounded(self) -> None:
        state = ed.EditDisciplineState(max_created_paths=5)
        for index in range(50):
            state.record_created(f"f{index}.py")
        self.assertEqual(len(state.created_paths()), 5)

    def test_blank_paths_are_ignored(self) -> None:
        self.state.record_created("   ")
        self.state.record_created("")
        self.assertEqual(self.state.created_paths(), ())

    def test_end_to_end_gcode_run(self) -> None:
        for path in _GCODE_CREATED:
            self.state.record_created(path)
        for name in ("analyze_final.py", "analyze_final2.py", "analyze_final3.py"):
            self.state.record_attempt(tool="fs_write", target=name, failed=True)
        for _ in range(5):
            self.state.record_attempt(tool="fs_write", target="analyze_final9.py", failed=True)
        self.assertEqual(self.state.thrash.notices_sent, 1)
        self.assertEqual(
            self.state.scratch_summary_line(),
            "Scratch files left in tree: analyze_final.py, analyze_final2.py, "
            "analyze_final3.py, analysis3_output.txt, analyze_gcode.py, "
            "analysis_output.txt, analysis_output2.txt, scratch_notes.md, "
            "tmp_parse.py, parse2.py",
        )


class IdempotenceTests(unittest.TestCase):
    """Re-running any read-only accessor must not change what it reports."""

    def test_assessment_is_pure(self) -> None:
        old = "\n".join(_numbered_lines(100))
        new = "\n".join(f"CHANGED {i}" for i in range(100))
        first = ed.assess_full_file_rewrite(path="a.py", original=old, updated=new)
        second = ed.assess_full_file_rewrite(path="a.py", original=old, updated=new)
        self.assertEqual(first, second)

    def test_normalization_is_a_fixed_point(self) -> None:
        once = ed.normalize_for_serialization(_HTML_RESERIALIZED)
        self.assertEqual(ed.normalize_for_serialization(once), once)

    def test_repeated_scratch_scans_agree(self) -> None:
        state = ed.EditDisciplineState()
        for path in _GCODE_CREATED:
            state.record_created(path)
        self.assertEqual(state.scratch_files(), state.scratch_files())
        self.assertEqual(state.scratch_summary_line(), state.scratch_summary_line())

    def test_replaying_the_same_run_reaches_the_same_state(self) -> None:
        def replay() -> tuple[int, tuple[str, ...]]:
            state = ed.EditDisciplineState()
            for path in _GCODE_CREATED:
                state.record_created(path)
                state.record_attempt(tool="fs_write", target=path, failed=True)
            return state.thrash.notices_sent, state.scratch_files()

        self.assertEqual(replay(), replay())

    def test_repeated_writes_do_not_re_warn(self) -> None:
        state = ed.EditDisciplineState()
        old = "\n".join(_numbered_lines(100))
        new = "\n".join(f"CHANGED {i}" for i in range(100))
        warnings = [state.warn_for_write(path="a.py", original=old, updated=new) for _ in range(5)]
        self.assertEqual(len([w for w in warnings if w]), 1)

    def test_repeated_notices_do_not_re_fire(self) -> None:
        state = ed.EditDisciplineState()
        notices = [
            state.record_attempt(tool="fs_write", target="a.py", failed=True) for _ in range(30)
        ]
        self.assertEqual(len([n for n in notices if n]), 1)


class ModelVisibleStringTests(unittest.TestCase):
    """The two strings this PR shows the model, pinned verbatim."""

    def test_rewrite_warning_template(self) -> None:
        self.assertEqual(
            ed.REWRITE_WARNING_TEMPLATE,
            "warning: full-file rewrite of {path}: {pct}% of lines changed. "
            "If the task requires preserving formatting of untouched regions, "
            "prefer a targeted edit.",
        )

    def test_thrash_notice_template(self) -> None:
        self.assertEqual(
            ed.THRASH_NOTICE_TEMPLATE,
            "Progress check: {n} similar attempts on {family} without a passing result. "
            "Synthesize what you know into a final answer or a concrete blocker report now.",
        )

    def test_scratch_summary_line_template(self) -> None:
        self.assertEqual(ed.SCRATCH_FILES_SUMMARY_LINE, "Scratch files left in tree: {list}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
