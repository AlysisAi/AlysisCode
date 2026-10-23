from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sandbox/npm-rebuild/build.py"
SPEC = importlib.util.spec_from_file_location("npm_rebuild", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


@pytest.mark.parametrize(
    "name", ["../escape", "/tmp/escape", "package/../escape", "package\\escape"]
)
def test_distribution_rejects_paths_outside_package(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "bad.tgz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo(name)
        info.size = 1
        bundle.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unexpected npm archive member"):
        BUILDER.extract_distribution(archive, tmp_path / "extract", dependencies=True)
    assert not (tmp_path / "escape").exists()


def test_distribution_rejects_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tgz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("package/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        bundle.addfile(info)
    with pytest.raises(ValueError, match="Unexpected npm archive member"):
        BUILDER.extract_distribution(archive, tmp_path / "extract", dependencies=True)


def test_runtime_source_excludes_all_original_dependencies(tmp_path: Path) -> None:
    archive = tmp_path / "upstream.tgz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name in ("package/LICENSE", "package/lib/npm.js", "package/node_modules/old/index.js"):
            info = tarfile.TarInfo(name)
            info.size = 1
            bundle.addfile(info, io.BytesIO(b"x"))
    BUILDER.extract_distribution(archive, tmp_path / "runtime", dependencies=False)
    root = tmp_path / "runtime/package"
    assert (root / "LICENSE").read_bytes() == b"x"
    assert (root / "lib/npm.js").read_bytes() == b"x"
    assert not (root / "node_modules").exists()


def test_lock_retains_only_integrity_bound_registry_packages() -> None:
    lock = json.loads((SCRIPT.parent / "package-lock.json").read_text())
    for name, package in lock["packages"].items():
        if not name:
            continue
        assert name.startswith("node_modules/")
        assert package["resolved"].startswith("https://registry.npmjs.org/")
        assert package["integrity"].startswith("sha512-")
        assert not package.get("link")
        assert not package.get("dev")
