from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from alysis_code.windows_mxc_contract import helper_contract
from scripts.release.prepare_windows_sandbox_signature import validate_candidate


@pytest.fixture
def candidate(tmp_path):
    inventory = {"schema_version": 1, "source_commit": "a" * 40, "files": {"native.rs": "b" * 64}}
    source = json.dumps(inventory).encode()
    binary = b"test unsigned build output"
    manifest = {
        **helper_contract(),
        "platform": "win32-x64",
        "source_commit": inventory["source_commit"],
        "source_inventory_sha256": hashlib.sha256(source).hexdigest(),
        "sha256": hashlib.sha256(binary).hexdigest(),
    }
    for name, data in {
        "SOURCE.json": source,
        "alysis-sandbox.exe": binary,
        "manifest.json": json.dumps(manifest).encode(),
        "LICENSE.mxc": b"test license",
    }.items():
        (tmp_path / name).write_bytes(data)
    return tmp_path, inventory


def test_unsigned_bytes_and_source_must_match_before_signing(candidate):
    bundle, inventory = candidate
    original = validate_candidate(bundle, inventory)
    assert original == json.loads((bundle / "manifest.json").read_bytes())
    (bundle / "alysis-sandbox.exe").write_bytes(b"replaced executable")
    with pytest.raises(ValueError, match="Unsigned candidate differs"):
        validate_candidate(bundle, inventory)


@pytest.mark.parametrize(
    "field", ["source_commit", "source_inventory_sha256", "platform", "security_revision"]
)
def test_candidate_rejects_provenance_and_contract_drift(candidate, field):
    bundle, inventory = candidate
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = "changed"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        validate_candidate(bundle, inventory)


def signature_for(bundle):
    return {
        "schemaVersion": 2,
        "status": "verified",
        "kind": "authenticode",
        "target": "win32-x64",
        "executable": "alysis-sandbox.exe",
        "executableSha256": hashlib.sha256(
            (bundle / "alysis-sandbox.exe").read_bytes()
        ).hexdigest(),
        "signerThumbprint": "c" * 40,
        "signerIdentity": "sha256:" + "d" * 64,
        "timestampSignerIdentity": "sha256:" + "e" * 64,
    }


def test_signed_digest_changes_only_after_matching_os_receipt(candidate):
    bundle, inventory = candidate
    original = validate_candidate(bundle, inventory)
    (bundle / "alysis-sandbox.exe").write_bytes(b"test signed build output")
    signature = signature_for(bundle)
    signed = validate_candidate(bundle, inventory, signature=signature)
    assert signed["sha256"] != original["sha256"]
    assert signed == {**original, "sha256": signature["executableSha256"]}


@pytest.mark.parametrize(
    "field", ["status", "target", "executableSha256", "signerThumbprint", "timestampSignerIdentity"]
)
def test_unverified_or_mismatched_signature_receipt_cannot_bind_binary(candidate, field):
    bundle, inventory = candidate
    signature = signature_for(bundle)
    signature[field] = "invalid"
    with pytest.raises(ValueError, match="Signature receipt"):
        validate_candidate(bundle, inventory, signature=signature)


def test_public_candidate_tag_requires_qualification_and_protected_signing_without_publication():
    root = Path(__file__).resolve().parents[1]
    native = yaml.safe_load((root / ".github/workflows/windows-native-sandbox.yml").read_text())
    release = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    # YAML 1.1 represents GitHub's "on" key as True.
    assert native[True]["push"]["tags"] == ["v-sandbox-*"]
    assert "!v-sandbox-*" in release[True]["push"]["tags"]
    job = native["jobs"]["sign-mxc-candidate"]
    assert job["environment"] == "managed-cli-native-signing"
    assert set(job["needs"]) == {"installed-qualification", "mxc-candidate-build"}
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert "AlysisAi/AlysisCode" in job["if"]
    steps = job["steps"]
    assert any("validate_github_environment_policy.py" in step.get("run", "") for step in steps)
    assert any("verify_windows_runtime.ps1" in step.get("run", "") for step in steps)
    assert any(step.get("uses", "").startswith("actions/upload-artifact@") for step in steps)
