#!/usr/bin/env python3
"""Rebuild the complete npm runtime from a reviewed lock; never publish to npm."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPSTREAM_URL = "https://registry.npmjs.org/npm/-/npm-11.20.0.tgz"
UPSTREAM_SHA256 = "d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483"
SECURITY_FIXES = {
    "brace-expansion": "5.0.12",
    "undici": "6.28.1",
    "http-cache-semantics": "4.3.0",
    "ip-address": "10.7.3",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def extract_distribution(archive: Path, destination: Path, *, dependencies: bool) -> None:
    """Only extract ordinary files/directories beneath the package prefix."""
    with tarfile.open(archive, "r:gz") as bundle:
        members = []
        seen: set[str] = set()
        for member in bundle.getmembers():
            parts = Path(member.name).parts
            if (
                not parts
                or parts[0] != "package"
                or ".." in parts
                or "\\" in member.name
                or not (member.isfile() or member.isdir())
                or member.name in seen
            ):
                raise ValueError("Unexpected npm archive member")
            seen.add(member.name)
            if dependencies or parts[1:2] != ("node_modules",):
                members.append(member)
        bundle.extractall(destination, members=members, filter="data")


def verify_runtime(root: Path, lock: dict) -> list[dict[str, str]]:
    expected = {key: entry for key, entry in lock["packages"].items() if key}
    inventory = []
    for key, entry in sorted(expected.items()):
        path = root / key / "package.json"
        package = read_json(path)
        if package["version"] != entry["version"]:
            raise ValueError(f"Installed version differs from frozen lock: {key}")
        if not entry.get("resolved", "").startswith("https://registry.npmjs.org/"):
            raise ValueError(f"Unapproved package origin: {key}")
        if not entry.get("integrity", "").startswith("sha512-"):
            raise ValueError(f"Missing package integrity: {key}")
        inventory.append({"path": key, "name": package["name"], "version": package["version"]})
    # Walk package roots, not package.json fixtures inside a dependency.
    observed: set[str] = set()

    def visit(modules: Path) -> None:
        if not modules.is_dir():
            return
        for item in modules.iterdir():
            if item.name.startswith("."):
                continue
            children = item.iterdir() if item.name.startswith("@") else [item]
            for package_dir in children:
                if not (package_dir / "package.json").is_file():
                    raise ValueError(f"Incomplete installed package: {package_dir.name}")
                observed.add(package_dir.relative_to(root).as_posix())
                visit(package_dir / "node_modules")

    visit(root / "node_modules")
    if observed != set(expected):
        raise ValueError("Installed package set differs from frozen lock")
    for name, version in SECURITY_FIXES.items():
        if {p["version"] for p in inventory if p["name"] == name} != {version}:
            raise ValueError(f"Security replacement is missing or duplicated: {name}")
    return inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    output = args.output_dir.resolve()
    # A fresh directory also prevents npm ci from deleting an unrelated tree.
    work.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    archive = output / "upstream-npm-11.20.0.tgz"
    with urllib.request.urlopen(UPSTREAM_URL, timeout=120) as response:
        archive.write_bytes(response.read())
    if sha256(archive) != UPSTREAM_SHA256:
        raise ValueError("Upstream npm archive digest mismatch")
    extract_distribution(archive, work / "bootstrap", dependencies=True)
    extract_distribution(archive, work / "runtime", dependencies=False)
    runtime = work / "runtime" / "package"
    bootstrap = work / "bootstrap" / "package" / "bin" / "npm-cli.js"
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(HERE / name, runtime / name)
        shutil.copyfile(HERE / name, output / name)
    lock = read_json(runtime / "package-lock.json")
    manifest = read_json(runtime / "package.json")
    if manifest.get("overrides") != SECURITY_FIXES:
        raise ValueError("Manifest security overrides differ from the reviewed recipe")
    lock_hash = sha256(runtime / "package-lock.json")
    env = os.environ.copy()
    env.update(
        {
            "NPM_CONFIG_CACHE": str(work / "cache"),
            "NPM_CONFIG_USERCONFIG": str(work / "empty.npmrc"),
            "NPM_CONFIG_GLOBALCONFIG": str(work / "empty-global.npmrc"),
            "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        }
    )
    npm = ["node", str(bootstrap)]

    def run(arguments: list[str]) -> str:
        return subprocess.check_output(
            npm + arguments,
            cwd=runtime,
            env=env,
            text=True,
            timeout=600,
        )

    print(run(["ci", "--ignore-scripts", "--no-audit", "--no-fund"]), flush=True)
    inventory = verify_runtime(runtime, lock)
    # A second full install must succeed from the retained cache, with networking
    # disabled by npm. All dependency integrity values are rechecked by npm ci.
    print(run(["ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"]), flush=True)
    if inventory != verify_runtime(runtime, lock):
        raise ValueError("Offline rebuild changed the dependency inventory")
    if sha256(runtime / "package-lock.json") != lock_hash:
        raise ValueError("Frozen installation changed the lock")
    write_json(output / "npm-ls.json", json.loads(run(["ls", "--all", "--json"])))
    audit = subprocess.run(
        npm + ["audit", "--audit-level=high", "--json"],
        cwd=runtime,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    (output / "npm-audit.json").write_text(audit.stdout, encoding="utf-8")
    if audit.returncode != 0:
        raise ValueError("npm dependency audit failed; inspect npm-audit.json")
    # Re-bundle every dependency after rebuilding the complete node_modules tree.
    # Upstream generated docs and licenses are retained; no lifecycle scripts run.
    manifest["bundleDependencies"] = sorted(manifest["dependencies"])
    write_json(runtime / "package.json", manifest)
    packed = json.loads(
        run(
            [
                "pack",
                "--offline",
                "--ignore-scripts",
                "--json",
                "--pack-destination",
                str(output),
            ]
        )
    )[0]
    result = output / packed["filename"]
    extract_distribution(result, work / "verification", dependencies=True)
    if inventory != verify_runtime(work / "verification" / "package", lock):
        raise ValueError("Packed archive differs from the installed runtime")
    write_json(output / "dependency-inventory.json", inventory)
    # Retain the content-addressed registry inputs, but not npm logs or credentials.
    with tarfile.open(output / "dependency-cache.tgz", "w:gz") as cache:
        cache.add(work / "cache" / "_cacache", arcname="_cacache")
    evidence = {
        "schema_version": 1,
        "version": manifest["version"],
        "upstream_url": UPSTREAM_URL,
        "upstream_sha256": UPSTREAM_SHA256,
        "source_commit": manifest["alysisRebuild"]["sourceCommit"],
        "manifest_sha256": sha256(HERE / "package.json"),
        "lock_sha256": lock_hash,
        "builder_sha256": sha256(Path(__file__)),
        "node_version": subprocess.check_output(["node", "--version"], text=True).strip(),
        "bootstrap_npm_version": run(["--version"]).strip(),
        "runtime_packages": len(inventory),
        "offline_clean_install": "passed",
        "packed_inventory": "passed",
        "dependency_audit": "passed",
        "archive": result.name,
        "archive_sha256": sha256(result),
        "cache_sha256": sha256(output / "dependency-cache.tgz"),
    }
    write_json(output / "npm-rebuild-evidence.json", evidence)
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    main()
