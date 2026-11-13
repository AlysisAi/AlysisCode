"""Real source trees and rollback ordering; no cloud or production data changes."""

import hashlib
import json

import pytest

from alysis_code.bots.hosted_release import (
    MANIFEST,
    activate_release,
    prepare_release,
    qualify_environment,
    verify_release,
)


def release(path, *, schema=36, marker="previous", lock=b"same-dependencies"):
    files = {
        "src/alysis_code/bots/schema.py": f"SCHEMA_VERSION = {schema}\n".encode(),
        "src/alysis_code/bots/marker.py": marker.encode(),
        "scripts/bots-hosted/install.py": b"# reviewed installer\n",
        "pyproject.toml": b"[project]\nname='fixture'\n",
        "uv.lock": lock,
    }
    for name, raw in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    manifest = {
        "format": 1,
        "source_commit": hashlib.sha1(marker.encode()).hexdigest(),
        "schema": schema,
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    }
    (path / MANIFEST).write_text(json.dumps(manifest))
    environment = path / ".venv/bin"
    environment.mkdir(parents=True)
    (environment / "python").write_bytes(b"qualified fixture environment")
    return manifest


def test_complete_manifest_refuses_a_missing_or_changed_delta_before_publication(tmp_path):
    base, overlay, candidate = (tmp_path / name for name in ("base", "overlay", "candidate"))
    release(base)
    manifest = release(overlay, marker="candidate")
    changed = overlay / "src/alysis_code/bots/marker.py"
    changed.write_bytes(b"corrupted source")
    with pytest.raises(ValueError, match="Incomplete"):
        prepare_release(base, candidate, manifest, overlay)
    assert not candidate.exists()
    changed.write_bytes(b"candidate")
    report = prepare_release(base, candidate, manifest, overlay)
    assert report["prepared"] and verify_release(candidate) == manifest
    assert (candidate / ".venv").resolve() == (base / ".venv").resolve()
    changed = candidate / "scripts/bots-hosted/install.py"
    changed.write_bytes(b"changed installer")
    with pytest.raises(ValueError, match="verification"):
        verify_release(candidate)


def test_symlink_members_and_dependency_changes_are_rejected(tmp_path):
    base, overlay, candidate = (tmp_path / name for name in ("base", "overlay", "candidate"))
    release(base)
    manifest = release(overlay, lock=b"different-dependencies")
    with pytest.raises(ValueError, match="Dependency"):
        prepare_release(base, candidate, manifest, overlay)
    assert not candidate.exists()
    manifest = release(tmp_path / "other")
    target = base / "src/alysis_code/bots/marker.py"
    target.rename(base / "original.py")
    target.symlink_to(base / "original.py")
    with pytest.raises(ValueError, match="symbolic"):
        verify_release(base)
    assert verify_release(tmp_path / "other") == manifest


@pytest.mark.parametrize("candidate_ready", [True, False])
def test_activation_and_rollback_require_actual_readiness_of_selected_release(
    tmp_path, candidate_ready
):
    previous, candidate = tmp_path / "previous", tmp_path / "candidate"
    old = release(previous)
    new = release(candidate, marker="candidate")
    events = []

    def install(path):
        events.append(("install", path.name))

    def ready(path):
        verify_release(path)
        events.append(("ready", path.name))
        return path == previous or candidate_ready

    result = activate_release(
        candidate,
        previous,
        schema=36,
        install=install,
        control=lambda action: events.append(("control", action)),
        ready=ready,
    )
    assert result["activated"] is candidate_ready
    assert result["rolled_back"] is not candidate_ready
    assert result["source_commit"] == (new if candidate_ready else old)["source_commit"]
    assert events[:4] == [
        ("control", "stop"),
        ("install", "candidate"),
        ("control", "start"),
        ("ready", "candidate"),
    ]
    if not candidate_ready:
        assert events[4:] == [
            ("control", "stop"),
            ("install", "previous"),
            ("control", "start"),
            ("ready", "previous"),
        ]


def test_schema_migration_is_not_misrepresented_as_a_reversible_code_update(tmp_path):
    previous, candidate = tmp_path / "previous", tmp_path / "candidate"
    release(previous, schema=35)
    release(candidate, schema=36)
    with pytest.raises(ValueError, match="current database schema"):
        activate_release(candidate, previous, schema=35, install=None, control=None, ready=None)


def test_failed_rollback_never_reports_success(tmp_path):
    previous, candidate = tmp_path / "previous", tmp_path / "candidate"
    release(previous)
    release(candidate, marker="candidate")
    with pytest.raises(RuntimeError, match="recovery remains unconfirmed"):
        activate_release(
            candidate,
            previous,
            schema=36,
            install=lambda _: None,
            control=lambda _: None,
            ready=lambda _: False,
        )


def test_existing_environment_qualification_checks_actual_required_versions():
    import sys

    project = (
        b"[project]\nname='fixture'\nrequires-python='>=3.11'\ndependencies=['packaging>=23']\n"
    )
    report = qualify_environment(sys.prefix, project)
    assert "packaging" in report["inventory"]["packages"]
    with pytest.raises(ValueError, match="unqualified"):
        qualify_environment(sys.prefix, project.replace(b"packaging>=23", b"packaging>=999"))


def test_explicit_qualification_records_reused_environment_instead_of_assuming_lock_install(
    tmp_path,
):
    import sys

    base, overlay, candidate = (tmp_path / name for name in ("base", "overlay", "candidate"))
    release(base)
    manifest = release(overlay, lock=b"new reviewed lock")
    result = prepare_release(base, candidate, manifest, overlay, qualified_environment=sys.prefix)
    assert result["prepared"]
    receipt = json.loads((candidate / "hosted-environment.json").read_text())
    assert receipt["candidate_lock_sha256"] == manifest["files"]["uv.lock"]
    assert receipt["inventory"]["python"] and receipt["inventory_sha256"]
