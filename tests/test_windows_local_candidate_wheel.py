from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from alysis_code.windows_mxc_candidate_builder import source_inventory
from alysis_code.windows_mxc_contract import helper_contract
from hatch_build import CustomBuildHook, load_source_module

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    for relative in (
        "hatch_build.py",
        "scripts/build_windows_mxc.py",
        "src/alysis_code/windows_mxc_candidate_builder.py",
        "src/alysis_code/windows_mxc_bundle.py",
        "src/alysis_code/windows_mxc_contract.py",
        "native/windows-sandbox/LICENSE.mxc",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    (root / "src/alysis_code/__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text("# synthetic candidate source\n", encoding="utf-8")
    (root / "uv.lock").write_text("# synthetic lock\n", encoding="utf-8")
    for arguments in (
        ["init", "--quiet"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    binary = bytearray(128)
    binary[:2] = b"MZ"
    binary[60:64] = (64).to_bytes(4, "little")
    binary[64:70] = b"PE\0\0\x64\x86"
    (bundle / "alysis-sandbox.exe").write_bytes(binary)
    digest = hashlib.sha256(binary).hexdigest()
    monkeypatch.setenv("ALYSIS_LOCAL_CANDIDATE", "1")
    monkeypatch.setenv("ALYSIS_WINDOWS_SANDBOX_BUNDLE", str(bundle))
    monkeypatch.setenv("ALYSIS_WINDOWS_SANDBOX_BUNDLE_SHA256", digest)

    def record():
        inventory = source_inventory(root)
        sources = json.dumps(inventory).encode()
        (bundle / "SOURCE.json").write_bytes(sources)
        manifest = {
            **helper_contract(),
            "platform": "win32-x64",
            "sha256": digest,
            "source_commit": inventory["source_commit"],
            "source_inventory_sha256": hashlib.sha256(sources).hexdigest(),
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    record()
    hook = CustomBuildHook(str(root), {}, None, None, "unused", "wheel")
    yield root, bundle, hook, record
    if hasattr(hook, "_candidate_temp"):
        hook._candidate_temp.cleanup()


def test_candidate_installs_verified_payload_and_preserves_source(candidate, tmp_path):
    root, bundle, hook, _ = candidate
    before = source_inventory(root)
    build_data = {}
    hook.initialize("standard", build_data)
    installed = tmp_path / "installed"
    package = installed / "alysis_code"
    package.mkdir(parents=True)
    shutil.copyfile(
        root / "src/alysis_code/windows_mxc_bundle.py", package / "windows_mxc_bundle.py"
    )
    for source, relative in build_data["force_include"].items():
        target = installed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    module = load_source_module("_installed_local_bundle", package / "windows_mxc_bundle.py")
    assert module.LOCAL_CANDIDATE and module.HELPER_SIGNER is None
    assert module.HELPER_SOURCE == before["source_commit"]
    assert module.verify_bundle().read_bytes() == (bundle / "alysis-sandbox.exe").read_bytes()
    assert source_inventory(root) == before
    module.verify_bundle().write_bytes(b"tampered after installation")
    with pytest.raises(ValueError, match="integrity"):
        module.verify_bundle()
    hook.finalize("standard", build_data, "unused")


@pytest.mark.parametrize("damage", ["binary", "contract", "inventory", "digest", "dirty"])
def test_candidate_rejects_tampered_or_uncommitted_build_inputs(candidate, monkeypatch, damage):
    root, bundle, hook, record = candidate
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if damage == "binary":
        with (bundle / "alysis-sandbox.exe").open("ab") as stream:
            stream.write(b"changed")
    elif damage == "contract":
        manifest["security_revision"] = 8
    elif damage == "inventory":
        inventory = json.loads((bundle / "SOURCE.json").read_text(encoding="utf-8"))
        del inventory["files"]["hatch_build.py"]
        sources = json.dumps(inventory).encode()
        (bundle / "SOURCE.json").write_bytes(sources)
        manifest["source_inventory_sha256"] = hashlib.sha256(sources).hexdigest()
    elif damage == "digest":
        monkeypatch.setenv("ALYSIS_WINDOWS_SANDBOX_BUNDLE_SHA256", "0" * 64)
    elif damage == "dirty":
        (root / "uv.lock").write_text("# changed before build\n", encoding="utf-8")
        record()
        with pytest.raises(ValueError, match="clean committed"):
            hook.initialize("standard", {})
        return
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        hook.initialize("standard", {})


def test_candidate_rechecks_source_after_wheel_build(candidate):
    root, _, hook, _ = candidate
    hook.initialize("standard", {})
    (root / "hatch_build.py").write_text("# changed during packaging", encoding="utf-8")
    with pytest.raises(ValueError, match="changed while building"):
        hook.finalize("standard", {}, "unused")


def _stamp_candidate(candidate):
    from alysis_code.build_identity import generate_build_info, render_build_info_module

    root, _, _, record = candidate
    path = root / "src/alysis_code/_build_info.py"
    path.write_text('BUILD_SOURCE = "dev-default"\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "default stamp",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    info = generate_build_info(repo_root=root, fetch=False)
    assert info.is_clean
    path.write_text(render_build_info_module(info), encoding="utf-8")
    record()
    return path


def test_candidate_accepts_the_official_current_commit_stamp_with_exact_inventory(candidate):
    root, _, hook, _ = candidate
    _stamp_candidate(candidate)
    before = source_inventory(root)
    hook.initialize("standard", {})
    assert source_inventory(root) == before
    hook.finalize("standard", {}, "unused")


@pytest.mark.parametrize(
    "damage", ["commit", "dirty", "executable", "duplicate", "untracked", "staged", "other_source"]
)
def test_candidate_stamp_exception_never_admits_other_changes(candidate, damage):
    root, _, hook, record = candidate
    path = _stamp_candidate(candidate)
    text = path.read_text(encoding="utf-8")
    if damage == "commit":
        text = text.replace('BUILD_COMMIT = "', 'BUILD_COMMIT = "stale-')
    elif damage == "dirty":
        text = text.replace("BUILD_DIRTY = False", "BUILD_DIRTY = True")
    elif damage == "executable":
        text += '\nraise RuntimeError("stamp code must never execute")\n'
    elif damage == "duplicate":
        text += '\nBUILD_SOURCE = "git"\n'
    elif damage == "untracked":
        (root / "untracked.py").write_text("# unreviewed\n", encoding="utf-8")
    elif damage == "staged":
        subprocess.run(["git", "add", str(path)], cwd=root, check=True, capture_output=True)
    elif damage == "other_source":
        (root / "uv.lock").write_text("# unreviewed\n", encoding="utf-8")
    if damage not in {"untracked", "staged", "other_source"}:
        path.write_text(text, encoding="utf-8")
    record()  # Even a matching inventory cannot bless uncommitted code.
    with pytest.raises(ValueError, match="clean committed|valid generated"):
        hook.initialize("standard", {})


@pytest.mark.parametrize("target,version", [("sdist", "standard"), ("wheel", "editable")])
def test_candidate_cannot_be_built_as_sdist_or_editable(candidate, target, version):
    root, _, _, _ = candidate
    hook = CustomBuildHook(str(root), {}, None, None, "unused", target)
    with pytest.raises(ValueError, match="standard wheels only"):
        hook.initialize(version, {})


def test_candidate_rejects_hardlinked_helper(candidate, tmp_path):
    _, bundle, hook, _ = candidate
    os.link(bundle / "alysis-sandbox.exe", tmp_path / "shared.exe")
    with pytest.raises(ValueError, match="single-link"):
        hook.initialize("standard", {})


@pytest.mark.parametrize("flag", ["ALYSIS_RELEASE_CANDIDATE", "ALYSIS_WINDOWS_RELEASE"])
def test_candidate_refuses_release_packaging(candidate, monkeypatch, flag):
    _, _, hook, _ = candidate
    monkeypatch.setenv(flag, "1")
    with pytest.raises(ValueError, match="cannot be used for release"):
        hook.initialize("standard", {})
