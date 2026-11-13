"""Real Linux controller/workspaces/Git + Electron disconnect; fixture model only."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
from bots_coding_recovery import until

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT.parent / "Alysis-Code-App-bots-dev"
FIXTURE = """
import json,sys
from pathlib import Path
import uvicorn
from alysis_code.bots.app import Settings,create_app
from alysis_code.llm.types import LLMResponse,ToolCall
seen=set()
class Fixture:
    model='linux-fixture'
    supports_tool_calling=True
    def __init__(self,task_id): self.task_id=task_id
    def chat(self,**kw):
        names={t['function']['name'] for t in kw.get('tools') or []}
        if 'coding_task' in names:
            return LLMResponse('',[ToolCall('start','coding_task',{'brief':'Perform the authorized synthetic local Git workflow.'})],{})
        if 'shell_run' in names and self.task_id not in seen:
            seen.add(self.task_id)
            cmd="git clone seed.git project && cd project && git switch -c bot-test && printf 'Linux cloud workspace\\n' > result.txt && git add result.txt && git -c user.name='Alysis Test' -c user.email='test@example.invalid' commit -m 'Synthetic Linux check' && git show --stat HEAD && sleep 12"
            return LLMResponse('',[ToolCall('git','shell_run',{'cmd':cmd,'timeout_s':45})],{})
        return LLMResponse('Local Git work has returned. The saved diff and checks govern its actual outcome.',[],{})
settings=Settings(database=Path('/var/lib/alysis/bots/bots.sqlite3'),**json.loads(sys.stdin.readline()))
uvicorn.run(create_app(settings,client_factory=Fixture),host='0.0.0.0',port=7080,access_log=False,log_level='warning')
"""


def main():
    root = Path(tempfile.mkdtemp(prefix="alysis-bots-linux-")).resolve()
    fixture = root / "fixture.py"
    fixture.write_text(FIXTURE)
    project = "alysis-bots-linux-" + uuid4().hex[:8]
    service, coding_name = project + "-runtime", project + "-coding"
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
    env = {
        **os.environ,
        "ALYSIS_BOTS_PORT": "7082",
        "ALYSIS_BOTS_SECCOMP": str(ROOT / "scripts/cloud-dev/seccomp.json"),
    }
    token, worker_token = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
    log = (root / "services.log").open("w")
    report = {
        "kind": "Fixture model; real Linux controller, bwrap, Git and Electron",
        "checks": [],
        "root": str(root),
        "project": project,
    }

    def start(args, payload):
        proc = subprocess.Popen(
            [*command, "run", "--rm", "-T", *args],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            env=env,
        )
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        proc.stdin.close()
        return proc

    server = start(
        [
            "--service-ports",
            "--use-aliases",
            "--name",
            service,
            "-v",
            f"{fixture}:/opt/fixture.py:ro",
            "bots",
            "python",
            "/opt/fixture.py",
        ],
        {
            "token": token,
            "coding_token": worker_token,
            "api_key": "fixture-only",
            "base_url": "https://example.invalid/v1",
            "model": "fixture",
            "managed_workspaces": True,
            "max_calls": 12,
        },
    )
    worker = None
    client = httpx.Client(
        base_url="http://127.0.0.1:7082",
        headers={"Authorization": "Bearer " + token},
        timeout=5,
        trust_env=False,
    )

    def api(method, path, **kw):
        response = client.request(method, path, **kw)
        response.raise_for_status()
        return response.json()

    def linux(code):
        result = subprocess.run(
            ["docker", "--context", "desktop-linux", "exec", "-i", coding_name, "python", "-"],
            input=code,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:])
        return result.stdout

    def start_worker():
        return start(
            ["--name", coding_name, "coding"],
            {
                "url": "http://bots:7080",
                "token": worker_token,
                "model": "fixture",
                "base_url": "https://example.invalid/v1",
                "verify": [],
            },
        )

    def desktop(bot, review=False):
        result = subprocess.run(
            ["node", "tests/e2e-bots.mjs"],
            cwd=APP,
            env={
                **os.environ,
                "ALYSIS_BOTS_E2E_LIVE": "1",
                "ALYSIS_BOTS_E2E_LINUX": "1",
                "ALYSIS_BOTS_LINUX_BOT": bot["id"],
                "ALYSIS_BOTS_LINUX_REVIEW": "1" if review else "0",
                "ALYSIS_BOTS_URL": "http://127.0.0.1:7082",
                "ALYSIS_BOTS_TOKEN": token,
            },
            stdout=log,
            stderr=log,
            timeout=120,
        )
        assert result.returncode == 0, "Desktop check failed; inspect the local test log"

    try:
        until(lambda: api("GET", "/v1/status"), lambda v: v["worker_ready"])
        worker = start_worker()
        bot = api(
            "POST",
            "/v1/bots",
            json={
                "name": "Linux persistence check",
                "purpose": "Synthetic local Git test",
                "memory_enabled": False,
            },
        )
        peer = api(
            "POST",
            "/v1/bots",
            json={
                "name": "Isolation peer",
                "purpose": "Separate workspace",
                "memory_enabled": False,
            },
        )
        until(
            lambda: api("GET", "/v1/coding/workspaces"),
            lambda v: len(v) == 2 and all(w["online"] for w in v),
        )
        workspace = f"/var/lib/alysis/coding/workspaces/{bot['id']}"
        peer_path = f"/var/lib/alysis/coding/workspaces/{peer['id']}"
        private = f"/var/lib/alysis/coding/sessions/{bot['id']}"
        linux(f"""from pathlib import Path
