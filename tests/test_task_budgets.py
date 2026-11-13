"""Tests for the Terminal-Bench per-task budget table and its generator.

Runnable two ways:

    python3 tests/test_task_budgets.py     # standalone, stdlib only
    pytest tests/test_task_budgets.py

Both modules under test are loaded directly from their file paths. The table
module is imported by ``box_harbor_agent.py``, which needs the installed
package; the generator is a script. Loading by path keeps these tests runnable
in a bare interpreter with no third-party packages installed.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TABLE_PATH = _REPO_ROOT / "scripts" / "benchmarks" / "terminal_bench" / "task_budgets.py"
_GENERATOR_PATH = _REPO_ROOT / "scripts" / "generate_tb_task_budgets.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tb = _load_module("_task_budgets", _TABLE_PATH)
gen = _load_module("_generate_tb_task_budgets", _GENERATOR_PATH)


# The per-task Harbor limits, in minutes, that the table is derived from.
# Restated here independently so the test is a real cross-check of the
# arithmetic rather than a copy of the table. 22 are the exact configured
# limit Harbor reported in its AgentTimeoutError message (campaign
# 20260831T110121Z, focus-64); the last 3 are earlier wall-clock kill
# observations for tasks that were never killed in that campaign.
OBSERVED_LIMIT_MINUTES: dict[str, int] = {
    # exact (from "timed out after N seconds")
    "adaptive-rejection-sampler": 60,
    "chess-best-move": 60,
    "code-from-image": 80,
    "db-wal-recovery": 60,
    "extract-moves-from-video": 120,
    "fix-code-vulnerability": 60,
    "gcode-to-text": 60,
    "gpt2-codegolf": 60,
    "make-doom-for-mips": 60,
    "make-mips-interpreter": 120,
    "mcmc-sampling-stan": 120,
    "mteb-retrieve": 120,
    "overfull-hbox": 50,
    "password-recovery": 60,
    "path-tracing": 120,
    "path-tracing-reverse": 120,
    "qemu-alpine-ssh": 60,
    "qemu-startup": 60,
    "raman-fitting": 60,
    "schemelike-metacircular-eval": 160,
    "tune-mjcf": 60,
    "write-compressor": 60,
    # wall-clock (unobserved in 20260831T110121Z)
    "custom-memory-heap-crash": 122,
    "dna-assembly": 122,
    "mailman": 123,
}


class TestSeededTable(unittest.TestCase):
    """Every seeded value is exactly 0.85 x the observed per-task limit."""

    def test_all_seeds_present(self) -> None:
        self.assertEqual(len(OBSERVED_LIMIT_MINUTES), 25)
        self.assertEqual(
            sorted(tb.TASK_BUDGET_SECONDS),
            sorted(OBSERVED_LIMIT_MINUTES),
        )

    def test_values_match_the_fraction_rule(self) -> None:
        for task, minutes in OBSERVED_LIMIT_MINUTES.items():
            with self.subTest(task=task):
                expected = int(minutes * 60 * tb.BUDGET_FRACTION_OF_OBSERVED_LIMIT)
                self.assertEqual(tb.TASK_BUDGET_SECONDS[task], expected)

    def test_the_documented_worked_example(self) -> None:
        # 160 min * 60 * 0.85 = 8160 (Harbor's exact 9600 s limit).
        self.assertEqual(tb.TASK_BUDGET_SECONDS["schemelike-metacircular-eval"], 8160)
        self.assertEqual(tb.BUDGET_FRACTION_OF_OBSERVED_LIMIT, 0.85)

    def test_every_budget_is_under_its_observed_limit(self) -> None:
        # The whole point: the agent's deadline must arrive before Harbor's.
        for task, minutes in OBSERVED_LIMIT_MINUTES.items():
            with self.subTest(task=task):
                self.assertLess(tb.TASK_BUDGET_SECONDS[task], minutes * 60)

    def test_values_are_plain_ints(self) -> None:
        for task, value in tb.TASK_BUDGET_SECONDS.items():
            with self.subTest(task=task):
                self.assertIsInstance(value, int)
                self.assertGreater(value, 0)

    def test_table_is_sorted(self) -> None:
        # The generator writes it sorted; a hand edit that breaks the order
        # would produce a noisy diff on the next regeneration.
        self.assertEqual(list(tb.TASK_BUDGET_SECONDS), sorted(tb.TASK_BUDGET_SECONDS))


class TestBudgetForTask(unittest.TestCase):
    def test_listed_task_returns_its_budget(self) -> None:
        self.assertEqual(tb.budget_for_task("overfull-hbox", default=10800), 2550)

    def test_unlisted_task_falls_back_to_default(self) -> None:
        self.assertEqual(tb.budget_for_task("some-other-task", default=10800), 10800)

    def test_empty_and_none_fall_back(self) -> None:
        for name in ("", "   ", None):
            with self.subTest(name=name):
                self.assertEqual(tb.budget_for_task(name, default=999), 999)

    def test_dataset_prefixed_name_is_accepted(self) -> None:
        # Harbor reports a task as "<dataset>/<task>" in config.json.
        self.assertEqual(
            tb.budget_for_task("terminal-bench/overfull-hbox", default=10800),
            2550,
        )

    def test_default_is_coerced_to_int(self) -> None:
        self.assertEqual(tb.budget_for_task("nope", default="7200"), 7200)


class TestBudgetLookup(unittest.TestCase):
    """The flag, not the number: a table miss must be distinguishable.

    A miss and a hit yield the same kind of integer, so an artifact recording
    only the number cannot say whether the table was consulted -- and a run
    that silently fell back to the flat deadline behaves exactly like one from
    before the table existed. That ambiguity is what made a previous campaign's
    artifacts unexplainable after the fact.
    """

    def test_a_listed_task_reports_a_hit(self) -> None:
        self.assertEqual(tb.budget_lookup("overfull-hbox", default=10800), (2550, True))

    def test_an_unlisted_task_reports_a_miss(self) -> None:
        self.assertEqual(tb.budget_lookup("some-other-task", default=10800), (10800, False))

    def test_an_unidentified_task_reports_a_miss(self) -> None:
        for name in ("", "   ", None):
            with self.subTest(name=name):
                self.assertEqual(tb.budget_lookup(name, default=999), (999, False))

    def test_a_dataset_prefixed_name_still_hits(self) -> None:
        self.assertEqual(
            tb.budget_lookup("terminal-bench/overfull-hbox", default=10800), (2550, True)
        )

    def test_the_default_is_coerced_to_int(self) -> None:
        self.assertEqual(tb.budget_lookup("nope", default="7200"), (7200, False))

    def test_budget_for_task_agrees_with_the_lookup(self) -> None:
        # The thin wrapper must not drift from the function it delegates to.
        for name in ("overfull-hbox", "terminal-bench/dna-assembly", "nope", "", None):
            with self.subTest(name=name):
                self.assertEqual(
                    tb.budget_for_task(name, default=10800),
                    tb.budget_lookup(name, default=10800)[0],
                )

    def test_every_listed_task_reports_a_hit(self) -> None:
        for name in tb.TASK_BUDGET_SECONDS:
            with self.subTest(name=name):
                seconds, hit = tb.budget_lookup(name, default=10800)
                self.assertTrue(hit)
                self.assertEqual(seconds, tb.TASK_BUDGET_SECONDS[name])


class TestTaskNameFromLogsDir(unittest.TestCase):
    """Harbor's trial directory is the only task identity the adapter gets."""

    def test_plain_trial_dir(self) -> None:
        self.assertEqual(
            tb.task_name_from_trial_dir("overfull-hbox__9ywoXbN"),
            "overfull-hbox",
        )

    def test_task_name_containing_double_underscore(self) -> None:
        # SWE-bench-style names contain "__" themselves; the split must anchor
        # on the LAST one or the name is truncated.
        self.assertEqual(
            tb.task_name_from_trial_dir("astropy__astropy-13236__9ywoXbN"),
            "astropy__astropy-13236",
        )

    def test_non_trial_dir_names_yield_nothing(self) -> None:
        for name in ("", "agent", "logs", "overfull-hbox", "task__short", None):
            with self.subTest(name=name):
                self.assertEqual(tb.task_name_from_trial_dir(name), "")

    def test_token_must_be_exactly_seven_alphanumerics(self) -> None:
        self.assertEqual(tb.task_name_from_trial_dir("t__abcdef"), "")  # 6
        self.assertEqual(tb.task_name_from_trial_dir("t__abcdefgh"), "")  # 8
        self.assertEqual(tb.task_name_from_trial_dir("t__abcde-g"), "")  # non-alnum
        self.assertEqual(tb.task_name_from_trial_dir("t__abcdefg"), "t")

    def test_logs_dir_walks_up_to_the_trial_dir(self) -> None:
        # logs_dir may be the trial dir itself or a subdirectory of it; this
        # repo cannot pin down which, so both must work.
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp) / "jobs" / "job-1" / "mailman__zCCmmK7"
            (trial / "agent").mkdir(parents=True)
            self.assertEqual(tb.task_name_from_logs_dir(trial), "mailman")
            self.assertEqual(tb.task_name_from_logs_dir(trial / "agent"), "mailman")

    def test_unrecognised_logs_dir_degrades_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tb.task_name_from_logs_dir(Path(tmp)), "")
        self.assertEqual(tb.task_name_from_logs_dir(None), "")

    def test_end_to_end_logs_dir_to_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp) / "path-tracing-reverse__aB3xYz9"
            trial.mkdir()
            name = tb.task_name_from_logs_dir(trial)
            self.assertEqual(tb.budget_for_task(name, default=10800), 6120)


