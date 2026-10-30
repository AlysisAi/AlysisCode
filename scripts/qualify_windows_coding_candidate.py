"""Offline coding-scope smoke test for the exactly identified Windows test kit.

Full file compatibility keeps its original verdict. Only the explicitly accepted
native read-only Git limitation is excluded from the separate coding verdict.
No account, application profile, firewall or Windows setting is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import traceback
import zipfile
from pathlib import Path, PurePosixPath

READONLY_GIT = {"git_status", "git_diff", "git_history"}
REQUIRED_FILES = {
    "fs_list",
    "search_rg",
    "symbol_search",
    "repo_map",
    "test_discover",
    "contained_git_patch",
    "git_metadata_unchanged",
    "large_prepared_write",
    "stale_write_refused",
    "contained_copy",
    "contained_root_overwrite",
    "contained_move",
    "contained_cross_directory_move",
    "contained_delete",
    "contained_mkdir",
    "image_bytes_publication",
    "tool_fs_read",
    "tool_fs_write",
    "tool_search",
    "private_hardlink_not_disclosed",
    "request_packets_removed",
    "private_canary_unchanged",
}


def file_verdict(result: dict, exit_code: int | None) -> dict:
    """Missing, duplicate, malformed and unknown observations cannot qualify."""
    passed, failures = result.get("checks"), result.get("failures")
    if not isinstance(passed, list) or not all(isinstance(n, str) for n in passed):
        return {"passed": False, "reason": "invalid passing observations"}
    if not isinstance(failures, list) or not all(isinstance(f, dict) for f in failures):
        return {"passed": False, "reason": "invalid failure observations"}
    failed = [f.get("check") for f in failures]
    if not all(isinstance(n, str) for n in failed):
        return {"passed": False, "reason": "invalid failure names"}
    seen = passed + failed
    complete = len(seen) == len(set(seen)) and set(seen) == REQUIRED_FILES | READONLY_GIT
    limitations = all(
        f["check"] in READONLY_GIT
        and isinstance(f.get("traceback"), str)
        and "ContainedGitDirectoryError: git_directory_access_denied:" in f["traceback"]
        for f in failures
    )
    full_pass = not failures
    consistent = result.get("passed") is full_pass and exit_code == (0 if full_pass else 1)
    ok = complete and REQUIRED_FILES <= set(passed) and limitations and consistent
    ok = ok and not any(result.get(key) for key in ("failure", "fixture_failures", "not_run"))
    return {
        "passed": ok,
        "full_file_compatibility_passed": full_pass and ok,
        "required_file_checks": len(REQUIRED_FILES),
        "known_readonly_limitations": failed if limitations else [],
        "complete_observations": complete,
        "reason": "qualified coding file scope" if ok else "unqualified file evidence",
    }


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def verify_engine_update(kit: Path, update: Path) -> tuple[dict, Path]:
    """Bind a small engine follow-up to the unchanged, previously supplied kit."""
    identity = read(update / "engine-update.json")
    if identity.get("schema_version") != 1 or identity.get("base_candidate_sha256") != digest(
        kit / "candidate.json"
    ):
        raise ValueError("Engine follow-up does not match the base candidate")
    source = identity.get("engine_source")
    if (
        not isinstance(source, str)
        or len(source) != 40
        or any(c not in "0123456789abcdef" for c in source)
    ):
        raise ValueError("Engine follow-up requires an exact source commit")
    files = identity.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Missing engine follow-up integrity index")
    for relative, expected in files.items():
        parts = PurePosixPath(relative)
        if parts.is_absolute() or ".." in parts.parts or "\\" in relative or ":" in relative:
            raise ValueError("Invalid engine follow-up path")
        path = (update / relative).resolve(strict=True)
        if not path.is_relative_to(update) or not path.is_file() or digest(path) != expected:
            raise ValueError("Engine follow-up integrity mismatch: " + relative)
    wheel_name = identity.get("wheel")
    required = {
        "scripts/qualify_windows_coding_candidate.py",
        "scripts/qualify_contained_file_tools.py",
        "scripts/qualify_windows_git_recovery.py",
        "scripts/qualify_windows_mxc_network.py",
        "scripts/qualify_windows_mxc.py",
        "scripts/windows_mxc_network.py",
        "src/alysis_code/__init__.py",
        "src/alysis_code/windows_mxc_contract.py",
    }
    if (
        not isinstance(wheel_name, str)
        or not wheel_name.startswith("wheels/alysis_code-")
        or not wheel_name.endswith("-py3-none-any.whl")
        or wheel_name not in files
        or not required <= files.keys()
        or digest(Path(__file__)) != files["scripts/qualify_windows_coding_candidate.py"]
    ):
        raise ValueError("Incomplete or mismatched engine follow-up entry point")
    return identity, update / wheel_name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--engine-update", type=Path, help="Verified follow-up bundle; base kit stays unchanged"
    )
    args = parser.parse_args()
    if sys.platform != "win32" or not args.output.is_absolute():
        parser.error("Native Windows and a new absolute output directory are required")
    kit = args.kit.resolve(strict=True)
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    resources = kit / "application/resources"
    bundle = resources / "cli-runtime"
    python = resources / "python-runtime/python.exe"
    report = {
        "production_ready": False,
        "coding_smoke_passed": False,
        "stages": {},
        "scope": "local installed coding smoke; not full read-only compatibility or release approval",
        "windows": list(sys.getwindowsversion()),
        "live_model_calls": 0,
        "not_run": [
            "packaged_renderer",
            "separate_lan_peer",
            "publisher_release_attestation",
            "other_windows_builds",
            "live_model_guidance_following",
        ],
    }

    def save():
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(("ALYSIS_", "SYLLIPTOR_", "PIP_", "GIT_", "PYTHON"))
    }
    env.update(
        PIP_CONFIG_FILE="NUL",
        PIP_NO_INDEX="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="NUL",
        GIT_TERMINAL_PROMPT="0",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
    )

    def run(name, argv, timeout):
        try:
            with (output / (name + ".log")).open("wb") as log:
                cp = subprocess.run(
                    list(map(str, argv)),
                    cwd=kit,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                )
            report["stages"][name] = {"exit_code": cp.returncode}
            return cp.returncode
        except Exception as exc:
            report["stages"][name] = {"exit_code": None, "error": type(exc).__name__}
            return None
        finally:
            save()

    try:
        identity = read(kit / "candidate.json")
        # The containing ZIP digest must be verified against the handoff before
        # extraction. This index then detects changed/missing installed payloads.
        for relative, expected in identity["files"].items():
            path = (kit / relative).resolve(strict=True)
            if not path.is_relative_to(kit) or digest(path) != expected:
                raise ValueError("Candidate file integrity mismatch: " + relative)
        manifest = read(bundle / "manifest.json")
        if manifest["fingerprint"] != identity["fingerprint"]:
            raise ValueError("Runtime fingerprint mismatch")
        helper = bundle / manifest["windowsSandbox"]["helper"]
        env.update(
            ALYSIS_WINDOWS_MXC_HELPER=str(helper),
            ALYSIS_WINDOWS_MXC_SHA256=manifest["windowsSandbox"]["sha256"],
        )
        report.update(
            fingerprint=manifest["fingerprint"],
            engine_commit=manifest["sourceCommit"],
            desktop_commit=identity["desktop_commit"],
            helper_sha256=digest(helper),
            candidate_sha256=digest(kit / "candidate.json"),
            verified_files=len(identity["files"]),
        )
        scripts = kit / "scripts"
        requirement = "alysis-code[server,dev]==" + manifest["version"]
        wheel = None
        if args.engine_update is not None:
            update = args.engine_update.resolve(strict=True)
            update_identity, wheel = verify_engine_update(kit, update)
            scripts = update / "scripts"
            requirement = str(wheel) + "[server,dev]"
            report.update(
                scope="isolated engine follow-up using unchanged base-kit dependencies/helper; not packaged Desktop acceptance",
                base_runtime_fingerprint=manifest["fingerprint"],
                engine_commit=update_identity["engine_source"],
                engine_update_sha256=digest(update / "engine-update.json"),
                engine_wheel_sha256=digest(wheel),
                engine_update_verified_files=len(update_identity["files"]),
            )
        runtime = output / "runtime"
        if run("venv", [python, "-I", "-B", "-m", "venv", runtime], 120) != 0:
            raise RuntimeError("Offline runtime creation failed")
        installed = runtime / "Scripts/python.exe"
        if (
            run(
                "install",
                [
                    installed,
                    "-I",
                    "-B",
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    bundle / "wheels",
                    requirement,
                ],
                180,
            )
            != 0
        ):
            raise RuntimeError("Offline installation failed")
        report["installed_python"] = str(installed)
        if wheel is not None:
            modules = ("alysis_code/host_file_handles.py", "alysis_code/host_read_worker.py")
            with zipfile.ZipFile(wheel) as archive:
                expected = {
                    name: hashlib.sha256(archive.read(name)).hexdigest() for name in modules
                }
            probe = (
                "import hashlib,json,sysconfig;from pathlib import Path;"
                "root=Path(sysconfig.get_paths()['purelib']);"
                f"print(json.dumps({{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in {modules!r}}}))"
            )
            if run("installed-identity", [installed, "-I", "-B", "-c", probe], 30) != 0:
                raise RuntimeError("Installed follow-up identity probe failed")
            actual = read(output / "installed-identity.log")
            if actual != expected:
                raise ValueError("Installed worker sources do not match the follow-up wheel")
            report["installed_worker_identity"] = actual
        files_exit = run(
            "file-tools",
            [
                installed,
                "-I",
                "-B",
                scripts / "qualify_contained_file_tools.py",
                output / "file-tools",
                "--backend",
                "windows-mxc",
            ],
            300,
        )
        files_path = output / "file-tools/result.json"
        report["coding_files"] = (
            file_verdict(read(files_path), files_exit) if files_path.exists() else {"passed": False}
        )
        recovery_exit = run(
            "git-recovery",
            [
                installed,
                "-I",
                "-B",
                scripts / "qualify_windows_git_recovery.py",
                "--output",
                output / "git-recovery",
            ],
            240,
        )
        recovery_path = output / "git-recovery/result.json"
        report["git_recovery_passed"] = (
            recovery_exit == 0
            and recovery_path.exists()
            and read(recovery_path).get("passed") is True
        )
        network_exit = run(
            "network",
            [
                python,
                "-I",
                "-B",
                scripts / "qualify_windows_mxc_network.py",
                "--helper",
                helper,
                "--sha256",
                env["ALYSIS_WINDOWS_MXC_SHA256"],
                "--node",
                resources / "bin/node.exe",
                "--output",
                output / "network",
            ],
            180,
        )
        network_path = output / "network/result.json"
        report["local_network_passed"] = (
            network_exit == 0 and network_path.exists() and read(network_path).get("passed") is True
        )
        report["coding_smoke_passed"] = (
            report["coding_files"]["passed"]
            and report["git_recovery_passed"]
            and report["local_network_passed"]
        )
        return 0 if report["coding_smoke_passed"] else 1
    except Exception:
        report["failure"] = traceback.format_exc()
        return 1
    finally:
        save()
        print(
            json.dumps(
                {
                    "coding_smoke_passed": report["coding_smoke_passed"],
                    "production_ready": False,
                    "evidence": str(output),
                }
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
