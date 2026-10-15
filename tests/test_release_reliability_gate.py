from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


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
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    jobs = workflow["jobs"]
    gate = jobs["reliability-qualification"]
    assert gate["needs"] == "validate-release-source"
    assert set(gate["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest",
        "windows-latest",
        "macos-14",
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