class TestDurationParsing(unittest.TestCase):
    def test_bare_numbers(self) -> None:
        self.assertEqual(gen.parse_duration_seconds(3600), 3600.0)
        self.assertEqual(gen.parse_duration_seconds("3600"), 3600.0)
        self.assertEqual(gen.parse_duration_seconds(1800.5), 1800.5)

    def test_unit_suffixes(self) -> None:
        self.assertEqual(gen.parse_duration_seconds("3600s"), 3600.0)
        self.assertEqual(gen.parse_duration_seconds("60m"), 3600.0)
        self.assertEqual(gen.parse_duration_seconds("1h"), 3600.0)
        self.assertEqual(gen.parse_duration_seconds("90 minutes"), 5400.0)
        self.assertEqual(gen.parse_duration_seconds("2 hours"), 7200.0)

    def test_quoted_values(self) -> None:
        self.assertEqual(gen.parse_duration_seconds('"3600"'), 3600.0)
        self.assertEqual(gen.parse_duration_seconds("'60m'"), 3600.0)

    def test_rejects_non_durations(self) -> None:
        for raw in (None, "", "   ", "abc", "1x", "-5", 0, -1, "nan", [], {}):
            with self.subTest(raw=raw):
                self.assertIsNone(gen.parse_duration_seconds(raw))

    def test_booleans_are_not_durations(self) -> None:
        # bool is an int in Python; True must not become 1 second.
        self.assertIsNone(gen.parse_duration_seconds(True))
        self.assertIsNone(gen.parse_duration_seconds(False))


