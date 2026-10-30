"""The narrower release scope must not hide mutation, privacy or missing results."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "coding_candidate", Path(__file__).parents[1] / "scripts/qualify_windows_coding_candidate.py"
)
qualifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualifier)


def accepted():
    return {
        "checks": sorted(qualifier.REQUIRED_FILES),
        "passed": False,
        "failures": [
            {
                "check": name,
                "traceback": "alysis_code.contained_read_tools.ContainedGitDirectoryError: git_directory_access_denied: original Git error",
            }
            for name in sorted(qualifier.READONLY_GIT)
        ],
    }


def test_known_readonly_limitations_remain_separate_from_full_compatibility():
    source = accepted()
    before = copy.deepcopy(source)
    verdict = qualifier.file_verdict(source, 1)
    assert verdict["passed"] is True and verdict["full_file_compatibility_passed"] is False
    assert len(verdict["known_readonly_limitations"]) == 3 and source == before


def test_future_full_compatibility_requires_all_observations_and_zero_exit():
    source = {
        "checks": sorted(qualifier.REQUIRED_FILES | qualifier.READONLY_GIT),
        "failures": [],
        "passed": True,
    }
    assert qualifier.file_verdict(source, 0)["full_file_compatibility_passed"] is True
    assert qualifier.file_verdict(source, 1)["passed"] is False


@pytest.mark.parametrize(
    "kind",
    [
        "patch",
        "privacy",
        "missing",
        "duplicate",
        "unknown",
        "generic_git_error",
        "fatal",
        "wrong_exit",
        "malformed",
        "not_run",
        "fixture_failure",
    ],
)
def test_required_failures_and_incomplete_evidence_cannot_pass(kind):
    source = accepted()
    exit_code = 1
    if kind in {"patch", "privacy"}:
        name = "contained_git_patch" if kind == "patch" else "private_hardlink_not_disclosed"
        source["checks"].remove(name)
        source["failures"].append({"check": name, "traceback": source["failures"][0]["traceback"]})
    elif kind == "missing":
        source["checks"].pop()
    elif kind == "duplicate":
        source["checks"].append(source["checks"][0])
    elif kind == "unknown":
        source["checks"].append("invented")
    elif kind == "generic_git_error":
        source["failures"][0]["traceback"] = "ConfigError: unexpected error"
    elif kind == "fatal":
        source["failure"] = "fixture aborted"
    elif kind == "wrong_exit":
        exit_code = 0
    elif kind == "not_run":
        source["not_run"] = [{"check": "large_prepared_write", "reason": "not observed"}]
    elif kind == "fixture_failure":
        source["fixture_failures"] = [{"fixture": "tools", "traceback": "synthetic"}]
    else:
        source["failures"][0]["check"] = []
    assert qualifier.file_verdict(source, exit_code)["passed"] is False


@pytest.fixture
def engine_update(tmp_path):
    kit, update = tmp_path / "base", tmp_path / "update"
    kit.mkdir()
    update.mkdir()
    (kit / "candidate.json").write_text("synthetic base identity")
    wheel = "wheels/alysis_code-0.14.2-py3-none-any.whl"
    paths = [
        wheel,
        "scripts/qualify_windows_coding_candidate.py",
        "scripts/qualify_contained_file_tools.py",
        "scripts/qualify_windows_git_recovery.py",
        "scripts/qualify_windows_mxc_network.py",
        "scripts/qualify_windows_mxc.py",
        "scripts/windows_mxc_network.py",
        "src/alysis_code/__init__.py",
        "src/alysis_code/windows_mxc_contract.py",
    ]
    for name in paths:
        path = update / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(
            Path(qualifier.__file__).read_bytes()
            if name.endswith("coding_candidate.py")
            else b"synthetic fixture"
        )
    identity = {
        "schema_version": 1,
        "base_candidate_sha256": qualifier.digest(kit / "candidate.json"),
        "engine_source": "a" * 40,
        "wheel": wheel,
        "files": {name: qualifier.digest(update / name) for name in paths},
    }
    (update / "engine-update.json").write_text(json.dumps(identity))
    return kit, update, identity


def test_engine_followup_verifies_identity_without_mutating_base(engine_update):
    kit, update, identity = engine_update
    before = (kit / "candidate.json").read_bytes()
    actual, wheel = qualifier.verify_engine_update(kit, update)
    assert actual == identity and wheel == update / identity["wheel"]
    assert (kit / "candidate.json").read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["base", "wheel", "script", "missing", "escape", "source", "entrypoint"]
)
def test_engine_followup_refuses_unbound_inputs(engine_update, fault):
    kit, update, identity = engine_update
    if fault == "base":
        (kit / "candidate.json").write_text("different base kit")
    elif fault in {"wheel", "script"}:
        name = identity["wheel"] if fault == "wheel" else "scripts/qualify_contained_file_tools.py"
        (update / name).write_bytes(b"changed payload")
    elif fault == "missing":
        identity["files"].pop("scripts/qualify_contained_file_tools.py")
    elif fault == "escape":
        identity["files"]["../base/candidate.json"] = qualifier.digest(kit / "candidate.json")
    elif fault == "source":
        identity["engine_source"] = "main"
    else:
        name = "scripts/qualify_windows_coding_candidate.py"
        (update / name).write_bytes(b"different entrypoint")
        identity["files"][name] = qualifier.digest(update / name)
    (update / "engine-update.json").write_text(json.dumps(identity))
    with pytest.raises(ValueError):
        qualifier.verify_engine_update(kit, update)
