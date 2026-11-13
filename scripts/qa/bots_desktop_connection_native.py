"""Mac UI + isolated Linux VM desktop-connection qualification.

Build with the app's scripts/qa/build-bots-host-preview.mjs, then select that
checkout explicitly with --app. Run with --live for saved
OpenCode Go GLM-5.3-flash/low only. The default is explicitly scripted.
--local-service uses loopback HTTP on this Mac when Docker is unavailable;
that mode does not qualify Linux isolation or remote TLS transport.
The VM has no mount of the Mac fixture. TLS private keys and service/model
credentials travel over stdin and stay in memory; only the public CA is saved.
Control file commands: start, stop-test, status, restart-app, restart-service,
revoke, quit. All UI approvals are manual/CUA. Data is retained on shutdown.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def worker():
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS
    from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

    config = json.loads(sys.stdin.readline())
    settings = Settings(
        Path(config.get("database", "/evidence/bots.db")),
        config["token"],
        config["key"],
        config["url"],
        "glm-5.3-flash" if config["live"] else "scripted-desktop",
        owner="desktop-connection-qa",
        reasoning_effort="low",
    )
    fixture = Path(config["fixture"])
    if not config.get("local_service"):
        assert not fixture.exists(), "The VM must not see the Mac fixture directly"

    class Model:
        model = "scripted-desktop"
        supports_tool_calling = True

        def __init__(self, task):
            self.task, self.stage = task, 0

        def chat(self, **kwargs):
            if kwargs.get("tools") is None:
                assessment = {"decision": "ready", "reason": "Scripted QA reply review"}
                if kwargs["messages"][0]["content"] != REVIEW_INSTRUCTIONS:
                    assessment = {
                        "decision": "allow",
                        "risk": "medium",
                        "user_authorization": "high",
                        "reason": "The user requested this isolated fixture operation.",
                    }
                return LLMResponse(
                    json.dumps(assessment),
                    [],
                    {},
                    usage=LLMUsage(1, 1, 2),
                )
            self.stage += 1
            task = app.state.store.task(settings.owner, self.task)
            device = app.state.runtime.local_computers.devices()[0]["id"]
            if "QA stop-test" in task["message"] and self.stage == 1:
                name, arguments = (
                    "local_command",
                    {
                        "device_id": device,
                        "cwd": str(fixture),
                        "command": "printf started > stop-started.txt; sleep 300; printf bad > stop-late.txt",
                    },
                )
            elif self.stage == 1:
                name, arguments = (
                    "local_command",
                    {
                        "device_id": device,
                        "cwd": str(fixture),
                        "command": "set -eu; set -C; shasum -a 256 input.bin > command.txt; cat command.txt",
                    },
                )
            elif self.stage == 2:
                name, arguments = (
                    "import_local_file",
                    {
                        "device_id": device,
                        "path": str(fixture / "input.bin"),
                        "name": "desktop-roundtrip.bin",
                    },
                )
            elif self.stage == 3:
                file = app.state.store.list_files(settings.owner, task["bot_id"])[0]
                name, arguments = (
                    "export_local_file",
                    {
                        "device_id": device,
                        "path": str(fixture / "roundtrip.bin"),
                        "file_id": file["id"],
                    },
                )
            else:
                return LLMResponse(
                    "Command and both transfers returned successful receipts with matching SHA-256 hashes.",
                    [],
                    {},
                    usage=LLMUsage(1, 1, 2),
                )
            return LLMResponse(
                "", [ToolCall(uuid4().hex, name, arguments)], {}, usage=LLMUsage(1, 1, 2)
            )

    clients = {}
    app = create_app(
        settings,
        client_factory=None
        if config["live"]
        else lambda task: clients.setdefault(task, Model(task)),
    )
    if config.get("local_service"):
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=config["port"],
            log_level="error",
            access_log=False,
        )
        return
    descriptors = []
    for name in ("cert", "private_key"):
        descriptor = os.memfd_create("alysis-qa-tls")
        os.write(descriptor, config[name].encode())
        descriptors.append(f"/proc/self/fd/{descriptor}")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=7080,
        ssl_certfile=descriptors[0],
        ssl_keyfile=descriptors[1],
        log_level="error",
        access_log=False,
    )


def certificate():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Alysis isolated desktop QA")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def main():
    import httpx

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True, help="App source checkout to test")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--local-service", action="store_true", help="Mac loopback service, no VM/TLS qualification"
    )
    parser.add_argument("--port", type=int, default=7098)
    parser.add_argument("--preview", type=Path, help="Explicit isolated app preview directory")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    app_root = args.app.resolve()
    build = args.preview.resolve() if args.preview else app_root / ".dev/bots-host-preview"
    if not (build / "out/main/index.js").is_file() or not (build / "package.json").is_file():
        parser.error("Run node scripts/qa/build-bots-host-preview.mjs in the selected app first.")
    electron = subprocess.check_output(
        ["node", "-p", "require('electron')"], cwd=app_root, text=True
    ).strip()
    run = uuid4().hex[:12]
    folder = root / ".dev/bots-desktop-connection" / run
    folder.mkdir(parents=True)
    fixture = Path(tempfile.mkdtemp(prefix="alysis-desktop-qa-"))
    payload = secrets.token_bytes(530013) + "Καλημέρα 東京".encode()
    (fixture / "input.bin").write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    cert, private_key = certificate() if not args.local_service else (None, None)
    ca = folder / "public-ca.pem"
    if cert:
        ca.write_text(cert)
    config = {
        "live": args.live,
        "fixture": str(fixture),
        "token": secrets.token_urlsafe(40),
        "key": "scripted",
        "url": "https://example.invalid",
        "cert": cert,
        "private_key": private_key,
        "local_service": args.local_service,
        "port": args.port,
    }
    if args.live:
        from alysis_code.config import load_config, resolve_profile_api_key
        from alysis_code.profiles import get_profile

        profile_config = load_config()
        profile = get_profile(profile_config, "opencode")
        if not profile or profile.base_url != "https://opencode.ai/zen/go/v1":
            raise RuntimeError("Saved Go profile unavailable; no fallback")
        config.update(
            key=resolve_profile_api_key(profile_config, profile.name).key, url=profile.base_url
        )
        if not config["key"]:
            raise RuntimeError("Saved Go credential unavailable; no fallback")
    address = f"{'http' if args.local_service else 'https'}://127.0.0.1:{args.port}"
    client = httpx.Client(
        base_url=address,
        verify=ssl.create_default_context(cafile=str(ca)) if cert else True,
        headers={"Authorization": "Bearer " + config["token"]},
        timeout=15,
    )
    owned, service, native = [], None, None

    def log(process, name):
        def consume():
            with (folder / name).open("a") as output:
                for line in process.stdout:
                    output.write(
                        line.replace(config["token"], "[redacted]").replace(
                            config["key"], "[redacted]"
                        )
                    )
                    output.flush()

        threading.Thread(target=consume, daemon=True).start()

    def start_service():
        name = f"alysis-bots-desktop-qa-{run}-{len(owned)}"
        if not args.local_service:
            owned.append(name)
        command = (
            [sys.executable, str(Path(__file__).resolve()), "--worker"]
            if args.local_service
            else [
                "docker",
                "run",
                "-i",
                "--name",
                name,
                "--cpus",
                "2",
                "--memory",
                "1g",
                "-p",
                f"127.0.0.1:{args.port}:7080",
                "-v",
                f"{root / 'src'}:/source:ro",
                "-v",
                f"{Path(__file__).resolve()}:/qa.py:ro",
                "-v",
                f"{folder}:/evidence",
                "-e",
                "PYTHONPATH=/source",
                "alysis-bots-cloud-control-20261023:qa",
                "python",
                "/qa.py",
                "--worker",
            ]
        )
        worker_config = {**config}
        if args.local_service:
            worker_config["database"] = str(folder / "bots.db")
        process = subprocess.Popen(
            command,
            env={**os.environ, "PYTHONPATH": str(root / "src")},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        process.stdin.write(json.dumps(worker_config) + "\n")
        process.stdin.flush()
        log(process, "service.log")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                if client.get("/v1/status").status_code == 200:
                    return process
            except httpx.HTTPError:
                pass
            if process.poll() is not None:
                raise RuntimeError("Isolated service failed; inspect redacted service.log")
            time.sleep(0.2)
        raise RuntimeError("Isolated service did not become ready")

    def api(method, path, body=None):
        response = client.request(method, path, json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"QA API request failed: {method} {path}: {response.status_code}")
        return response.json()

    def start_app():
        cli = folder / "engine"
        cli.write_text(
            "#!/bin/sh\nexport PYTHONPATH="
            + shlex.quote(str(root / "src"))
            + "\nexec "
            + shlex.quote(sys.executable)
            + ' -m alysis_code "$@"\n'
        )
        cli.chmod(0o700)
        # Main rebuilds the engine environment from this approved setting.
        # An inherited ALYSIS_APP_CLI alone is filtered from runtime launches.
        profile = folder / "app-profile"
        profile.mkdir(exist_ok=True, mode=0o700)
        (profile / "runtime-supervisor.json").write_text(
            json.dumps(
                {
                    "permissionModeDefault": "workspace-write",
                    "networkAccess": False,
                    "useLoginShellEnv": False,
                    "customBinaryPath": str(cli),
                }
            )
        )
        (profile / "agent-import.json").write_text(json.dumps({"version": 1, "dismissed": True}))
        env = {
            **os.environ,
            "ALYSIS_E2E": "1",
            "ALYSIS_E2E_USER_DATA": str(profile),
            "ALYSIS_ALLOW_MULTIPLE_INSTANCES": "1",
            "ALYSIS_APP_CLI": str(cli),
            "PYTHONPATH": str(root / "src"),
            "ALYSIS_BOTS_URL": address,
            "ALYSIS_BOTS_TOKEN": config["token"],
            **({"NODE_EXTRA_CA_CERTS": str(ca)} if cert else {}),
        }
        env.pop("ELECTRON_RUN_AS_NODE", None)
        process = subprocess.Popen(
            [
                electron,
                str(build),
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
            ],
            cwd=app_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log(process, "native.log")
        return process

    task = None
    control = folder / "control.txt"
    control.write_text("")
    try:
        service = start_service()
        if cert:
            # A client without this test's CA must reject the endpoint.
            try:
                httpx.get(address + "/v1/status", timeout=5)
            except httpx.ConnectError:
                pass
            else:
                raise RuntimeError("Untrusted TLS certificate was accepted")
        bot = api(
            "POST",
            "/v1/bots",
            {
                "name": "Desktop connection test",
                "purpose": "Only the explicitly requested synthetic Mac test. No other files or services.",
            },
        )
        chat = api("POST", f"/v1/bots/{bot['id']}/chats", {"title": "Desktop connection"})
        native = start_app()
        print(
            json.dumps(
                {
                    "ready": True,
                    "live": args.live,
                    "control": str(control),
                    "fixture": str(fixture),
                    "evidence": str(folder),
                    "container": owned[-1] if owned else None,
                    "service_mode": "mac-loopback" if args.local_service else "linux-vm-https",
                    "app_checkout": str(app_root),
                    "engine_checkout": str(root),
                    "app_profile": str(folder / "app-profile"),
                }
            ),
            flush=True,
        )
        seen = 0
        while True:
            lines = control.read_text().splitlines()
            commands, seen = lines[seen:], len(lines)
            for command in commands:
                command = command.strip()
                if command == "quit":
                    return
                if command == "restart-app":
                    native.terminate()
                    native.wait(timeout=20)
                    native = start_app()
                elif command == "restart-service":
                    if args.local_service:
                        service.terminate()
                    else:
                        subprocess.run(
                            ["docker", "stop", "--time", "10", owned[-1]],
                            check=True,
                            capture_output=True,
                        )
                    service.wait(timeout=20)
                    service = start_service()
                elif command == "revoke":
                    # Only this isolated account's device, never other running previews.
                    import sqlite3

                    with sqlite3.connect(folder / "bots.db") as db:
                        devices = db.execute("SELECT id FROM bot_local_devices").fetchall()
                    for device in devices:
                        api("POST", f"/v1/local-computers/{device[0]}/revoke", {})
                elif command in {"start", "stop-test"}:
                    message = f"Use my explicitly connected Mac for one synthetic test. Only access {fixture}. Run a shell command there to compute the SHA-256 of input.bin and exclusively create command.txt with that checksum, refusing overwrite. Import input.bin into this Bot's Library as desktop-roundtrip.bin. Export that same Library file to {fixture / 'roundtrip.bin'}, without overwriting. Report actual bytes and hashes from receipts, including any failures honestly. Request normal local approvals. Do not access other files, apps, accounts, credentials, settings, network services, memory or routines. Do not repeat successful writes. Stop after reporting."
                    if command == "stop-test":
                        message = f"QA stop-test: only in {fixture}, run 'printf started > stop-started.txt; sleep 300; printf bad > stop-late.txt'. I will stop this synthetic task before the last write. Do not perform any other work."
                    task = api(
                        "POST",
                        f"/v1/chats/{chat['id']}/tasks",
                        {"request_id": uuid4().hex, "message": message},
                    )
                    print(json.dumps({"task_id": task["id"]}), flush=True)
                if command in {
                    "status",
                    "start",
                    "stop-test",
                    "restart-service",
                    "restart-app",
                    "revoke",
                }:
                    # Human acceptance can create a follow-up task. Report the
                    # latest actual turn, not the original proposal's status.
                    turns = api("GET", f"/v1/chats/{chat['id']}")["tasks"]
                    latest = (
                        max(turns, key=lambda turn: (turn["created_at"], turn["id"]))
                        if turns
                        else None
                    )
                    actual = api("GET", f"/v1/tasks/{latest['id']}") if latest else None
                    files = api("GET", f"/v1/bots/{bot['id']}/files")
                    exported = fixture / "roundtrip.bin"
                    result = {
                        "task_id": actual["id"] if actual else None,
                        "status": actual["status"] if actual else "not_started",
                        "output": actual["output"] if actual else "",
                        "error": actual["error"] if actual else None,
                        "usage": api("GET", "/v1/status")["usage"],
                        "library_verified": any(
                            f["sha256"] == expected and f["size_bytes"] == len(payload)
                            for f in files
                        ),
                        "roundtrip_verified": exported.exists()
                        and exported.read_bytes() == payload,
                        "completion_review": bool(
                            actual
                            and any(e["kind"] == "reply_review_started" for e in actual["events"])
                        ),
                        "local_receipts": [
                            e["data"]
                            for e in actual["events"]
                            if e["kind"] == "local_computer_result"
                        ]
                        if actual
                        else [],
                        "stop_started": (fixture / "stop-started.txt").exists(),
                        "stop_late": (fixture / "stop-late.txt").exists(),
                    }
                    (folder / "result.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2)
                    )
                    with (folder / "history.jsonl").open("a") as history:
                        history.write(
                            json.dumps({"command": command, **result}, ensure_ascii=False) + "\n"
                        )
                    print(json.dumps(result, ensure_ascii=False), flush=True)
            time.sleep(0.2)
    finally:
        if native and native.poll() is None:
            native.terminate()
            native.wait(timeout=20)
        if args.local_service and service and service.poll() is None:
            service.terminate()
            service.wait(timeout=20)
        for name in owned:
            subprocess.run(["docker", "stop", "--time", "10", name], capture_output=True)
        client.close()
        print(
            json.dumps({"stopped": True, "data_retained": str(folder), "containers": owned}),
            flush=True,
        )


if __name__ == "__main__":
    worker() if "--worker" in sys.argv else main()