class TestKeyPriority(unittest.TestCase):
    def test_specific_key_beats_generic_in_json(self) -> None:
        data = {"timeout": 100, "agent_timeout": 900}
        self.assertEqual(gen.find_timeout_in_mapping(data), 900.0)

    def test_nested_specific_key_beats_top_level_generic(self) -> None:
        data = {"timeout": 100, "agent": {"agent_timeout_sec": 900}}
        self.assertEqual(gen.find_timeout_in_mapping(data), 900.0)

    def test_generic_key_used_when_nothing_specific(self) -> None:
        self.assertEqual(gen.find_timeout_in_mapping({"timeout": 250}), 250.0)

    def test_no_timeout_returns_none(self) -> None:
        self.assertIsNone(gen.find_timeout_in_mapping({"name": "x", "tags": ["a"]}))

    def test_searches_inside_lists(self) -> None:
        data = {"stages": [{"name": "run", "agent_timeout": "45m"}]}
        self.assertEqual(gen.find_timeout_in_mapping(data), 2700.0)


class TestTextScanning(unittest.TestCase):
    def test_yaml_shape(self) -> None:
        text = "name: overfull-hbox\nagent_timeout_sec: 3300\n"
        self.assertEqual(gen.find_timeout_in_text(text), 3300.0)

    def test_indented_yaml_block(self) -> None:
        text = "agent:\n  timeout: 3300\n"
        self.assertEqual(gen.find_timeout_in_text(text), 3300.0)

    def test_toml_shape(self) -> None:
        text = '[agent]\nname = "x"\nagent_timeout = "55m"\n'
        self.assertEqual(gen.find_timeout_in_text(text), 3300.0)

    def test_comments_are_stripped(self) -> None:
        text = "timeout: 3300  # seconds, per task\n"
        self.assertEqual(gen.find_timeout_in_text(text), 3300.0)

    def test_commented_out_line_is_ignored(self) -> None:
        text = "# agent_timeout: 99\ntimeout: 3300\n"
        self.assertEqual(gen.find_timeout_in_text(text), 3300.0)

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(gen.find_timeout_in_text("name: x\ndifficulty: hard\n"))

    def test_unparseable_value_does_not_win(self) -> None:
        # A "timeout: null" must not shadow a usable value elsewhere.
        text = "agent_timeout: null\ntimeout: 3300\n"
        self.assertEqual(gen.find_timeout_in_text(text), 3300.0)


