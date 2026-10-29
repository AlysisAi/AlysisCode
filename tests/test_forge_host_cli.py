from __future__ import annotations

import json
from pathlib import Path

import pytest

from alysis_code.forge import RunPaths
from alysis_code.forge_host_cli import (
    ForgeHostCliError,
    ForgeHostResumeManifest,
    load_resume_manifest,
    write_resume_manifest,
)
from alysis_code.forge_host_protocol.codec import validate_envelope
from alysis_code.forge_host_protocol.models import EventEnvelope, PlanReady

FIXTURES = Path(__file__).parents[1] / "schemas" / "forge-host-v2" / "fixtures" / "valid"


def _ready() -> PlanReady:
    event = validate_envelope((FIXTURES / "plan-ready-event.json").read_bytes())
    assert isinstance(event, EventEnvelope)
    assert isinstance(event.payload, PlanReady)
    return event.payload


def _paths(tmp_path: Path) -> RunPaths:
    plan_dir = tmp_path / "run" / "plan"
    plan_dir.mkdir(parents=True)
    values = {
        field: tmp_path / field
        for field in RunPaths.__dataclass_fields__
        if field not in {"root", "run_id", "plan_dir"}
    }
    values.update(root=tmp_path, run_id="run_abcdefgh", plan_dir=plan_dir)
    return RunPaths(**values)


def test_resume_manifest_roundtrips_only_non_secret_authority(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    ready = _ready()
    manifest = ForgeHostResumeManifest.from_plan_ready(
        paths=paths,
        planner_input="Build the scoped feature.",
        plan_ready=ready,
        intent_kind="build",
        verification_commands=("pytest -q",),
        declared_scope_kinds={"src/app.py": "file"},
    )

    target = write_resume_manifest(paths, manifest)
    loaded = load_resume_manifest(paths)

    assert loaded == manifest
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema"] == "alysis.forge-host.local-resume/v1"
    assert "provider" not in json.dumps(payload).lower()
    assert "credential" not in json.dumps(payload).lower()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra="forbidden"),
        lambda value: value.update(nativeRunId="../foreign"),
        lambda value: value.update(verificationCommands=["rm -rf vendor"]),
        lambda value: value.update(hostPlanFingerprint="0" * 63),
        lambda value: value.update(declaredScopeKinds={"src/app.py": "unknown"}),
    ],
)
def test_resume_manifest_rejects_noncanonical_or_widened_records(
    tmp_path: Path,
    mutation,
) -> None:  # type: ignore[no-untyped-def]
    paths = _paths(tmp_path)
    ready = _ready()
    manifest = ForgeHostResumeManifest.from_plan_ready(
        paths=paths,
        planner_input="Build it.",
        plan_ready=ready,
        intent_kind=None,
        verification_commands=("pytest -q",),
        declared_scope_kinds={},
    )
    payload = manifest.to_payload()
    mutation(payload)
    target = paths.plan_dir / "forge-host-v2-resume.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ForgeHostCliError, match="invalid|match"):
        load_resume_manifest(paths)


def test_resume_manifest_rejects_symlink(tmp_path: Path, require_symlinks: None) -> None:
    paths = _paths(tmp_path)
    foreign = tmp_path / "foreign.json"
    foreign.write_text("{}", encoding="utf-8")
    (paths.plan_dir / "forge-host-v2-resume.json").symlink_to(foreign)

    with pytest.raises(ForgeHostCliError, match="invalid"):
        load_resume_manifest(paths)
