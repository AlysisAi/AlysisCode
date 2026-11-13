"""Complete-source verification and same-schema host release rollback.

No data rollback, infrastructure provisioning, public ingress or secret values.
An additive schema migration needs its own reviewed backup/upgrade operation.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

MANIFEST = "hosted-release.json"


def _path(name):
    if not isinstance(name, str):
        raise ValueError("Invalid hosted release member.")
    path = PurePosixPath(name)
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or str(path) != name
        or path.is_absolute()
        or ".." in path.parts
        or not (
            name.startswith("src/")
            or name.startswith("scripts/bots-hosted/")
            or name in ("pyproject.toml", "uv.lock")
        )
    ):
        raise ValueError("Invalid hosted release member.")
    return path


def _read(root, name):
    path = root
    for part in _path(name).parts:
        path /= part
        if path.is_symlink():
            raise ValueError("Hosted source members cannot be symbolic links.")
    return path.read_bytes()


def validate_manifest(manifest):
    if (
        manifest.get("format") != 1
        or not re.fullmatch(r"[a-f0-9]{40}", str(manifest.get("source_commit", "")))
        or type(manifest.get("schema")) is not int
        or not isinstance(manifest.get("files"), dict)
        or not 1 <= len(manifest["files"]) <= 10000
    ):
        raise ValueError("Invalid hosted release manifest.")
    for name, digest in manifest["files"].items():
        _path(name)
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid hosted source fingerprint.")
    for required in (
        "pyproject.toml",
        "uv.lock",
        "src/alysis_code/bots/schema.py",
        "scripts/bots-hosted/install.py",
    ):
        if required not in manifest["files"]:
            raise ValueError("Hosted release is incomplete.")


def _schema(root):
    tree = ast.parse(_read(root, "src/alysis_code/bots/schema.py"))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if type(value) is int and value > 0:
                return value
    raise ValueError("Hosted source schema is unconfirmed.")


def source_manifest(root):
    root = Path(root).resolve(strict=True)
    roots = ["src", "scripts/bots-hosted", "pyproject.toml", "uv.lock"]
    if subprocess.check_output(["git", "status", "--porcelain", "--", *roots], cwd=root):
        raise ValueError("Commit the reviewed hosted source before building a manifest.")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    names = subprocess.check_output(
        ["git", "ls-files", "--", *roots], cwd=root, text=True
    ).splitlines()
    manifest = {
        "format": 1,
        "source_commit": commit,
        "schema": _schema(root),
        "files": {name: hashlib.sha256(_read(root, name)).hexdigest() for name in names},
    }
    validate_manifest(manifest)
    return manifest


def verify_release(root, manifest=None):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Use an immutable release directory, not a symlink.")
    manifest = manifest or json.loads((root / MANIFEST).read_text())
    validate_manifest(manifest)
    for name, digest in manifest["files"].items():
        if hashlib.sha256(_read(root, name)).hexdigest() != digest:
            raise ValueError("Hosted release source verification failed.")
    if _schema(root) != manifest["schema"]:
        raise ValueError("Hosted schema declaration differs from verified source.")
    return manifest


def qualify_environment(environment, project):
    """Check installed requirements/extras and record the reused inventory.

    This does not claim an older environment was built from the newer lockfile.
    """
    program = """import importlib.metadata as metadata,json,sys,tomllib
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
project=tomllib.loads(sys.stdin.read())['project']
environment=default_environment()
assert environment['python_full_version'] in SpecifierSet(project.get('requires-python',''))
todo=[]
def include(raw,extras):
 requirement=Requirement(raw)
 if requirement.marker is None or any(requirement.marker.evaluate({**environment,'extra':extra}) for extra in {'',*extras}):todo.append(requirement)
for raw in project.get('dependencies',[]):include(raw,set())
for extra in ('server','documents','azure-host','shared-database','cloud-storage'):
 for raw in project.get('optional-dependencies',{}).get(extra,[]):include(raw,{extra})
seen={};inventory={}
while todo:
 requirement=todo.pop();name=canonicalize_name(requirement.name)
 version=metadata.version(name);assert version in requirement.specifier
 extras=set(requirement.extras)|seen.get(name,set())
 inventory[name]=version
 if name in seen and extras==seen[name]:continue
 seen[name]=extras
 for raw in metadata.requires(name) or []:include(raw,extras)
