"""Opt-in bounded GLM check on an isolated Linux service, with a real desktop.

Uses only the saved Z.ai Coding Plan. Secrets stay in memory/stdin. The test
project contains deliberately broken Unicode code and pre-existing stdlib tests.
After reporting, stays open for native UI inspection until interrupted.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from alysis_code.config import load_config, resolve_profile_api_key  # noqa: E402
from alysis_code.profiles import get_profile  # noqa: E402

TESTS = """import unittest
from notes import normalize_text
class ExistingChecks(unittest.TestCase):
    def test_greek(self): self.assertEqual(normalize_text("ε\\u0301"), "έ")
    def test_japanese(self): self.assertEqual(normalize_text("日本語"), "日本語")
    def test_whitespace(self): self.assertEqual(normalize_text("  Ω\\r\\n"), "  Ω\\r\\n")
    def test_empty(self): self.assertEqual(normalize_text(""), "")
"""


def until(read, accepts, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = read()
            if accepts(result):
                return result
        except httpx.TransportError:
            pass
        time.sleep(0.25)
    raise RuntimeError("Verification study timed out; inspect saved local evidence.")


def main():
    if not sys.argv[1:] or sys.argv[1] != "--live" or len(sys.argv) not in (2, 3):
        raise SystemExit(
            "Explicit --live required: up to 16 calls using the saved Z.ai Coding Plan."
        )
    cfg = load_config()
    profile = get_profile(cfg, "zai-coding-plan")
    key = resolve_profile_api_key(cfg, "zai-coding-plan")
    if profile is None or not key.key:
        raise SystemExit("Saved Z.ai Coding Plan is unavailable. No fallback.")
    project = "alysis-coding-verification-" + uuid4().hex[:8]
    root = ROOT / "reports" / project
    root.mkdir(parents=True)
    log = (root / "service.log").open("w")
    command = [
        "docker",
        "--context",
        "desktop-linux",
        "compose",
        "--project-name",
        project,
        "-f",
        str(ROOT / "scripts/bots-dev/compose.yaml"),
    ]
    if len(sys.argv) == 3:
        # Optional operator-selected idle study networks. Reuse networking only;
        # this run still has new, isolated data volumes and random capabilities.
        prefix = sys.argv[2]
        if not prefix.startswith("alysis-bots-linux-") or not prefix.replace("-", "").isalnum():
            raise SystemExit("Only an existing Alysis Linux study network pair may be reused.")
        names = [prefix + "_models", prefix + "_workers"]
        info = json.loads(
            subprocess.check_output(
                ["docker", "--context", "desktop-linux", "network", "inspect", *names]
            )
        )
        if any(item["Containers"] for item in info) or [item["Internal"] for item in info] != [
            False,
            True,
        ]:
            raise SystemExit("Selected networks must be idle, with an isolated worker network.")
        overlay = root / "networks.json"
        overlay.write_text(
            json.dumps(
                {
                    "networks": {
                        "models": {"external": True, "name": names[0]},
                        "workers": {"external": True, "name": names[1]},
                    }
                }
            )
        )
        command += ["-f", str(overlay)]
    env = {
        **os.environ,
        "ALYSIS_BOTS_PORT": "7083",
        "ALYSIS_BOTS_SECCOMP": str(ROOT / "scripts/cloud-dev/seccomp.json"),
    }
    token, worker_token = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
    processes = []

    def start(args, payload):
        proc = subprocess.Popen(
            [*command, "run", "--rm", "-T", *args],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            env=env,
        )
        processes.append(proc)
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        proc.stdin.close()
        return proc

    client = httpx.Client(
        base_url="http://127.0.0.1:7083",
        timeout=10,
        trust_env=False,
        headers={"Authorization": "Bearer " + token},
    )

    def api(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    coding_name = project + "-coding"

    def linux(code):
        result = subprocess.run(
            ["docker", "--context", "desktop-linux", "exec", "-i", coding_name, "python", "-"],
            input=code,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        return result.stdout

    try:
        start(
            ["--service-ports", "--use-aliases", "--name", project + "-runtime", "bots"],
            {
                "token": token,
                "coding_token": worker_token,
                "api_key": key.key,
                "base_url": profile.base_url,
                "model": "glm-5.3-flash",
                "protocol": "openai_compat",
                "provider_key": "zai_coding_plan",
                "reasoning_effort": "low",
                "managed_workspaces": True,
                "max_calls": 16,
                "coding_calls": 8,
            },
        )
        until(lambda: api("GET", "/v1/status"), lambda value: value["worker_ready"])
        start(
            ["--name", coding_name, "coding"],
            {
                "url": "http://bots:7080",
                "token": worker_token,
                "base_url": profile.base_url,
                "model": "glm-5.3-flash",
                "verify": [],
            },
        )
        bot = api(
            "POST",
            "/v1/bots",
            json={
                "name": "Coding verification check",
                "purpose": "Maintain this synthetic Unicode project and report actual checks.",
            },
        )
        until(
            lambda: api("GET", "/v1/coding/workspaces"),
            lambda rows: any(row["bot_id"] == bot["id"] and row["online"] for row in rows),
        )
        workspace = "/var/lib/alysis/coding/workspaces/" + bot["id"]
        linux(
            f"from pathlib import Path\np=Path({workspace!r})\n"
            "(p/'notes.py').write_text('def normalize_text(text):\\n    return text.strip()\\n')\n"
            f"(p/'test_existing.py').write_text({TESTS!r})\n"
        )
        chat = api("POST", f"/v1/bots/{bot['id']}/chats", json={})
        app = ROOT.parent / "Alysis-Code-App-bots-dev"
        data = ROOT / ".dev" / project
        data.mkdir(mode=0o700)
        engine = ROOT / ".dev/bots-engine"
        if not engine.is_file():
            raise RuntimeError(
                "Run the normal development launcher once to create its engine wrapper."
            )
        (data / "runtime-supervisor.json").write_text(json.dumps({"customBinaryPath": str(engine)}))
        processes.append(
            subprocess.Popen(
                [
                    str(app / "node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"),
                    str(app),
                ],
                env={
                    **os.environ,
                    "ALYSIS_BOTS_URL": "http://127.0.0.1:7083",
                    "ALYSIS_BOTS_TOKEN": token,
                    "ALYSIS_E2E": "1",
                    "ALYSIS_E2E_USER_DATA": str(data),
                    "ALYSIS_ALLOW_MULTIPLE_INSTANCES": "1",
                    "ALYSIS_APP_REPO": str(ROOT),
                    "ALYSIS_APP_PYTHON": sys.executable,
                    "ALYSIS_APP_CLI": str(engine),
                    "PYTHONPATH": str(ROOT / "src"),
                },
                stdout=log,
                stderr=log,
            )
        )
        task = api(
            "POST",
            f"/v1/chats/{chat['id']}/tasks",
            json={
                "request_id": uuid4().hex,
                "message": "Fix normalize_text in notes.py in your coding workspace: return Unicode NFC "
                "while preserving all spaces, line endings and empty input. Inspect and run "
                "the existing stdlib tests, which must remain unchanged. Add a separate test "
                "for already-normalized text if useful. Complete the change and report actual "
                "check results. No external accounts, publishing or network access.",
            },
        )
        print(
            json.dumps(
                {
                    "state": "running",
                    "project": project,
                    "bot": bot["id"],
                    "report": str(root),
                    "port": 7083,
                }
            ),
            flush=True,
        )
        final = until(
            lambda: api("GET", f"/v1/tasks/{task['id']}"),
            lambda value: value["status"] in {"completed", "failed", "cancelled"},
            600,
        )
        observed = json.loads(
            linux(
                "import json,hashlib\nfrom pathlib import Path\n"
                "from alysis_code.bots.coding_worker import ScopedLinuxRunner\n"
                f"p=Path({workspace!r})\n"
                "r=ScopedLinuxRunner(workspace=p,close_stdin=True).run(root=p,cwd=p,"
                "cmd='python -m unittest discover -v',timeout_s=30)\n"
                "print(json.dumps({'exit_code':r.returncode,'output':r.stdout+r.stderr,"
                "'tests_sha256':hashlib.sha256((p/'test_existing.py').read_bytes()).hexdigest()}))\n"
            )
        )
        receipt = {
            "kind": "Live GLM; real isolated Linux, bwrap and desktop",
            "project": project,
            "bot_id": bot["id"],
            "task": final,
            "independent_check": observed,
            "usage": api("GET", "/v1/status")["usage"],
            "existing_tests_unchanged": observed["tests_sha256"]
            == hashlib.sha256(TESTS.encode()).hexdigest(),
        }
        (root / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        memory = api(
            "POST",
            f"/v1/bots/{bot['id']}/memories",
            json={
                "title": "Synthetic review marker",
                "content": "The current review marker is Παλαιό-17.",
            },
        )
        calls_before = api("GET", "/v1/status")["usage"]["calls"]
        memory_task = api(
            "POST",
            f"/v1/chats/{chat['id']}/tasks",
            json={
                "request_id": uuid4().hex,
                "message": "What is my current saved review marker? Use approved memory and reply with the exact marker.",
            },
        )
        until(
            lambda: api("GET", "/v1/status")["usage"],
            lambda usage: usage["calls"] > calls_before and usage["unconfirmed_calls"] > 0,
        )
        api(
            "PUT",
            f"/v1/bots/{bot['id']}/memories/{memory['id']}",
            json={
                "title": "Synthetic review marker",
                "content": "The current review marker is Νέο-29.",
            },
        )
        memory_final = until(
            lambda: api("GET", f"/v1/tasks/{memory_task['id']}"),
            lambda value: value["status"] in {"completed", "failed", "cancelled"},
            180,
        )
        receipt["memory_task"] = memory_final
        receipt["memory_conversation"] = api("GET", f"/v1/chats/{chat['id']}")["messages"]
        receipt["usage"] = api("GET", "/v1/status")["usage"]
        receipt["acceptance"] = {
            "original_tests_pass_unchanged": observed["exit_code"] == 0
            and receipt["existing_tests_unchanged"],
            "memory_refresh_discarded_stale_response": any(
                event["kind"] == "memory_refreshed" and event["data"].get("response_discarded")
                for event in memory_final["events"]
            ),
            "memory_reply_uses_current_value": memory_final["status"] == "completed"
            and "Νέο-29" in (memory_final["output"] or "")
            and "Παλαιό-17" not in (memory_final["output"] or ""),
        }
        (root / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "state": "finished",
                    "task_status": final["status"],
                    "tests_exit": observed["exit_code"],
                    "existing_tests_unchanged": receipt["existing_tests_unchanged"],
                    "acceptance": receipt["acceptance"],
                    "usage": receipt["usage"],
                    "report": str(root),
                }
            ),
            flush=True,
        )
        print(
            "Ready for native desktop inspection. Ctrl+C stops only this isolated study.",
            flush=True,
        )
        while True:
            time.sleep(1)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        client.close()
        log.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