import subprocess
root=Path({workspace!r})
subprocess.run(['git','init','--bare',str(root/'seed.git')],check=True,capture_output=True)
Path({peer_path!r},'peer.txt').write_text('synthetic peer fixture')
Path({private!r},'controller.txt').write_text('synthetic private state')
""")
        probe = f"""import os,socket
assert not os.path.exists({peer_path!r})
assert not os.path.exists({private!r})
assert not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/Users')
assert not os.environ.get('ALYSIS_BOTS_TOKEN')
assert not os.environ.get('ALYSIS_API_KEY')
try:
 socket.create_connection(('1.1.1.1',443),timeout=1)
except OSError: pass
else: raise AssertionError('network escaped')
print('isolated')
"""
        import shlex

        output = linux(f"""from pathlib import Path
from alysis_code.bots.coding_worker import ScopedLinuxRunner
root=Path({workspace!r})
r=ScopedLinuxRunner(workspace=root,close_stdin=True).run(root=root,cwd=root,cmd={"python -c " + shlex.quote(probe)!r},timeout_s=10)
assert r.returncode==0,(r.stdout,r.stderr)
print(r.stdout)
""")
        assert "isolated" in output
        report["checks"].append(
            "Real bwrap denies peer/state/host/socket access and outbound network"
        )
        cancelled = linux(f"""from pathlib import Path
import threading
from alysis_code.bots.coding_worker import ScopedLinuxRunner
from alysis_code.process_reaping import CommandCancelledError
root=Path({workspace!r})
cancel=threading.Event()
threading.Timer(0.3,cancel.set).start()
try:
 ScopedLinuxRunner(workspace=root,close_stdin=True).run(root=root,cwd=root,cmd='sleep 5; touch cancel-late',timeout_s=10,cancel_check=cancel.is_set)
except CommandCancelledError: pass
else: raise AssertionError('Command did not cancel')
assert not (root/'cancel-late').exists()
print('cancelled')
""")
        assert "cancelled" in cancelled
        report["checks"].append(
            "Bwrap cancellation kills the actual command before its delayed write"
        )
        desktop(bot)
        closed_at = time.time()
        chat = api("GET", f"/v1/bots/{bot['id']}/chats")[0]
        task = api("GET", f"/v1/chats/{chat['id']}")["tasks"][0]
        assert task["status"] == "waiting"
        final = until(
            lambda: api("GET", f"/v1/tasks/{task['id']}"),
            lambda v: v["status"] in ("completed", "failed", "interrupted"),
            90,
        )
        assert final["status"] == "completed", final.get("error")
        assert final["updated_at"] > closed_at
        git = linux(f"""import json,subprocess
from pathlib import Path
root=Path({workspace!r})/'project'
def git(*args): return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
print(json.dumps({{'branch':git('branch','--show-current'),'commits':git('rev-list','--count','HEAD'),'file':(root/'result.txt').read_text(),'status':git('status','--porcelain')}}))
""")
        report["git"] = json.loads(git)
        assert report["git"] == {
            "branch": "bot-test",
            "commits": "1",
            "file": "Linux cloud workspace\n",
            "status": "",
        }
        report["checks"].append(
            "Real Linux clone, branch, file, local commit and inspection finish after Electron closes"
        )
        desktop(bot, True)
        report["checks"].append(
            "Reopened desktop displays the saved completion without another model call"
        )
        calls = api("GET", "/v1/status")["usage"]["calls"]
        subprocess.run(
            ["docker", "--context", "desktop-linux", "stop", "--time", "20", coding_name],
            check=True,
            capture_output=True,
        )
        worker.wait(timeout=25)
        until(
            lambda: api("GET", "/v1/coding/workspaces"), lambda v: all(not w["online"] for w in v)
        )
        worker = start_worker()
        until(
            lambda: api("GET", "/v1/coding/workspaces"),
            lambda v: all(w["online"] for w in v) and worker.poll() is None,
        )
        until(
            lambda: linux(
                f"from pathlib import Path; print(Path({workspace!r},'project/result.txt').read_text())"
            ),
            lambda v: "Linux cloud workspace" in v,
        )
        time.sleep(3)
        assert api("GET", "/v1/status")["usage"]["calls"] == calls
        report["checks"].append(
            "Worker replacement retains per-bot files and Git history without replay"
        )
        report["usage"] = api("GET", "/v1/status")["usage"]
        report["actual_provider_calls"] = 0
        report["outcome"] = final["coding_job"]["result"]["outcome"]
        print(json.dumps(report, indent=2))
    finally:
        for name, process in ((coding_name, worker), (service, server)):
            if process and process.poll() is None:
                subprocess.run(
                    ["docker", "--context", "desktop-linux", "stop", "--time", "20", name],
                    capture_output=True,
                )
                process.wait(timeout=25)
        client.close()
        log.close()
        (root / "report.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