class TestDeriveBudget(unittest.TestCase):
    def test_fraction_is_applied(self) -> None:
        self.assertEqual(gen.derive_budget_seconds(3300, fraction=0.85), 2805)

    def test_agent_multiplier_is_applied(self) -> None:
        self.assertEqual(
            gen.derive_budget_seconds(3300, agent_multiplier=2.0, fraction=0.85),
            5610,
        )

    def test_cap_clamps(self) -> None:
        self.assertEqual(
            gen.derive_budget_seconds(100000, fraction=1.0, cap=10800),
            10800,
        )

    def test_cap_does_not_raise_a_small_budget(self) -> None:
        self.assertEqual(gen.derive_budget_seconds(600, fraction=0.85, cap=10800), 510)

    def test_result_is_an_int_floor(self) -> None:
        value = gen.derive_budget_seconds(101, fraction=0.85)  # 85.85
        self.assertIsInstance(value, int)
        self.assertEqual(value, 85)

    def test_never_returns_zero(self) -> None:
        self.assertEqual(gen.derive_budget_seconds(1, fraction=0.0001), 1)

    def test_reproduces_the_checked_in_table(self) -> None:
        # The generator's arithmetic and the committed table must agree, or
        # regenerating would silently churn every value.
        for task, minutes in OBSERVED_LIMIT_MINUTES.items():
            with self.subTest(task=task):
                self.assertEqual(
                    gen.derive_budget_seconds(minutes * 60, fraction=0.85),
                    tb.TASK_BUDGET_SECONDS[task],
                )


