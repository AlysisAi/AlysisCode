"""Real local lifecycle/portability proof. No cloud, model or native app calls.

Use --execute-local explicitly. Own computers are stopped/deleted in finally;
normal labs and their volumes are never selected. Only synthetic data is used.
The provider is exercised through the same contract the Azure adapter implements.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from alysis_code.bots.compute import (  # noqa: E402
    ComputeError,
    ComputerIdentity,
    ComputerSpec,
    wait_for,
)
from alysis_code.bots.compute_docker import DockerComputers  # noqa: E402


def checked(provider, ref, code, *args):
    result = provider.run(ref, ["python", "-c", code, *args])
    if result.exit_code != 0:
        # This opt-in study has no credentials, personal mounts or external
        # input. Retain bounded synthetic diagnostics, unlike product errors.
        raise RuntimeError(
            f"Synthetic guest command failed (exit {result.exit_code}): {result.stderr[-8192:]}"
        )
    return result.stdout


def qualify(provider, image, report, *, desktop=False):
    refs = []
    identity = ComputerIdentity("portable-study-" + uuid4().hex, "synthetic-bot", uuid4().hex)
    allocations = [identity]
    report.update({"evidence": "real Docker/Linux, no model", "checks": [], "cleanup": []})
    try:
        refs.append(provider.create(identity, ComputerSpec(image)))
        first = refs[0]
        report["computers"] = [asdict(first)]
        wait_for(provider, first, "running")
        report["checks"].append("running state independently observed")
        assert provider.find(identity) == first
        report["checks"].append("allocation reconciled without second creation")

        if desktop:
            native = json.loads(
                checked(
                    provider,
                    first,
                    """import hashlib,json
from pathlib import Path
from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
workspace=Path('/var/lib/alysis/workspace/native-proof')
workspace.mkdir(mode=0o700)
computer=OfflineLinuxDesktop('synthetic-native',workspace)
try:
 result=computer.call('synthetic-native','run',{'command': 'printf "portable desktop"', 'timeout': 5})
 assert result['exit_code']==0 and result['output']=='portable desktop',result
 pixels=computer.capture()
 assert pixels.startswith(b'\\x89PNG\\r\\n\\x1a\\n')
 print(json.dumps({'command_exit':result['exit_code'],'png_bytes':len(pixels),
  'png_sha256':hashlib.sha256(pixels).hexdigest()}))
finally:computer.close()
""",
                )
            )
            report["native"] = native
            report["checks"].append(
                "actual isolated desktop command and X11 screenshot in candidate image"
            )

        payload = ("Saved Alysis work — Ελλάδα 日本語\n" + uuid4().hex).encode()
        expected = hashlib.sha256(payload).hexdigest()
        filename = "work-Ω-日本語.bin"
        write = """import base64,sys
from pathlib import Path
p=Path('/var/lib/alysis/workspace')/sys.argv[1]
with p.open('xb') as f: f.write(base64.b64decode(sys.argv[2],validate=True))
"""
        read = """import base64,sys
from pathlib import Path
print(base64.b64encode((Path('/var/lib/alysis/workspace')/sys.argv[1]).read_bytes()).decode())
"""
        checked(provider, first, write, filename, base64.b64encode(payload).decode())

        probe = (ROOT / "scripts/qa/bots_sandbox_state_probe.py").read_bytes()
        checked(
            provider,
            first,
            """import base64,sys
from pathlib import Path
Path('/tmp/state-probe.py').write_bytes(base64.b64decode(sys.argv[1],validate=True))
""",
            base64.b64encode(probe).decode(),
        )
        launch = """import subprocess,sys,time,urllib.request
subprocess.Popen([sys.executable,'/tmp/state-probe.py','serve'],stdin=subprocess.DEVNULL,
 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(50):
 try:
  with opener.open('http://127.0.0.1:8769/state',timeout=.2) as response:
   print(response.read().decode());break
 except OSError:time.sleep(.05)
else:raise RuntimeError('probe did not start')
"""
        before = json.loads(checked(provider, first, launch))
        wait_for(provider, first, "running")
        stopped = provider.stop(first)
        assert stopped.state == "stopped"
        assert stopped.resume_kind == "disk-restart"
        try:
            provider.run(first, ["true"])
        except ComputeError:
            pass
        else:
            raise AssertionError("Stopped computer executed or implicitly resumed")
        provider.start(first)
        wait_for(provider, first, "running")
        old_process = provider.run(first, ["python", "/tmp/state-probe.py", "observe"])
        assert old_process.exit_code != 0
        after = json.loads(checked(provider, first, launch))
        assert before["disk_sha256"] == after["disk_sha256"]
        assert before["memory_sha256"] != after["memory_sha256"]
        report["checks"].append("disk survives stop/start; prior process memory does not")
        report["restart"] = {"before": before, "after": after}

        exported = base64.b64decode(checked(provider, first, read, filename).strip(), validate=True)
        assert hashlib.sha256(exported).hexdigest() == expected
        second_identity = replace(identity, allocation_id=uuid4().hex)
        allocations.append(second_identity)
        refs.append(provider.create(second_identity, ComputerSpec(image)))
        second = refs[1]
        report["computers"].append(asdict(second))
        # No shared volume, snapshot or first computer's root filesystem.
        absent = provider.run(second, ["test", "-e", "/var/lib/alysis/workspace/" + filename])
        assert absent.exit_code == 1
        checked(provider, second, write, filename, base64.b64encode(exported).decode())
        restored = base64.b64decode(
            checked(provider, second, read, filename).strip(), validate=True
        )
        assert hashlib.sha256(restored).hexdigest() == expected
        report["checks"].append("saved file restored exactly into independent fresh computer")
        report["file"] = {"bytes": len(restored), "sha256": expected}

        foreign = replace(second, identity=replace(identity, owner_id="different-owner"))
        try:
            provider.run(foreign, ["true"])
        except ComputeError:
            pass
        else:
            raise AssertionError("Foreign owner was accepted")
        report["checks"].append("foreign owner rejected before execution")
        isolation = provider._inspect(second.resource_id)
        assert isolation["HostConfig"]["NetworkMode"] == "none"
        assert not isolation["Mounts"] and not isolation["HostConfig"]["PortBindings"]
        report["checks"].append("no network, host mounts or exposed ports")
        report["passed"] = True
    finally:
        for allocation in reversed(allocations):
            try:
                ref = provider.find(allocation)
                if ref is None:
                    continue
                status = provider.status(ref)
                if status.state == "running":
                    provider.stop(ref)
                deleted = provider.delete(ref)
                report["cleanup"].append({"resource_id": ref.resource_id, "state": deleted.state})
            except Exception as exc:
                report["cleanup"].append(
                    {"allocation_id": allocation.allocation_id, "error": type(exc).__name__}
                )
                report["passed"] = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-local", action="store_true")
    parser.add_argument("--context", default="desktop-linux")
    parser.add_argument("--image", required=True)
    parser.add_argument("--namespace-profile", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.execute_local:
        parser.error("Local resource creation requires --execute-local.")
    report = {"passed": False, "external_model_calls": 0, "cloud_operations": 0}
    try:
        qualify(
            DockerComputers(args.context, namespace_profile=args.namespace_profile),
            args.image,
            report,
            desktop=args.namespace_profile is not None,
        )
    except Exception as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {k: report[k] for k in ("passed", "external_model_calls", "cloud_operations")}
            )
        )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
