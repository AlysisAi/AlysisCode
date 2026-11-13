"""Tests for the canary suite's parsing/aggregation core.

Runnable two ways:

    python3 tests/test_canary_suite.py     # standalone, stdlib only
    pytest tests/test_canary_suite.py

``scripts/canary_suite.py`` is loaded by file path so importing it never pulls
in the package, Docker, or Harbor. Every metric is extracted from synthetic
fixture artifacts written to a temp dir.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "canary_suite.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_canary_suite", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cs = _load_module()
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Task list parsing
# ---------------------------------------------------------------------------


class TestReadTaskList(unittest.TestCase):
    def test_strips_comments_and_blanks(self):
        tmp = Path(tempfile.mkdtemp())
        _write(tmp, "tasks.txt", "# comment\n\nalpha\n  beta  \n# another\ngamma\n")
        self.assertEqual(cs.read_task_list(tmp / "tasks.txt"), ["alpha", "beta", "gamma"])

    def test_committed_task_list_parses_to_fifteen(self):
        tasks = cs.read_task_list(_REPO_ROOT / "scripts" / "canary_tasks.txt")
        self.assertEqual(len(tasks), 15, f"expected 15 canary tasks, got {len(tasks)}: {tasks}")
        # The seven regression targets must NOT appear in the canary set.
        for excluded in (
            "compile-compcert",
            "hf-model-inference",
            "configure-git-webserver",
            "polyglot-rust-c",
            "qemu-alpine-ssh",
            "filter-js-from-html",
            "gcode-to-text",
        ):
            self.assertNotIn(excluded, tasks)


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------


class TestParseTaskMetrics(unittest.TestCase):
    def _task_dir(self, records_jsonl=None, results_json=None):
        tmp = Path(tempfile.mkdtemp())
        if records_jsonl is not None:
            _write(
                tmp,
                "sessions/s.jsonl",
                "".join(json.dumps(r) + "\n" for r in records_jsonl),
            )
        if results_json is not None:
            _write(tmp, "results.json", json.dumps(results_json))
        return tmp

    def test_resolved_bool_from_results(self):
        tmp = self._task_dir(results_json={"resolved": True})
        m = cs.parse_task_metrics(tmp, "t")
        self.assertIs(m.resolved, True)

    def test_resolved_from_reward(self):
        tmp = self._task_dir(results_json={"reward": 1})
        m = cs.parse_task_metrics(tmp, "t")
        self.assertIs(m.resolved, True)
        tmp2 = self._task_dir(results_json={"reward": 0})
        self.assertIs(cs.parse_task_metrics(tmp2, "t").resolved, False)

    def test_dispatch_overhead_float_and_dict_forms(self):
        records = [
            {"type": "shell_run_result", "payload": {"dispatch_overhead_seconds": 0.25}},
            {
                "type": "deadline_exhausted",
                "payload": {
                    "deadline": {
                        "duration_observations": {
                            "dispatch_overhead_seconds": {"count": 2, "total": 0.75}
                        }
                    }
                },
            },
        ]
        m = cs.parse_task_metrics(self._task_dir(records_jsonl=records), "t")
        self.assertAlmostEqual(m.dispatch_overhead_seconds, 1.0)

    def test_cost_and_tokens_summed(self):
        records = [
            {"type": "provider_call", "payload": {"cost_usd": 0.01, "total_tokens": 1000}},
            {"type": "provider_call", "payload": {"cost_usd": 0.02, "total_tokens": 500}},
        ]
        m = cs.parse_task_metrics(self._task_dir(records_jsonl=records), "t")
        self.assertAlmostEqual(m.cost_usd, 0.03)
        self.assertEqual(m.tokens, 1500)

    def test_missing_metrics_are_none(self):
        m = cs.parse_task_metrics(self._task_dir(results_json={"note": "x"}), "t")
        self.assertIsNone(m.resolved)
        self.assertIsNone(m.dispatch_overhead_seconds)
        self.assertIsNone(m.cost_usd)

    def test_deep_find_key(self):
        obj = {"a": {"b": [{"cost_usd": 1.5}, {"cost_usd": 2.5}]}}
        self.assertEqual(list(cs.deep_find_key(obj, "cost_usd")), [1.5, 2.5])


# ---------------------------------------------------------------------------
# Aggregation + baseline comparison
# ---------------------------------------------------------------------------


class TestSummaryAndBaseline(unittest.TestCase):
    def _rows(self):
        return [
            cs.TaskMetrics("a", True, 0.2, 0.01, 100),
            cs.TaskMetrics("b", True, 0.4, 0.02, 200),
            cs.TaskMetrics("c", False, 0.6, 0.03, 300),
            cs.TaskMetrics("d", None, None, None, None),
        ]

    def test_summary_counts(self):
        s = cs.summarize(self._rows())
        self.assertEqual(s.total, 4)
        self.assertEqual(s.pass_count, 2)
        self.assertEqual(s.fail_count, 1)
        self.assertEqual(s.unknown_count, 1)
        self.assertAlmostEqual(s.mean_dispatch_overhead_seconds, 0.4)
        self.assertAlmostEqual(s.total_cost_usd, 0.06)

    def test_verdict_regression_on_failure(self):
        s = cs.summarize(self._rows())
        text, regressed = cs.evaluate_verdict(s, cs.Baseline())
        self.assertTrue(regressed)
        self.assertIn("REGRESSION", text)

    def test_verdict_ok_when_all_pass(self):
        rows = [cs.TaskMetrics("a", True, 0.1, 0.01, 10), cs.TaskMetrics("b", True, 0.1, 0.01, 10)]
        s = cs.summarize(rows)
        text, regressed = cs.evaluate_verdict(s, cs.Baseline())
        self.assertFalse(regressed)
        self.assertIn("OK", text)

    def test_verdict_regression_when_below_baseline_passcount(self):
        rows = [
            cs.TaskMetrics("a", True, 0.1, 0.01, 10),
            cs.TaskMetrics("b", None, None, None, None),
        ]
        s = cs.summarize(rows)
        text, regressed = cs.evaluate_verdict(s, cs.Baseline(pass_count=2))
        self.assertTrue(regressed)

    def test_verdict_inconclusive_when_all_unknown(self):
        rows = [cs.TaskMetrics("a", None, None, None, None)]
        s = cs.summarize(rows)
        text, regressed = cs.evaluate_verdict(s, cs.Baseline())
        self.assertFalse(regressed)
        self.assertIn("INCONCLUSIVE", text)

    def test_baseline_from_json(self):
        tmp = Path(tempfile.mkdtemp())
        _write(tmp, "b.json", json.dumps({"pass_count": 15, "total_cost_usd": 1.23}))
        b = cs.Baseline.from_json(tmp / "b.json")
        self.assertEqual(b.pass_count, 15)
        self.assertAlmostEqual(b.total_cost_usd, 1.23)

    def test_render_table_contains_rows_and_verdict(self):
        s = cs.summarize(self._rows())
        table = cs.render_table(s, cs.Baseline())
        self.assertIn("task", table)
        self.assertIn("VERDICT", table)
        self.assertIn("Comparison to baseline", table)
        for name in ("a", "b", "c", "d"):
            self.assertIn(name, table)


if __name__ == "__main__":
    unittest.main(verbosity=2)