class TestDiscoverTaskTimeouts(unittest.TestCase):
    def _dataset(self, root: Path, layout: dict[str, tuple[str, str]]) -> None:
        for task, (filename, content) in layout.items():
            task_dir = root / task
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / filename).write_text(content, encoding="utf-8")

    def test_each_supported_config_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._dataset(
                root,
                {
                    "yaml-task": ("task.yaml", "agent_timeout_sec: 3300\n"),
                    "yml-task": ("task.yml", "timeout: 55m\n"),
                    "toml-task": ("task.toml", 'agent_timeout = "3300s"\n'),
                    "json-task": ("task.json", json.dumps({"agent_timeout": 3300})),
                    "config-json-task": (
                        "config.json",
                        json.dumps({"agent": {"timeout_sec": 3300}}),
                    ),
                },
            )
            timeouts, skipped = gen.discover_task_timeouts(root)

        self.assertEqual(skipped, [])
        self.assertEqual(
            timeouts,
            {
                "yaml-task": 3300.0,
                "yml-task": 3300.0,
                "toml-task": 3300.0,
                "json-task": 3300.0,
                "config-json-task": 3300.0,
            },
        )

    def test_tasks_subdirectory_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._dataset(root / "tasks", {"nested": ("task.yaml", "timeout: 600\n")})
            timeouts, _ = gen.discover_task_timeouts(root)
        self.assertEqual(timeouts, {"nested": 600.0})

    def test_task_without_a_timeout_is_skipped_not_invented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._dataset(
                root,
                {
                    "has-it": ("task.yaml", "timeout: 600\n"),
                    "lacks-it": ("task.yaml", "name: lacks-it\ndifficulty: hard\n"),
                },
            )
            timeouts, skipped = gen.discover_task_timeouts(root)

        self.assertEqual(timeouts, {"has-it": 600.0})
        self.assertEqual(skipped, ["lacks-it"])

    def test_directory_with_no_config_is_not_reported_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "not-a-task").mkdir()
            (root / ".hidden").mkdir()
            timeouts, skipped = gen.discover_task_timeouts(root)
        self.assertEqual(timeouts, {})
        self.assertEqual(skipped, [])

    def test_malformed_json_falls_through_to_the_text_scanner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Trailing comma: not valid JSON, still readable line-wise.
            self._dataset(root, {"loose": ("task.json", '{\n "timeout": 900,\n}\n')})
            timeouts, _ = gen.discover_task_timeouts(root)
        self.assertEqual(timeouts, {"loose": 900.0})

    def test_end_to_end_build_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._dataset(
                root,
                {
                    "short": ("task.yaml", "agent_timeout_sec: 3300\n"),
                    "very-long": ("task.yaml", "agent_timeout_sec: 100000\n"),
                },
            )
            timeouts, _ = gen.discover_task_timeouts(root)
            budgets = gen.build_budgets(timeouts, fraction=0.85, cap=10800)

        self.assertEqual(budgets, {"short": 2805, "very-long": 10800})


class TestRenderAndReplace(unittest.TestCase):
    def test_render_is_sorted_and_stable(self) -> None:
        rendered = gen.render_table_block({"b": 2, "a": 1})
        self.assertEqual(
            rendered,
            'TASK_BUDGET_SECONDS: dict[str, int] = {\n    "a": 1,\n    "b": 2,\n}',
        )
        self.assertEqual(rendered, gen.render_table_block({"a": 1, "b": 2}))

    def test_render_of_an_empty_table(self) -> None:
        self.assertEqual(
            gen.render_table_block({}),
            "TASK_BUDGET_SECONDS: dict[str, int] = {\n}",
        )

    def test_replace_swaps_only_the_table(self) -> None:
        source = _TABLE_PATH.read_text(encoding="utf-8")
        updated = gen.replace_table_block(source, {"only-task": 42})

        self.assertIn('"only-task": 42,', updated)
        self.assertNotIn('"overfull-hbox"', updated.split("TASK_BUDGET_SECONDS")[1])
        # Docstring, provenance notes and helpers all survive.
        self.assertIn("Provenance of the numbers", updated)
        self.assertIn("def budget_for_task(", updated)
        self.assertIn("def task_name_from_logs_dir(", updated)
        self.assertIn("BUDGET_FRACTION_OF_OBSERVED_LIMIT = 0.85", updated)

    def test_replace_is_idempotent_against_the_committed_table(self) -> None:
        # Regenerating with the same numbers must produce no diff at all.
        source = _TABLE_PATH.read_text(encoding="utf-8")
        self.assertEqual(gen.replace_table_block(source, tb.TASK_BUDGET_SECONDS), source)

    def test_replace_without_a_table_raises(self) -> None:
        with self.assertRaises(ValueError):
            gen.replace_table_block("nothing to see here\n", {"a": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
