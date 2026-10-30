"""Synthetic runner failures must not hide later containment observations.

No sandbox helper, Git subprocess, provider, or installed runtime is executed.
"""

import base64
import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[1] / "scripts" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qualifier = load_script("qualify_contained_file_tools")
candidate = load_script("qualify_windows_coding_candidate")


class SyntheticStaleError(Exception):
    pass


class SyntheticWorker:
    def __init__(self, root, *, failures=(), corrupt=()):
        self.root = root
        self.failures = failures
        self.corrupt = corrupt
        self.calls = []

    def attempt(self, name):
        self.calls.append(name)
        if name in self.failures:
            raise PermissionError("synthetic denied operation: " + name)

    def run(self, name, arguments):
        self.attempt(name)
        return {
            "paths": ["test_sample.py"],
            "matches": ["test_marker_function"],
            "subject": "Synthetic fixture",
        }

    def run_write(self, operation, arguments):
        if operation == "git_apply_patch":
            self.attempt("contained_git_patch")
            (self.root / "ordinary.txt").write_text("patched\n")
            return {"applied": True}
        if operation == "fs_write_prepared":
            path = self.root / arguments["path"]
            name = (
                "large_prepared_write"
                if arguments["path"] == "nested/large.txt"
                else "contained_root_overwrite"
            )
            self.attempt(name)
            if path.exists() != arguments["precondition"]["exists"]:
                self.attempt("stale_write_refused")
                raise SyntheticStaleError("synthetic stale fixture")
            created = not path.exists()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(arguments["content"] if name not in self.corrupt else "wrong content")
            return {"created": created}
        if operation == "fs_copy":
            self.attempt("contained_copy")
            shutil.copyfile(
                self.root / arguments["source_path"], self.root / arguments["destination_path"]
            )
            if "contained_copy" in self.corrupt:
                (self.root / arguments["destination_path"]).write_text("wrong content")
        elif operation == "fs_move":
            name = (
                "contained_move"
                if arguments["destination_path"] == "moved.txt"
                else "contained_cross_directory_move"
            )
            self.attempt(name)
            (self.root / arguments["source_path"]).rename(self.root / arguments["destination_path"])
        elif operation == "fs_delete":
            self.attempt("contained_delete")
            (self.root / arguments["path"]).unlink()
        elif operation == "fs_mkdir":
            self.attempt("contained_mkdir")
            (self.root / arguments["path"]).mkdir()
        elif operation == "image_create_bytes":
            self.attempt("image_bytes_publication")
            path = self.root / arguments["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(arguments["payload"]))
            return {"created": True}
        else:
            raise AssertionError("unexpected operation: " + operation)
        return {}


def exercise(
    tmp_path,
    *,
    failures=(),
    corrupt=(),
    leak=False,
    tools_fail=False,
    cleanup_fail=False,
    stamp_fail=False,
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git/config").write_text("synthetic metadata")
    (root / "ordinary.txt").write_text("old\n")
    (root / "read-sentinel.txt").write_text("READ_TOOL_FIXTURE\n")
    canary = tmp_path / "canary.txt"
    canary.write_text(qualifier.PRIVATE_CANARY)
    worker = SyntheticWorker(root, failures=failures, corrupt=corrupt)
    report = {"checks": [], "failures": [], "not_run": [], "passed": False}
    observations = qualifier.Observations(report)

    def read(arguments):
        if arguments["path"] == "linked-private.txt":
            return {"content": qualifier.PRIVATE_CANARY} if leak else {}
        return {"content": (root / arguments["path"]).read_text()}

    def write(arguments):
        worker.attempt("tool_fs_write")
        (root / arguments["path"]).write_text(arguments["content"])
        return {}

    def search(arguments):
        return (
            {}
            if arguments["pattern"] == "SYNTHETIC_PRIVATE"
            else {"matches": ["test_marker_function"]}
        )

    def tools_factory():
        if cleanup_fail:
            (root / ".alysis-file-request-leftover.json").write_text("{}")
        if tools_fail:
            raise RuntimeError("synthetic tool setup failed")
        return {
            "fs_read": SimpleNamespace(run=read),
            "fs_write": SimpleNamespace(run=write),
            "search_rg": SimpleNamespace(run=search),
        }

    def stamp(path):
        if stamp_fail and path == "nested/large.txt":
            raise PermissionError("synthetic precondition failure")
        return {"exists": (root / path).exists()}

    qualifier.run_file_checks(
        observations,
        root=root,
        canary=canary,
        worker=worker,
        tools_factory=tools_factory,
        stamp=stamp,
        stale_error=SyntheticStaleError,
    )
    exit_code = observations.finish()
    seen = report["checks"] + [f["check"] for f in report["failures"] + report["not_run"]]
    assert len(seen) == len(set(seen)) == 25
    assert set(seen) == set(qualifier.CHECK_NAMES)
    return report, exit_code, worker


def test_complete_success_keeps_existing_candidate_schema(tmp_path):
    report, exit_code, _ = exercise(tmp_path)
    assert exit_code == 0 and report["passed"] is True
    assert not report["failures"] and not report["not_run"]
    assert candidate.file_verdict(report, exit_code)["full_file_compatibility_passed"] is True


def test_write_failure_skips_only_stale_dependency_and_continues_privacy_cleanup(tmp_path):
    report, exit_code, worker = exercise(tmp_path, failures={"large_prepared_write"})
    assert exit_code == 1 and report["passed"] is False
    assert [f["check"] for f in report["failures"]] == ["large_prepared_write"]
    assert "PermissionError: synthetic denied" in report["failures"][0]["traceback"]
    assert report["not_run"] == [
        {
            "check": "stale_write_refused",
            "reason": "prerequisite did not pass",
            "blocked_by": ["large_prepared_write"],
        }
    ]
    assert worker.calls.count("large_prepared_write") == 1
    assert {
        "contained_copy",
        "contained_root_overwrite",
        "contained_move",
        "contained_cross_directory_move",
        "contained_delete",
        "private_hardlink_not_disclosed",
        "request_packets_removed",
        "private_canary_unchanged",
    } <= set(report["checks"])
    assert candidate.file_verdict(report, exit_code)["passed"] is False


def test_precondition_failure_is_collected_with_the_write_and_does_not_abort(tmp_path):
    report, exit_code, worker = exercise(tmp_path, stamp_fail=True)
    assert exit_code == 1
    assert [f["check"] for f in report["failures"]] == ["large_prepared_write"]
    assert "synthetic precondition failure" in report["failures"][0]["traceback"]
    assert "large_prepared_write" not in worker.calls
    assert [n["check"] for n in report["not_run"]] == ["stale_write_refused"]
    assert {
        "contained_copy",
        "private_hardlink_not_disclosed",
        "request_packets_removed",
        "private_canary_unchanged",
    } <= set(report["checks"])


def test_unexpected_stale_write_error_is_a_failure_and_later_checks_continue(tmp_path):
    report, exit_code, _ = exercise(tmp_path, failures={"stale_write_refused"})
    assert exit_code == 1
    assert [f["check"] for f in report["failures"]] == ["stale_write_refused"]
    assert "PermissionError: synthetic denied" in report["failures"][0]["traceback"]
    assert not report["not_run"]
    assert {
        "large_prepared_write",
        "contained_copy",
        "private_hardlink_not_disclosed",
        "request_packets_removed",
        "private_canary_unchanged",
    } <= set(report["checks"])


@pytest.mark.parametrize(
    "name",
    [
        "contained_git_patch",
        "contained_copy",
        "contained_root_overwrite",
        "contained_move",
        "contained_cross_directory_move",
        "contained_delete",
        "contained_mkdir",
        "tool_fs_write",
    ],
)
def test_each_mutation_error_stays_failed_without_hiding_later_checks(tmp_path, name):
    report, exit_code, _ = exercise(tmp_path, failures={name})
    assert exit_code == 1
    assert [f["check"] for f in report["failures"]] == [name]
    assert name not in report["checks"]
    assert not report["not_run"]
    assert {
        "private_hardlink_not_disclosed",
        "request_packets_removed",
        "private_canary_unchanged",
    } <= set(report["checks"])
    assert candidate.file_verdict(report, exit_code)["passed"] is False


@pytest.mark.parametrize(
    "name", ["large_prepared_write", "contained_copy", "contained_root_overwrite"]
)
def test_content_assertions_are_part_of_the_check_and_cannot_leave_false_pass(tmp_path, name):
    report, exit_code, _ = exercise(tmp_path, corrupt={name})
    assert exit_code == 1
    assert [f["check"] for f in report["failures"]] == [name]
    assert name not in report["checks"]
    assert "private_hardlink_not_disclosed" in report["checks"]


def test_earlier_write_failure_does_not_hide_privacy_or_cleanup_failures(tmp_path):
    report, exit_code, _ = exercise(
        tmp_path, failures={"large_prepared_write"}, leak=True, cleanup_fail=True
    )
    assert exit_code == 1
    assert [f["check"] for f in report["failures"]] == [
        "large_prepared_write",
        "private_hardlink_not_disclosed",
        "request_packets_removed",
    ]
    assert "private_canary_unchanged" in report["checks"]
    assert not (tmp_path / "workspace/linked-private.txt").exists()


def test_tool_setup_failure_marks_dependent_checks_not_run_and_still_checks_cleanup(tmp_path):
    report, exit_code, _ = exercise(tmp_path, tools_fail=True)
    assert exit_code == 1 and report["passed"] is False
    assert not report["failures"]
    assert {n["check"] for n in report["not_run"]} == {
        "tool_fs_read",
        "tool_fs_write",
        "tool_search",
        "private_hardlink_not_disclosed",
    }
    assert all(n["blocked_by"] == ["tools"] for n in report["not_run"])
    assert "synthetic tool setup failed" in report["fixture_failures"][0]["traceback"]
    assert {"request_packets_removed", "private_canary_unchanged"} <= set(report["checks"])
    assert candidate.file_verdict(report, exit_code)["passed"] is False


def test_unexecuted_checks_cannot_pass_and_duplicate_observations_are_rejected():
    report = {"checks": [], "failures": [], "not_run": [], "passed": False}
    observations = qualifier.Observations(report)
    observations.check("fs_list", lambda: None)
    with pytest.raises(ValueError, match="duplicate"):
        observations.check("fs_list", lambda: None)
    assert observations.finish() == 1
    assert len(report["not_run"]) == 24
    assert report["passed"] is False
    assert candidate.file_verdict(report, 1)["passed"] is False
