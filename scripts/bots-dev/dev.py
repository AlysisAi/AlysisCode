#!/usr/bin/env python3
"""Run the Linux prototype using an existing saved provider connection.

The provider key is read in this process and sent through Docker stdin. The app
receives only a freshly generated local API token in its process environment.
No credentials are written to .env, Docker metadata, the repo, or logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def development_target(testing: bool, test_id: str | None, port: int | None) -> tuple[str, int]:
    if test_id and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", test_id):
        raise ValueError("Use a test ID with lowercase letters, digits and hyphens (up to 40).")
    isolated = testing or test_id is not None
    project = "alysis-bots-test-" + (test_id or uuid4().hex[:12]) if isolated else "alysis-bots-dev"
    selected_port = port if port is not None else (7081 if isolated else 7080)
    if not 1024 <= selected_port <= 65535:
        raise ValueError("Use a local port between 1024 and 65535.")
    return project, selected_port


def main() -> int:
    from alysis_code.config import load_config, resolve_profile_api_key
    from alysis_code.profiles import get_profile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        default="zai-coding-plan",
        help="Existing Alysis provider profile (no fallback)",
    )
    parser.add_argument("--model", help="Explicit inexpensive model override")
    computer = parser.add_mutually_exclusive_group()
    computer.add_argument(
        "--browser-computer",
        action="store_true",
        help="Opt in to the guarded online Linux browser and Computer view",
    )
    computer.add_argument(
        "--desktop-computer",
        action="store_true",
        help="Opt in to task-approved offline Linux commands, apps, files and visual input",
    )
    computer.add_argument(
        "--computer",
        action="store_true",
        help="Enable browser and persistent file processing for the same task",
    )
    parser.add_argument("--protocol", choices=["openai_compat", "anthropic_messages"])
    parser.add_argument("--reasoning-effort", help="Provider-supported reasoning effort, e.g. low")
    parser.add_argument("--provider", help="Provider adapter identity, e.g. alysis")
    parser.add_argument(
        "--max-calls",
        type=int,
        default=100,
        help="Total durable model-call allowance for this database",
    )
    parser.add_argument("--app", type=Path, help="Built Alysis desktop source checkout to open")
    parser.add_argument(
        "--port", type=int, help="Loopback port; preview defaults to 7080, tests to 7081"
    )
    parser.add_argument(
        "--test-id",
        help="Reuse isolated test state by name; never selects the user preview database",
    )
    parser.add_argument(
        "--check", action="store_true", help="Verify service and exit without making a model call"
    )
    parser.add_argument(
        "--build", action="store_true", help="Build the Linux image before starting"
    )
    parser.add_argument(
        "--test-live", action="store_true", help="Run the desktop live-model check; requires --app"
    )
    parser.add_argument(
        "--test-schedules",
        action="store_true",
        help="Run schedule UI checks with provider calls disabled; requires --app",
    )
    parser.add_argument(
        "--coding-workspace",
        type=Path,
        help="Explicit editable workspace for the isolated AlysisCode worker",
    )
    parser.add_argument(
        "--coding-calls",
        type=int,
        default=8,
        help="Execution calls per coding job, including repairs (1–30); one separate report call",
    )
    parser.add_argument(
        "--coding-verify",
        action="append",
        default=[],
        help="Verification command to run inside Linux (repeatable)",
    )
    parser.add_argument(
        "--linux-worker",
        action="store_true",
        help="Provision per-bot workspaces and run the coding controller inside Linux",
    )
    parser.add_argument(
        "--keep-service-on-close",
        action="store_true",
        help="Keep Linux services running after the preview desktop closes; Ctrl+C still stops them",
    )
    args = parser.parse_args()
    if args.computer:
        args.browser_computer = args.desktop_computer = True
    if not 1 <= args.coding_calls <= 30:
        parser.error("Use a coding execution allowance between 1 and 30 calls.")
    if args.linux_worker and args.coding_workspace:
        parser.error("Choose managed Linux workspaces or an explicit host fixture, not both.")
    if args.coding_workspace and not args.coding_workspace.resolve().is_dir():
        parser.error("The coding workspace must be an existing directory.")
    if args.coding_verify and not (args.coding_workspace or args.linux_worker):
        parser.error("Verification commands require --coding-workspace.")
    if (args.test_live or args.test_schedules) and not args.app:
        parser.error("Desktop checks require --app.")
    if args.test_live and args.test_schedules:
        parser.error("Choose either live-model checks or schedule UI checks.")
    try:
        project, port = development_target(
            args.test_live or args.test_schedules, args.test_id, args.port
        )
    except ValueError as exc:
        parser.error(str(exc))
    container_name = project + "-runtime"
    compose_env = {
        **os.environ,
        "ALYSIS_BOTS_PORT": str(port),
        "ALYSIS_BOTS_SECCOMP": str(ROOT / "scripts/cloud-dev/seccomp.json"),
    }
    cfg = load_config()
    profile = get_profile(cfg, args.profile)
    if profile is None:
        parser.error("The named provider profile does not exist. Connect it in Alysis first.")
    resolution = resolve_profile_api_key(cfg, profile.name)
    if not resolution.key:
        parser.error("No saved API key is available for this profile.")
    if not 0 <= args.max_calls <= 10000:
        parser.error("Use a model-call allowance between 0 and 10000.")
    command = [
        "docker",
        "--context",
        "desktop-linux",
        "compose",
        "--project-name",
        project,
        "-f",
        str(Path(__file__).with_name("compose.yaml")),
    ]
    if args.browser_computer or args.desktop_computer:
        command += ["-f", str(Path(__file__).with_name("browser.compose.yaml"))]
    context = subprocess.run(
        ["docker", "context", "inspect", "desktop-linux", "--format", "{{.Endpoints.docker.Host}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not context.startswith("unix://"):
        parser.error("The desktop-linux context must use a local Unix socket.")
    if args.build:
        subprocess.run([*command, "build", "bots"], check=True, env=compose_env)
    token = secrets.token_urlsafe(36)
    coding_token = secrets.token_urlsafe(36) if args.coding_workspace or args.linux_worker else None
    payload = {
        "token": token,
        "coding_token": coding_token,
        "managed_workspaces": args.linux_worker,
        "browser_enabled": args.browser_computer,
        "desktop_enabled": args.desktop_computer,
        "api_key": resolution.key,
        "base_url": profile.base_url,
        "model": args.model or profile.default_model,
        "protocol": args.protocol or profile.protocol,
        "provider_key": args.provider,
        "max_calls": 0 if args.check or args.test_schedules else args.max_calls,
        "coding_calls": args.coding_calls,
        "reasoning_effort": args.reasoning_effort or profile.reasoning_effort,
    }
    process = subprocess.Popen(
        [
            *command,
            "run",
            "--rm",
            "--service-ports",
            "--use-aliases",
            "--name",
            container_name,
            "-T",
            "bots",
        ],
        stdin=subprocess.PIPE,
        env=compose_env,
    )
    app = None
    worker = None
    stopping = False

    def stop(_signum=None, _frame=None):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        assert process.stdin is not None
        process.stdin.write((json.dumps(payload) + "\n").encode())
        process.stdin.close()
        payload.clear()
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + token},
            timeout=3,
        ) as client:
            ready = None
            for _ in range(60):
                if process.poll() is not None or stopping:
                    break
                try:
                    response = client.get("/v1/status")
                    if response.is_success:
                        ready = response.json()
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            if ready is None:
                print(
                    "Bots did not become ready. Check the container output above.", file=sys.stderr
                )
                return 1
            print(
                f"Bots ready on local Linux. Model: {ready['model']}. Calls used: {ready['usage']['calls']}/{ready['max_calls']}.",
                flush=True,
            )
            print(
                "No Azure resources were created. Ctrl+C stops the service and preserves its database.",
                flush=True,
            )
            print(f"Development state: {project}. Loopback port: {port}.", flush=True)
            if args.check:
                return 0
            if args.linux_worker:
                worker = subprocess.Popen(
                    [*command, "run", "--rm", "--name", project + "-coding", "-T", "coding"],
                    stdin=subprocess.PIPE,
                    env=compose_env,
                )
                worker_payload = {
                    "url": "http://bots:7080",
                    "token": coding_token,
                    "model": args.model or profile.default_model,
                    "base_url": profile.base_url,
                    "verify": args.coding_verify,
                }
                worker.stdin.write((json.dumps(worker_payload) + "\n").encode())
                worker.stdin.close()
                worker_payload.clear()
            if args.coding_workspace:
                worker = subprocess.Popen(
                    [sys.executable, "-m", "alysis_code.bots.coding_worker"],
                    stdin=subprocess.PIPE,
                    env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                )
                worker_payload = {
                    "url": f"http://127.0.0.1:{port}",
                    "token": coding_token,
                    "workspace": str(args.coding_workspace.resolve()),
                    "state": str(ROOT / ".dev" / "coding-workers"),
                    "model": args.model or profile.default_model,
                    "base_url": profile.base_url,
                    "verify": args.coding_verify,
                    "image": compose_env.get("ALYSIS_BOTS_IMAGE", "alysis-bots-dev:local"),
                }
                worker.stdin.write((json.dumps(worker_payload) + "\n").encode())
                worker.stdin.close()
                worker_payload.clear()
            if args.app:
                app_root = args.app.resolve()
                if not (app_root / "out/main/index.js").is_file():
                    raise SystemExit("Build the desktop checkout before using --app.")
                data_dir = (
                    ROOT / ".dev" / ("bots-desktop" if project == "alysis-bots-dev" else project)
                )
                data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                engine = ROOT / ".dev" / "bots-engine"
                engine.write_text(
                    "#!/bin/sh\nexport PYTHONPATH="
                    + shlex.quote(str(ROOT / "src"))
                    + "\nexec "
                    + shlex.quote(sys.executable)
                    + ' -m alysis_code "$@"\n'
                )
                engine.chmod(0o700)
                # Runtime selection is a persisted, main-process-owned setting;
                # environment-only overrides are intentionally filtered by the app.
                supervisor_path = data_dir / "runtime-supervisor.json"
                supervisor = (
                    json.loads(supervisor_path.read_text()) if supervisor_path.exists() else {}
                )
                supervisor["customBinaryPath"] = str(engine)
                supervisor_path.write_text(json.dumps(supervisor) + "\n")
                supervisor_path.chmod(0o600)
                app_env = {
                    **os.environ,
                    "ALYSIS_BOTS_URL": f"http://127.0.0.1:{port}",
                    "ALYSIS_BOTS_TOKEN": token,
                    "ALYSIS_E2E": "1",
                    "ALYSIS_E2E_USER_DATA": str(data_dir),
                    "ALYSIS_ALLOW_MULTIPLE_INSTANCES": "1",
                    "ALYSIS_APP_REPO": str(ROOT),
                    "ALYSIS_APP_PYTHON": sys.executable,
                    "ALYSIS_APP_CLI": str(engine),
                    "PYTHONPATH": str(ROOT / "src"),
                }
                # This isolated development profile uses this checkout. The app
                # still performs its real capability gate; no compatibility bypass.
                if args.test_live or args.test_schedules:
                    app_env[
                        "ALYSIS_BOTS_E2E_SCHEDULES"
                        if args.test_schedules
                        else "ALYSIS_BOTS_E2E_LIVE"
                    ] = "1"
                    app = subprocess.Popen(
                        ["node", "tests/e2e-bots.mjs"], cwd=app_root, env=app_env
                    )
                else:
                    app = subprocess.Popen(
                        ["pnpm", "exec", "electron", "."], cwd=app_root, env=app_env
                    )
            while not stopping and process.poll() is None:
                if worker is not None and worker.poll() is not None:
                    print(
                        "The coding worker stopped; the service will stop without replaying its work.",
                        file=sys.stderr,
                    )
                    return 1
                if app is not None and app.poll() is not None:
                    if not args.keep_service_on_close or args.test_live or args.test_schedules:
                        break
                    app = None
                    print(
                        "Desktop closed; Linux services remain running. Ctrl+C stops this development service.",
                        flush=True,
                    )
                time.sleep(0.5)
            return (app.returncode if app is not None else process.returncode) or 0
    finally:
        if worker is not None and worker.poll() is None:
            if args.linux_worker:
                subprocess.run(
                    [
                        "docker",
                        "--context",
                        "desktop-linux",
                        "stop",
                        "--time",
                        "20",
                        project + "-coding",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                )
            else:
                worker.terminate()
            try:
                worker.wait(timeout=25)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        if app is not None and app.poll() is None:
            app.terminate()
        if process.poll() is None:
            subprocess.run(
                [
                    "docker",
                    "--context",
                    "desktop-linux",
                    "stop",
                    "--time",
                    "20",
                    container_name,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
            )
            process.wait(timeout=30)


if __name__ == "__main__":
    raise SystemExit(main())
