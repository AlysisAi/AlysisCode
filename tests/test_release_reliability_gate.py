from __future__ import annotations

import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _controlled_qualification(tmp_path, monkeypatch):
    from scripts.qa import qualify_production_reliability as qualification

    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (tests / "test_candidate.py").write_text("def test_candidate(): pass\n")
    adapters = project / "scripts/benchmarks/terminal_bench"
    adapters.mkdir(parents=True)
    (adapters / "__init__.py").write_text("")
    monkeypatch.setattr(qualification, "ROOT", project)
    monkeypatch.setattr(qualification, "RELIABILITY_TESTS", ("test_candidate.py",))
    monkeypatch.setattr(qualification, "SAFETY_TESTS", ("test_candidate.py::test_candidate",))
    monkeypatch.setattr(qualification, "INSTALLED_TESTS", ("test_candidate.py",))
    monkeypatch.setattr(qualification, "_git", lambda *_: "0")
    monkeypatch.setattr(qualification.shutil, "which", lambda _: "controlled-uv")
    monkeypatch.setattr(qualification.platform, "platform", lambda: "controlled-platform")
    monkeypatch.setattr(
        qualification,
        "_source_identity",
        lambda: {"commit": "a" * 40, "dirty": False, "source_sha256": "b" * 64},
    )
    return qualification


def test_source_suite_budget_is_scoped_and_dispatched_with_unchanged_stage_limits(
    tmp_path, monkeypatch
):
    qualification = _controlled_qualification(tmp_path, monkeypatch)
    output = tmp_path / "qualification"
    calls = []

    class Process:
        def __init__(self, command, **_kwargs):
            self.command = command
            if command[1:4] == ["-m", "hatchling", "build"]:
                dist = output / "dist"
                dist.mkdir()
                (dist / "controlled.whl").write_bytes(b"controlled process boundary")

        def wait(self, timeout):
            active = json.loads((output / "manifest.json").read_text())["active_stage"]
            assert active["timeout_seconds"] == timeout
            calls.append((active["name"], timeout, self.command))
            return 0

    monkeypatch.setattr(qualification.subprocess, "Popen", Process)
    arguments = ["--require-clean", "--full", "--output-dir", str(output)]
    if os.name == "nt":
        arguments.append("--windows-managed-install")
    assert qualification.main(arguments) == 0
    expected = [
        ("source-reliability", 1800),
        ("source-safety", 900),
        ("source-full", 3600),
        ("build-wheel", 900),
        ("install-wheel", 900),
    ]
    if os.name == "nt":
        expected.append(("compile-installed-bytecode", 300))
    expected.append(("installed-faults", 180))
    assert [(name, timeout) for name, timeout, _ in calls] == expected
    for index, selectors in enumerate(
        (qualification.RELIABILITY_TESTS, qualification.SAFETY_TESTS)
    ):
        command = calls[index][2]
        assert command[1:8] == ["-m", "pytest", "-q", "-x", "--tb=short", "-p", "no:cacheprovider"]
        assert command[-len(selectors) :] == [
            str(qualification.ROOT / "tests" / name) for name in selectors
        ]
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "passed"
    assert "active_stage" not in manifest
    assert [(row["name"], row["timeout_seconds"]) for row in manifest["stages"]] == expected


def test_source_timeout_settles_owned_process_and_blocks_all_later_stages(tmp_path, monkeypatch):
    qualification = _controlled_qualification(tmp_path, monkeypatch)
    output = tmp_path / "qualification"
    processes = []
    cleaned = []

    class Process:
        def __init__(self, command, **_kwargs):
            self.command = command
            self.waits = []
            processes.append(self)

        def wait(self, timeout):
            self.waits.append(timeout)
            if len(self.waits) == 1:
                raise subprocess.TimeoutExpired(self.command, timeout)
            assert cleaned == [self]
            return 15

    monkeypatch.setattr(qualification.subprocess, "Popen", Process)
    monkeypatch.setattr(qualification, "_stop_owned_tree", cleaned.append)
    assert qualification.main(["--require-clean", "--output-dir", str(output)]) == 1
    assert len(processes) == 1
    assert processes[0].waits == [1800, 10]
    assert cleaned == processes
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert len(manifest["stages"]) == 1
    stage = manifest["stages"][0]
    assert stage["name"] == "source-reliability"
    assert stage["timed_out"] is True
    assert stage["timeout_seconds"] == 1800
    assert stage["returncode"] == 15
    assert not (output / "dist").exists()


