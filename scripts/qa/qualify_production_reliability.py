"""Blocking source and installed-wheel reliability qualification; no live provider.

Run from a frozen development environment. Dependencies are reused from that
environment; the package itself is installed into a fresh, isolated target.
The existing distribution validator separately qualifies a clean installation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RELIABILITY_TESTS = (
    "test_objective_removed.py",
    "test_task_evidence_lifecycle.py",
    "test_acceptance_contract.py",
    "test_acceptance_contract_integration.py",
    "test_acceptance_evidence_authority_integration.py",
    "test_completion_certificate.py",
    "test_task_outcome_authority.py",
    "test_agent_loop_event_emission.py",
    "test_provider_failure_salvage.py",
    "test_durable_service_ownership.py",
    "test_durable_service_cleanup_recovery.py",
    "test_durable_service_manager.py",
    "test_shell_service_integration.py",
    "test_service_deadline_handoff.py",
    "test_managed_host_deadline_resolver.py",
    "test_managed_host_deadline_contract.py",
    "test_managed_host_startup_deadline.py",
    "test_managed_deadline_anchor_adapters.py",
    "test_execution_deadline.py",
    "test_process_reaping.py",
    "test_deadline_finalization.py",
    "test_run_wall_clock_budget.py",
    "test_box_managed_deadline.py",
    "test_harbor_terminal_bench_adapter.py",
    "test_terminal_bench_deadline_adapter.py",
    "test_read_delivery_authority.py",
    "test_tool_output_offload.py",
    "test_consumer_verification_profiles.py",
    "test_runtime_capability_authority.py",
    "test_visual_delivery_authority.py",
    "test_anytime_checkpoint.py",
    "test_checkpoint_review.py",
    "test_checkpoint_deadline_boundaries.py",
    "test_checkpoint_worker_cleanup.py",
    "test_release_reliability_gate.py",
    "test_shell_wait.py",
    "test_prompt_payload.py",
    "test_prompts_invariants.py",
    "test_greenfield_verification_bootstrap.py",
    "test_verify_gate.py",
    "test_verify_tool.py",
    "test_turn_contract_v2.py",
    "test_one_shot_prompt_contract.py",
    "test_cli_api_key.py::test_system_prompt_reinforces_narrow_repo_execution_changes",
)
SAFETY_TESTS = (
    "test_agent_loop_review_approval.py",
    "test_fs_safety_policy.py",
    "test_fs_tools.py",
    "test_permission_policy.py",
    "test_permissions_staging.py",
    "test_workspace_binding.py",
    "test_workspace_context.py",
    "test_workspace_trust.py",
    "test_workspace_guard_input_stash.py",
    "test_task_scope.py",
    "test_provider_error_url_privacy.py",
    "test_logging_redaction.py",
    "test_search_rg_security.py",
    "test_custom_tools_trust.py",
    "test_chat_resume.py",
    "test_mcp_scope.py",
)
INSTALLED_TESTS = (
    "test_deadline_fault_injection.py",
    "test_http_cancellation.py",
    "test_packaged_deadline_launch.py",
    "test_checkpoint_deadline_boundaries.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*arguments: str) -> str:
    return subprocess.check_output(
        ["git", *arguments], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def _source_identity() -> dict[str, Any]:
    names = _git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    entries = {}
    for name in sorted(set(names)):
        path = ROOT / name
        if path.is_file() and (
            name.startswith(("src/", "scripts/", "tests/", ".github/"))
            or name in {"pyproject.toml", "uv.lock"}
        ):
            entries[name] = _sha256(path)
    return {
        "commit": _git("rev-parse", "HEAD"),
        "dirty": bool(_git("status", "--porcelain")),
        "source_sha256": hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "lock_sha256": _sha256(ROOT / "uv.lock"),
        "files": entries,
    }


def _stop_owned_tree(process: subprocess.Popen[Any]) -> None:
    # Retain the live parent identity while collecting its descendants. This
    # does not sweep unrelated processes, and is used only for this runner's job.
    import psutil

    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for item in reversed(children):
        try:
            item.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        parent.kill()
    except psutil.NoSuchProcess:
        pass
    psutil.wait_procs([*children, parent], timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--full", action="store_true", help="Also run the entire source pytest suite"
    )
    parser.add_argument("--require-clean", action="store_true", help="Reject uncommitted source")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output_dir or ROOT / "qa_reports" / "production" / stamp).resolve()
    # Never overwrite a previous qualification, including failed evidence.
    output.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "python": sys.version,
        "platform": platform.platform(),
        "dependency_mode": "caller_environment; CI uses uv sync --frozen --extra dev --extra documents",
        "dependencies": dict(
            sorted(
                (item.metadata["Name"], item.version) for item in importlib.metadata.distributions()
            )
        ),
        "provider_fixture": "loopback only; installed CLI rejects external DNS",
        "stages": [],
    }
    manifest_path = output / "manifest.json"

    def save() -> None:
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    def run(
        name: str,
        command: list[str],
        *,
        cwd: Path = ROOT,
        env: dict[str, str] | None = None,
        timeout: float = 900,
    ) -> None:
        log = output / f"{name}.log"
        started = time.monotonic()
        timed_out = False
        print(f"Running {name}", flush=True)
        with log.open("w", encoding="utf-8") as stream:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop_owned_tree(process)
                returncode = process.wait(timeout=10)
            except BaseException:
                _stop_owned_tree(process)
                process.wait(timeout=10)
                raise
        manifest["stages"].append(
            {
                "name": name,
                "command": command,
                "returncode": returncode,
                "timed_out": timed_out,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "log": log.name,
                "log_sha256": _sha256(log),
            }
        )
        save()
        if returncode or timed_out:
            raise RuntimeError(f"{name} failed; see {log}")

    try:
        manifest["source"] = _source_identity()
        save()
        if args.require_clean and manifest["source"]["dirty"]:
            raise RuntimeError("Release qualification requires a clean checkout")
        for group, files in (
            ("source-reliability", RELIABILITY_TESTS),
            ("source-safety", SAFETY_TESTS),
        ):
            run(
                group,
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={output / (group + '.xml')}",
                    *(str(ROOT / "tests" / name) for name in files),
                ],
            )
        if args.full:
            run(
                "source-full",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={output / 'source-full.xml'}",
                ],
                timeout=3600,
            )
        dist = output / "dist"
        build_env = dict(os.environ)
        build_env["SOURCE_DATE_EPOCH"] = _git("show", "-s", "--format=%ct", "HEAD")
        run(
            "build-wheel",
            [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(dist)],
            env=build_env,
        )
        wheels = list(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("Expected exactly one wheel from the current source")
        wheel = wheels[0]
        manifest["wheel"] = {"name": wheel.name, "sha256": _sha256(wheel)}
        installed = output / "installed"
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("Pinned uv is required to install the built wheel")
        run(
            "install-wheel",
            [
                uv,
                "pip",
                "install",
                "--python",
                sys.executable,
                "--target",
                str(installed),
                "--no-deps",
                "--no-index",
                str(wheel),
            ],
        )
        consumer = output / "consumer"
        consumer.mkdir()
        for name in INSTALLED_TESTS:
            shutil.copy2(ROOT / "tests" / name, consumer / name)
        adapters = Path("scripts/benchmarks/terminal_bench")
        shutil.copytree(
            ROOT / adapters, consumer / adapters, ignore=shutil.ignore_patterns("__pycache__")
        )
        config = consumer / "pytest.ini"
        config.write_text("[pytest]\n", encoding="utf-8")
        runner = consumer / "run_installed.py"
        runner.write_text(
            "import pathlib, sys\n"
            f"installed = pathlib.Path({str(installed)!r}).resolve()\n"
            "sys.path.insert(0, str(installed))\n"
            "import alysis_code, pytest\n"
            "assert pathlib.Path(alysis_code.__file__).resolve().is_relative_to(installed)\n"
            "print('qualified_package=' + str(alysis_code.__file__), flush=True)\n"
            f"raise SystemExit(pytest.main({['-q', '-p', 'no:cacheprovider', '-c', str(config), '--durations=20', '--junitxml=' + str(output / 'installed-faults.xml'), *INSTALLED_TESTS]!r}))\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        run("installed-faults", [sys.executable, str(runner)], cwd=consumer, env=env, timeout=180)
        after = _source_identity()
        manifest["dirty_after"] = after["dirty"]
        manifest["source_unchanged"] = (
            after["commit"] == manifest["source"]["commit"]
            and after["source_sha256"] == manifest["source"]["source_sha256"]
        )
        if not manifest["source_unchanged"]:
            raise RuntimeError("Source changed during qualification; rerun against stable source")
        if args.require_clean and after["dirty"]:
            raise RuntimeError("Checkout became dirty during release qualification")
        manifest["status"] = "passed"
    except (Exception, KeyboardInterrupt) as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    print(f"{manifest['status']}: {manifest_path}", flush=True)
    return 0 if manifest["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
