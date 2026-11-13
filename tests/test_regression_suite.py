"""Tests for the defect regression suite's artifact-inspection/assertion core.

Runnable two ways:

    python3 tests/test_regression_suite.py     # standalone, stdlib only
    pytest tests/test_regression_suite.py

``scripts/regression_suite.py`` is loaded directly from its file path so that
importing it never executes ``alysis_code/__init__`` or pulls in Docker,
Harbor, or any third-party dependency. Every assertion here runs against
synthetic fixture artifacts written to a temp dir -- the same JSONL shapes the
real Harbor archives carry -- so the testable core is exercised with no runner
box.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "regression_suite.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_regression_suite", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rs = _load_module()


# ---------------------------------------------------------------------------
# Fixture helpers: write realistic session/crash JSONL into a temp archive
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records),
        encoding="utf-8",
    )


CLEAN_BUILD_SNAPSHOT = {
    "type": "config_snapshot",
    "payload": {
        "schema_version": 1,
        "version": "0.13.0",
        "build": {
            "schema_version": 1,
            "commit": "a" * 40,
            "commit_short": "aaaaaaaaaaaa",
            "timestamp": "2026-08-21T00:00:00Z",
            "dirty": False,
            "source": "git",
            "identifiable": True,
            "clean": True,
        },
        "sampling": {"configured": True, "temperature": 0.0, "top_p": 1.0, "seed": 7},
    },
}

DIRTY_BUILD_SNAPSHOT = {
    "type": "config_snapshot",
    "payload": {
        "build": {
            "commit": "",
            "dirty": True,
            "source": "dev-default",
            "identifiable": False,
            "clean": False,
        }
    },
}

BUDGET_FINAL = {
    "type": "final",
    "payload": {
        "content": "stopped: out of budget",
        "degraded": True,
        "degraded_reason": "run_budget_exhausted",
        "stop_reason": "run_budget_exhausted",
    },
}

DEADLINE_EXHAUSTED = {
    "type": "deadline_exhausted",
    "payload": {
        "operation": "shell_run",
        "stop_reason": "run_budget_exhausted",
        "deadline": {
            "duration_observations": {
                "dispatch_overhead_seconds": {"count": 3, "total": 1.2, "max": 0.6}
            }
        },
    },
}

RUN_FINISHED_CLEAN = {
    "event": "run_finished",
    "status": "deadline_exhausted",
    "stop_reason": "run_budget_exhausted",
}


def _make_bundle(*, session=None, crash=None, extra_texts=None):
    """Build an ArtifactBundle from synthetic records via a temp dir."""
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    if session is not None:
        _write_jsonl(root / "sessions" / "sess-1.jsonl", session)
    if crash is not None:
        _write_jsonl(root / "alysis-crash.jsonl", crash)
    for name, content in (extra_texts or {}).items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return rs.ArtifactBundle.from_dir(root)


# ---------------------------------------------------------------------------
# Parsing / bundle
# ---------------------------------------------------------------------------


class TestArtifactBundle(unittest.TestCase):
    def test_from_dir_parses_session_and_crash(self):
        bundle = _make_bundle(
            session=[CLEAN_BUILD_SNAPSHOT, BUDGET_FINAL],
            crash=[RUN_FINISHED_CLEAN],
        )
        self.assertEqual(len(bundle.events), 3)
        self.assertTrue(bundle.texts)
        self.assertEqual(len(bundle.events_of_type("config_snapshot")), 1)
        self.assertEqual(len(bundle.events_of_type("run_finished")), 1)

    def test_missing_dir_is_empty_bundle(self):
        bundle = rs.ArtifactBundle.from_dir(Path(tempfile.mkdtemp()) / "nope")
        self.assertEqual(bundle.events, [])
        self.assertEqual(bundle.texts, [])

    def test_malformed_lines_are_skipped(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "s.jsonl").write_text('{"type":"ok","payload":{}}\nnot json\n\n', encoding="utf-8")
        bundle = rs.ArtifactBundle.from_dir(tmp)
        self.assertEqual(len(bundle.events), 1)

    def test_deep_find_key_nested(self):
        found = list(rs.deep_find_key(DEADLINE_EXHAUSTED, "dispatch_overhead_seconds"))
        self.assertEqual(len(found), 1)
        self.assertIn("count", found[0])

    def test_config_snapshot_build_lookup(self):
        bundle = _make_bundle(session=[CLEAN_BUILD_SNAPSHOT])
        build = bundle.config_snapshot_build()
        self.assertIsNotNone(build)
        self.assertTrue(build["clean"])


# ---------------------------------------------------------------------------
# compile-compcert (PR2/PR3)
# ---------------------------------------------------------------------------


class TestCompileCompcert(unittest.TestCase):
    def test_clean_budget_stop_all_pass(self):
        bundle = _make_bundle(
            session=[CLEAN_BUILD_SNAPSHOT, DEADLINE_EXHAUSTED, BUDGET_FINAL],
            crash=[RUN_FINISHED_CLEAN],
        )
        report = rs.evaluate_compile_compcert(bundle)
        by = {c.name: c for c in report.checks}
        self.assertEqual(by["pr2_no_nonzero_exit_error"].status, rs.PASS)
        self.assertEqual(by["pr2_clean_budget_stop"].status, rs.PASS)
        self.assertEqual(by["pr3_dispatch_overhead_present"].status, rs.PASS)
        self.assertFalse(report.failed)

    def test_nonzero_exit_error_fails(self):
        bundle = _make_bundle(
            session=[DEADLINE_EXHAUSTED],
            extra_texts={"harbor/run.log": "raise NonZeroAgentExitCodeError(exit_code=1)"},
        )
        report = rs.evaluate_compile_compcert(bundle)
        by = {c.name: c for c in report.checks}
        self.assertEqual(by["pr2_no_nonzero_exit_error"].status, rs.FAIL)
        self.assertTrue(report.failed)

    def test_budget_stop_with_terminal_error_fails(self):
        bundle = _make_bundle(
            session=[BUDGET_FINAL, {"type": "terminal_error", "payload": {"error_type": "X"}}]
        )
        report = rs.evaluate_compile_compcert(bundle)
        by = {c.name: c for c in report.checks}
        self.assertEqual(by["pr2_clean_budget_stop"].status, rs.FAIL)

    def test_no_budget_stop_is_skip(self):
        bundle = _make_bundle(session=[{"type": "final", "payload": {"content": "done"}}])
        report = rs.evaluate_compile_compcert(bundle)
        by = {c.name: c for c in report.checks}
        self.assertEqual(by["pr2_clean_budget_stop"].status, rs.SKIP)

    def test_missing_dispatch_overhead_fails(self):
        bundle = _make_bundle(session=[BUDGET_FINAL])
        report = rs.evaluate_compile_compcert(bundle)
        by = {c.name: c for c in report.checks}
        self.assertEqual(by["pr3_dispatch_overhead_present"].status, rs.FAIL)


# ---------------------------------------------------------------------------
# Service tasks (PR4)
# ---------------------------------------------------------------------------


class TestServiceTasks(unittest.TestCase):
    def test_persist_start_with_liveness_passes(self):
        service_start = {
            "type": "service_start",
            "payload": {
                "persist": True,
                "pid_alive": True,
                "liveness": "pid alive; listening on :8000",
                "probe_port": 8000,
                "port_listening": True,
            },
        }
        bundle = _make_bundle(session=[service_start])
        report = rs.evaluate_service_task(bundle, "hf-model-inference")
        by = {c.name: c for c in report.checks}
        self.assertEqual(by["pr4_service_liveness_recorded"].status, rs.PASS)
        self.assertEqual(by["pr4_dead_service_notice_or_alive"].status, rs.PASS)
        self.assertFalse(report.failed)

    def test_dead_service_notice_text_passes(self):
        bundle = _make_bundle(
            session=[{"type": "assistant_message", "payload": {"content": "ok"}}],
            extra_texts={
                "runtime/transcript.txt": (
                    "Service check: process started as a persistent service is no "
                    "longer running: myserver (pid 42). Restart it or note why it is "
                    "not needed."
                )
            },
        )
        report = rs.evaluate_service_task(bundle, "configure-git-webserver")
        by = {c.name: c for c in report.checks}
        self.assertEqual(by["pr4_dead_service_notice_or_alive"].status, rs.PASS)

    def test_no_persist_no_notice_is_skip(self):
        bundle = _make_bundle(session=[{"type": "final", "payload": {"content": "done"}}])
        report = rs.evaluate_service_task(bundle, "qemu-alpine-ssh")
        self.assertEqual(report.checks[0].name, "pr4_service_persistence_signal")
        self.assertEqual(report.checks[0].status, rs.SKIP)
        self.assertFalse(report.failed)


# ---------------------------------------------------------------------------
# filter-js-from-html + gcode-to-text (PR5)
# ---------------------------------------------------------------------------


class TestEditDisciplineTasks(unittest.TestCase):
    def test_rewrite_warning_present_passes(self):
        bundle = _make_bundle(
            session=[
                {
                    "type": "assistant_message",
                    "payload": {"content": "warning: full-file rewrite of index.html: 91% ..."},
                }
            ]
        )
        report = rs.evaluate_filter_js(bundle)
        self.assertEqual(report.checks[0].status, rs.PASS)

    def test_no_warning_with_reward_passes(self):
        bundle = _make_bundle(session=[{"type": "final", "payload": {"content": "done"}}])
        report = rs.evaluate_filter_js(bundle, reward=1.0)
        self.assertEqual(report.checks[0].status, rs.PASS)

    def test_no_warning_no_reward_is_warn(self):
        bundle = _make_bundle(session=[{"type": "final", "payload": {"content": "done"}}])
        report = rs.evaluate_filter_js(bundle, reward=None)
        self.assertEqual(report.checks[0].status, rs.WARN)
        self.assertFalse(report.failed)

    def test_gcode_thrash_notice_wired(self):
        bundle = _make_bundle(
            session=[{"type": "final", "payload": {"content": "done"}}],
            extra_texts={
                "t.txt": "Progress check: 8 similar attempts on analyze_final without a passing result."
            },
        )
        report = rs.evaluate_gcode(bundle)
        by = {c.name: c for c in report.checks}
        self.assertEqual(by["pr5_thrash_guard_wired"].status, rs.PASS)
        self.assertEqual(by["pr5_reached_terminal_state"].status, rs.PASS)


# ---------------------------------------------------------------------------
# Cross-cutting: canary (PR1) + build identity (PR6)
# ---------------------------------------------------------------------------


class TestCrossCutting(unittest.TestCase):
    def test_canary_cleartext_fails(self):
        secret = "sk-super-secret-abc123def456"
        bundle = _make_bundle(
            session=[CLEAN_BUILD_SNAPSHOT],
            extra_texts={"leak.log": f"Authorization: Bearer {secret}"},
        )
        check = rs.check_no_canary_cleartext(bundle, secret)
        self.assertEqual(check.status, rs.FAIL)

    def test_canary_absent_passes(self):
        bundle = _make_bundle(
            session=[CLEAN_BUILD_SNAPSHOT],
            extra_texts={"clean.log": "Authorization: «redacted:ALYSIS_API_KEY»"},
        )
        check = rs.check_no_canary_cleartext(bundle, "sk-never-appears-here")
        self.assertEqual(check.status, rs.PASS)
        self.assertIn("markers present", check.detail)

    def test_canary_no_value_is_skip(self):
        bundle = _make_bundle(session=[CLEAN_BUILD_SNAPSHOT])
        check = rs.check_no_canary_cleartext(bundle, None)
        self.assertEqual(check.status, rs.SKIP)

    def test_build_identity_clean_passes(self):
        bundle = _make_bundle(session=[CLEAN_BUILD_SNAPSHOT])
        check = rs.check_build_identity_clean(bundle)
        self.assertEqual(check.status, rs.PASS)

    def test_build_identity_dirty_fails(self):
        bundle = _make_bundle(session=[DIRTY_BUILD_SNAPSHOT])
        check = rs.check_build_identity_clean(bundle)
        self.assertEqual(check.status, rs.FAIL)

    def test_build_identity_missing_fails(self):
        bundle = _make_bundle(session=[BUDGET_FINAL])
        check = rs.check_build_identity_clean(bundle)
        self.assertEqual(check.status, rs.FAIL)

    def test_build_clean_derived_from_components(self):
        snapshot = {
            "type": "config_snapshot",
            "payload": {"build": {"commit": "b" * 12, "dirty": False, "identifiable": True}},
        }
        bundle = _make_bundle(session=[snapshot])
        check = rs.check_build_identity_clean(bundle)
        self.assertEqual(check.status, rs.PASS)


# ---------------------------------------------------------------------------
# Integration: evaluate_task + report aggregation
# ---------------------------------------------------------------------------


class TestEvaluateTaskIntegration(unittest.TestCase):
    def test_full_pass_run_exit_zero(self):
        bundle = _make_bundle(
            session=[CLEAN_BUILD_SNAPSHOT, DEADLINE_EXHAUSTED, BUDGET_FINAL],
            crash=[RUN_FINISHED_CLEAN],
        )
        report = rs.evaluate_task("compile-compcert", bundle, canary_value="sk-not-present")
        self.assertFalse(report.failed)
        self.assertFalse(rs.any_failed([report]))
        # Cross-cutting checks were appended.
        names = {c.name for c in report.checks}
        self.assertIn("pr1_canary_no_cleartext", names)
        self.assertIn("pr6_build_identity_clean", names)

    def test_dirty_build_makes_task_fail(self):
        bundle = _make_bundle(session=[DIRTY_BUILD_SNAPSHOT, BUDGET_FINAL, DEADLINE_EXHAUSTED])
        report = rs.evaluate_task("compile-compcert", bundle, canary_value="x")
        self.assertTrue(report.failed)
        self.assertTrue(rs.any_failed([report]))

    def test_format_report_renders_all(self):
        bundle = _make_bundle(session=[CLEAN_BUILD_SNAPSHOT])
        reports = [rs.evaluate_task(t, bundle, canary_value=None) for t in rs.REGRESSION_TASKS]
        text = rs.format_report(reports)
        for task in rs.REGRESSION_TASKS:
            self.assertIn(task, text)
        self.assertIn("TOTAL", text)

    def test_task_list_is_exactly_seven(self):
        self.assertEqual(len(rs.REGRESSION_TASKS), 7)
        self.assertIn("compile-compcert", rs.REGRESSION_TASKS)
        self.assertIn("gcode-to-text", rs.REGRESSION_TASKS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