print(json.dumps({'python':environment['python_full_version'],'packages':dict(sorted(inventory.items()))}))
"""
    try:
        result = subprocess.run(
            [str(Path(environment) / "bin/python"), "-I", "-c", program],
            input=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=True,
        )
        inventory = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("Hosted dependency environment is unqualified.") from None
    return {
        "inventory": inventory,
        "inventory_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "project_sha256": hashlib.sha256(project).hexdigest(),
    }


def prepare_release(base, candidate, manifest, overlay, *, qualified_environment=None):
    """Reuse only hash-matching base files; an incomplete delta cannot publish."""
    base, candidate, overlay = Path(base), Path(candidate), Path(overlay)
    validate_manifest(manifest)
    if candidate.exists() or candidate.is_symlink():
        raise FileExistsError("Use a new immutable hosted release directory.")
    sources = {}
    for name, digest in manifest["files"].items():
        origin = overlay if (overlay / name).exists() else base
        if hashlib.sha256(_read(origin, name)).hexdigest() != digest:
            raise ValueError("Incomplete or changed hosted source; release was not prepared.")
        sources[name] = origin
    lock_changed = (base / "uv.lock").exists() and manifest["files"]["uv.lock"] != hashlib.sha256(
        _read(base, "uv.lock")
    ).hexdigest()
    if lock_changed and qualified_environment is None:
        raise ValueError("Dependency changes require a separately qualified environment.")
    environment = Path(qualified_environment or base / ".venv").resolve(strict=True)
    if not (environment / "bin/python").is_file():
        raise ValueError("A qualified hosted Python environment is required.")
    qualification = None
    if qualified_environment is not None:
        qualification = qualify_environment(
            environment, _read(sources["pyproject.toml"], "pyproject.toml")
        )
        qualification["candidate_lock_sha256"] = manifest["files"]["uv.lock"]
    candidate.mkdir(parents=True, exist_ok=False)
    for name, origin in sources.items():
        target = candidate / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_read(origin, name))
        shutil.copymode(origin / name, target)
    (candidate / ".venv").symlink_to(environment, target_is_directory=True)
    (candidate / MANIFEST).write_text(json.dumps(manifest, sort_keys=True) + "\n")
    if qualification is not None:
        (candidate / "hosted-environment.json").write_text(
            json.dumps(qualification, sort_keys=True) + "\n"
        )
    verify_release(candidate)
    return {
        "prepared": True,
        "source_commit": manifest["source_commit"],
        "verified_files": len(sources),
    }


def activate_release(candidate, previous, *, schema, install, control, ready):
    """One activation attempt; restore reviewed code if readiness fails."""
    candidate, previous = Path(candidate), Path(previous)
    if candidate.resolve() == previous.resolve():
        raise ValueError("Candidate and previous release must be different.")
    new, old = verify_release(candidate), verify_release(previous)
    if new["schema"] != schema or old["schema"] != schema:
        raise ValueError("Automatic activation/rollback requires the current database schema.")
    control("stop")
    try:
        install(candidate)
        control("start")
        if not ready(candidate):
            raise RuntimeError("Candidate readiness was not confirmed.")
    except Exception:
        control("stop")
        install(previous)
        control("start")
        if not ready(previous):
            raise RuntimeError("Previous release recovery remains unconfirmed.") from None
        return {"activated": False, "rolled_back": True, "source_commit": old["source_commit"]}
    return {"activated": True, "rolled_back": False, "source_commit": new["source_commit"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--source", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "manifest":
            result = source_manifest(args.source)
            if args.output.exists():
                raise FileExistsError("Use a new manifest output path.")
            args.output.write_text(json.dumps(result, sort_keys=True) + "\n")
        else:
            result = verify_release(args.source)
        print(
            json.dumps(
                {
                    "verified": True,
                    "source_commit": result["source_commit"],
                    "verified_files": len(result["files"]),
                }
            )
        )
        return 0
    except Exception:
        print('{"verified":false,"reason":"hosted_release_unconfirmed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