def test_source_failure_reports_before_later_cases_and_blocks_qualification(tmp_path, monkeypatch):
    from scripts.qa import qualify_production_reliability as qualification

    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    marker = project / "later-case-ran"
    (tests / "test_candidate.py").write_text(
        "from pathlib import Path\n"
        "def test_first_failure():\n"
        "    assert False, 'synthetic qualification failure'\n"
        "def test_later_case():\n"
        f"    Path({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(qualification, "ROOT", project)
    monkeypatch.setattr(qualification, "RELIABILITY_TESTS", ("test_candidate.py",))
    monkeypatch.setattr(
        qualification, "_source_identity", lambda: {"commit": "a" * 40, "dirty": False}
    )
    output = tmp_path / "qualification"

    assert qualification.main(["--require-clean", "--output-dir", str(output)]) == 1

    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert len(manifest["stages"]) == 1
    stage = manifest["stages"][0]
    assert stage["name"] == "source-reliability"
    assert stage["returncode"] == 1
    assert stage["timed_out"] is False
    assert "synthetic qualification failure" in (output / stage["log"]).read_text()
    assert not marker.exists()
    cases = list(ET.parse(output / "source-reliability.xml").iter("testcase"))
    assert len(cases) == 1
    assert cases[0].find("failure") is not None
    assert not (output / "dist").exists()


def test_qualification_inventory_references_existing_test_files():
    from scripts.qa.qualify_production_reliability import (
        INSTALLED_TESTS,
        RELIABILITY_TESTS,
        SAFETY_TESTS,
    )

    for entry in (*RELIABILITY_TESTS, *SAFETY_TESTS, *INSTALLED_TESTS):
        assert (ROOT / "tests" / entry.split("::", 1)[0]).is_file(), entry
    assert "test_objective_removed.py" in RELIABILITY_TESTS
    assert "test_task_evidence_lifecycle.py" in RELIABILITY_TESTS


def test_tag_publication_requires_successful_exact_source_platform_qualification():
    """A green package smoke job in another workflow cannot authorize publication."""
    for name in ("ci.yml", "release.yml"):
        policy = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        bounded_gate = policy["jobs"]["reliability-qualification"]
        assert bounded_gate["timeout-minutes"] == 40
        commands = [step.get("run", "") for step in bounded_gate["steps"]]
        assert any(
            "scripts/qa/qualify_production_reliability.py" in command
            and "--require-clean" in command
            for command in commands
        )
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    jobs = workflow["jobs"]
    gate = jobs["reliability-qualification"]
    assert gate["needs"] == "validate-release-source"
    assert set(gate["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest",
        "windows-latest",
        "macos-15",
    }
    assert not gate.get("continue-on-error", False)
    checkout = next(step for step in gate["steps"] if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    qualification = next(
        step
        for step in gate["steps"]
        if "scripts/qa/qualify_production_reliability.py" in step.get("run", "")
    )
    assert "--require-clean" in qualification["run"]
    assert "--frozen --no-sync" in qualification["run"]
    assert not qualification.get("continue-on-error", False)
    assert "if" not in qualification
    assert "if" not in gate

    def dependencies(job_id):
        needs = jobs[job_id].get("needs", [])
        return [needs] if isinstance(needs, str) else needs

    def ancestors(job_id):
        direct = dependencies(job_id)
        return set(direct).union(*(ancestors(item) for item in direct))

    for publisher in ("publish-pypi", "github-release"):
        assert "reliability-qualification" in ancestors(publisher)
        # No status override may let a failed/skipped required job publish.
        for job_id in {publisher, *ancestors(publisher)}:
            assert "if" not in jobs[job_id]
            assert not jobs[job_id].get("continue-on-error", False)
