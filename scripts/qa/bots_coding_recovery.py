"""Zero-provider fault injection against real Linux service + AlysisCode worker.

The explicit model fixture asks for a slow sandbox command. Tests cover Stop,
worker death, orphan cleanup on restart, and no replay. No user state is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = """
from pathlib import Path
import uvicorn
from alysis_code.bots.app import Settings,create_app
from alysis_code.llm.types import LLMResponse,ToolCall
import json,sys
settings=json.loads(sys.stdin.readline())
seen=set()
class ModelFixture:
    model='coding-recovery-fixture'
    supports_tool_calling=True
    def __init__(self,task_id): self.task_id=task_id
    def chat(self,**kw):
        names={t['function']['name'] for t in kw.get('tools') or []}
        if 'coding_task' in names:
            return LLMResponse('',[ToolCall('delegate','coding_task',{'brief':'Run the authorized slow sandbox cancellation check.'})],{})
        if 'shell_run' in names and self.task_id not in seen:
            seen.add(self.task_id)
            cmd='touch started; sleep 45; touch finished'
            return LLMResponse('',[ToolCall('slow','shell_run',{'cmd':cmd,'timeout_s':60})],{})
        return LLMResponse('The actual receipt controls the result.',[],{})
app=create_app(Settings(database=Path('/state/bots.db'),**settings),client_factory=ModelFixture)
uvicorn.run(app,host='0.0.0.0',port=7080,access_log=False,log_level='warning')
"""


def until(read, accepts, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            value = read()
            if accepts(value):
                return value
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("Fault injection did not reach expected state")


def main():
    root = Path(tempfile.mkdtemp(prefix="alysis-coding-recovery-")).resolve()
    workspace = root / "project"
    workspace.mkdir()
    state = root / "state"
    state.mkdir()
    state.chmod(0o777)
    fixture = root / "fixture.py"
    fixture.write_text(FIXTURE)
    token, worker_token = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
    name = "alysis-coding-recovery-" + uuid4().hex[:10]
    scope = hashlib.sha256(str(workspace).encode()).hexdigest()[:32]
    report = {
        "kind": "Provider fixture; real Linux controller, AlysisCode session and Docker shell",
        "root": str(root),
        "checks": [],
    }
    server = subprocess.Popen(
        [
            "docker",
            "--context",
            "desktop-linux",
            "run",
            "--rm",
            "--name",
            name,
            "-i",
            "--init",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=32m,mode=1777",
            "-u",
            "1000:1000",
            "-p",
            "127.0.0.1:7082:7080",
            "-v",
            f"{fixture}:/opt/fixture.py:ro",
            "-v",
            f"{state}:/state:rw",
            "alysis-bots-dev:local",
            "python",
            "/opt/fixture.py",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    server.stdin.write(
        (
            json.dumps(
                {
                    "token": token,
                    "coding_token": worker_token,
                    "api_key": "fixture-only",
                    "base_url": "https://example.invalid/v1",
                    "model": "fixture",
                    "max_calls": 16,
                }
            )
            + "\n"
        ).encode()
    )
    server.stdin.close()
    worker = None
    log = (root / "worker.log").open("w")
    client = httpx.Client(
        base_url="http://127.0.0.1:7082",
        headers={"Authorization": "Bearer " + token},
        timeout=5,
        trust_env=False,
    )

    def api(method, path, **kw):
        r = client.request(method, path, **kw)
        r.raise_for_status()
        return r.json()

    def start_worker():
        proc = subprocess.Popen(
            [sys.executable, "-m", "alysis_code.bots.coding_worker"],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        payload = {
            "url": "http://127.0.0.1:7082",
            "token": worker_token,
            "workspace": str(workspace),
            "state": str(root / "worker-state"),
            "model": "glm-5.3-flash",
            "base_url": "https://api.z.ai/api/coding/paas/v4",
            "image": "alysis-bots-dev:local",
            "verify": [],
        }
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        proc.stdin.close()
        return proc

    try:
        until(lambda: api("GET", "/v1/status"), lambda v: v["worker_ready"])
        worker = start_worker()
        until(lambda: api("GET", "/v1/coding/workspaces"), lambda v: v and v[0]["online"])
        bot = api(
            "POST",
            "/v1/bots",
            json={"name": "Recovery fixture", "purpose": "Synthetic interruption check"},
        )
        api("PUT", f"/v1/bots/{bot['id']}/coding", json={"workspace_id": scope})
        chat = api("POST", f"/v1/bots/{bot['id']}/chats", json={})

        def send():
            return api(
                "POST",
                f"/v1/chats/{chat['id']}/tasks",
                json={"message": "Run the synthetic slow coding check", "request_id": uuid4().hex},
            )

        first = send()
        until(lambda: (workspace / "started").exists(), bool, 60)
        api("POST", f"/v1/tasks/{first['id']}/cancel", json={})
        until(lambda: api("GET", f"/v1/tasks/{first['id']}"), lambda v: v["status"] == "cancelled")

        # Wait until the actual worker completed cancellation and no child remains.
        def containers():
            return subprocess.run(
                ["docker", "ps", "-q", "--filter", "name=^/alysis-sbx-" + scope + "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        until(containers, lambda v: not v)
        assert not (workspace / "finished").exists()
        report["checks"].append("Stop cancels the running Linux command; no delayed write")
        (workspace / "started").unlink()
        second = send()
        until(lambda: (workspace / "started").exists(), bool, 60)
        job = api("GET", f"/v1/tasks/{second['id']}")["coding_job"]
        worker.kill()
        worker.wait(timeout=5)
        until(
            lambda: api("GET", f"/v1/tasks/{second['id']}"),
            lambda v: v.get("coding_job", {}).get("status") == "interrupted",
            25,
        )
        worker = start_worker()
        until(lambda: api("GET", "/v1/coding/workspaces"), lambda v: v and v[0]["online"])
        until(containers, lambda v: not v)
        before = api("GET", "/v1/status")["usage"]["calls"]
        time.sleep(2)
        final = api("GET", f"/v1/tasks/{second['id']}")
        assert (
            final["coding_job"]["id"] == job["id"]
            and final["coding_job"]["status"] == "interrupted"
        )
        assert not (workspace / "finished").exists()
        assert api("GET", "/v1/status")["usage"]["calls"] == before
        report["checks"] += [
            "Worker death returns an interrupted receipt",
            "Replacement removes owned orphan containers before claiming",
            "Old assignment is not replayed and cannot become success",
        ]
        report["calls"] = api("GET", "/v1/status")["usage"]["calls"]
        report["actual_provider_calls"] = 0
        print(json.dumps(report, indent=2))
    finally:
        if worker and worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=25)
        subprocess.run(
            ["docker", "--context", "desktop-linux", "stop", "--timeout", "10", name],
            capture_output=True,
        )
        server.wait(timeout=20)
        log.close()
        client.close()
        (root / "report.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
