"""Black-box provider fault/recovery checks against an unsigned frozen CLI.

Only synthetic credentials and a loopback HTTP server are used. This is component
evidence; it does not claim failures were exercised against a real paid provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.release.smoke_managed_cli import _smoke_environment  # noqa: E402


class FaultHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *_args: object) -> None:
        pass

    def send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        self.send_json(200, {"object": "list", "data": [{"id": "qa-ok"}]})

    def do_POST(self) -> None:  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.requests.append(payload)
        model = payload.get("model", "")
        if model.startswith("qa-http-"):
            status = int(model.rsplit("-", 1)[1])
            descriptions = {
                401: "invalid API key",
                402: "insufficient balance",
                429: "rate limit exceeded",
                400: "model not found",
            }
            self.send_json(
                status, {"error": {"message": descriptions[status], "type": "qa_failure"}}
            )
            return
        if model == "qa-disconnect":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            self.close_connection = True
            return
        if model == "qa-slow":
            time.sleep(2)
        self.send_json(
            200,
            {
                "id": "chatcmpl-local-qa",
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "RECOVERY_OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
        )


class Bridge:
    def __init__(self, executable: Path, env: dict[str, str], cwd: Path):
        self.child = subprocess.Popen(
            [str(executable), "ide-bridge", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            text=True,
            env=env,
            cwd=cwd,
        )
        self.frames: queue.Queue[dict] = queue.Queue()
        self.events: list[dict] = []
        self.counter = 0

        def read() -> None:
            assert self.child.stdout
            for line in self.child.stdout:
                try:
                    self.frames.put(json.loads(line))
                except json.JSONDecodeError:
                    self.frames.put({"invalid_json": True})
            self.frames.put({"eof": True})

        threading.Thread(target=read, daemon=True).start()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.counter += 1
        request_id = f"fault-{self.counter}"
        assert self.child.stdin
        self.child.stdin.write(
            json.dumps(
                {
                    "protocol_version": "1",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
            + "\n"
        )
        self.child.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            frame = self.frames.get(timeout=max(0.01, deadline - time.monotonic()))
            if frame.get("eof") or frame.get("invalid_json"):
                raise RuntimeError("Frozen bridge stopped or emitted an invalid frame")
            if frame.get("id") == request_id:
                if not frame.get("ok"):
                    raise RuntimeError(f"{method}: {frame.get('error')}")
                return frame["result"]
            self.events.append(frame)
        raise TimeoutError(method)

    def turn(self, session_id: str, prompt: str) -> dict:
        started = self.request("chat.send", {"session_id": session_id, "message": prompt})
        # Connection drops deliberately retain five jittered retries (2/4/8/16/32s),
        # even when the ordinary HTTP retry count is zero. Cover that production
        # policy without making this experiment depend on a favorable jitter draw.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            job = self.request("job.status", {"job_id": started["job_id"]})
            if job["status"] not in {"queued", "running", "cancellation_requested"}:
                return job
            time.sleep(0.05)
        self.request("session.cancel", {"session_id": session_id})
        raise TimeoutError("Provider fault did not settle within 90 seconds")

    def close(self) -> None:
        try:
            if self.child.poll() is None:
                self.request("bridge.shutdown")
                self.child.wait(timeout=10)
        finally:
            if self.child.poll() is None:
                self.child.terminate()
                self.child.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    runtime_sha256 = hashlib.file_digest(executable.open("rb"), "sha256").hexdigest()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FaultHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (
            tempfile.TemporaryDirectory(prefix="anr-") as tmp,
            args.output.open("w", encoding="utf-8") as report,
        ):
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            marker = workspace / "human.txt"
            marker.write_bytes(b"Human content must remain unchanged.\n")
            env = _smoke_environment(root)
            env["ALYSIS_API_KEY"] = "qa-synthetic-credential"
            if os.name == "nt":
                env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
            config_dir = Path(env["ALYSIS_CONFIG_DIR"])
            config_dir.mkdir()
            config = {
                "active_profile": "qa",
                "base_url": base_url,
                "model": "qa-ok",
                "profiles": {
                    "qa": {
                        "name": "qa",
                        "protocol": "openai_compat",
                        "base_url": base_url,
                        "api_key_env": "ALYSIS_API_KEY",
                        "default_model": "qa-ok",
                        "extra_headers": {},
                    }
                },
                "routing_mode": "code_only",
                "stream": False,
                "llm_timeout_s": 0.5,
                "provider_retry_max_retries": 0,
                "provider_retry_base_delay_seconds": 0.01,
                "provider_retry_max_delay_seconds": 0.01,
                "skills_enabled": False,
                "skills_auto_invoke": False,
                "custom_tools_enabled": False,
                "subagents_enabled": False,
                "web_search_mode": "off",
            }
            (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            bridge = Bridge(executable, env, workspace)
            try:
                bridge.request("initialize")
                for fault in (
                    "qa-http-401",
                    "qa-http-402",
                    "qa-http-429",
                    "qa-http-400",
                    "qa-disconnect",
                    "qa-slow",
                ):
                    started = time.monotonic()
                    session = bridge.request(
                        "session.create",
                        {
                            "workspace": str(workspace),
                            "mode": "readonly",
                            "model": fault,
                            "stream": False,
                        },
                    )["session_id"]
                    failed = bridge.turn(
                        session, "Reply with a short acknowledgment. Do not use tools."
                    )
                    assert failed["status"] in {"failed", "completed"}, failed
                    assert failed.get("exit_code") != 0 or failed["status"] == "failed", failed
                    fault_requests = sum(
                        request.get("model") == fault for request in FaultHandler.requests
                    )
                    assert fault_requests > 0, "The fault did not reach the loopback provider"
                    bridge.request("session.setModel", {"session_id": session, "model": "qa-ok"})
                    before = len(FaultHandler.requests)
                    for number in (1, 2):
                        restored = bridge.turn(
                            session,
                            f"Recovery turn {number}. Reply with a short acknowledgment. Do not use tools.",
                        )
                        assert (
                            restored["status"] == "completed" and restored.get("exit_code") == 0
                        ), restored
                    captured = [
                        r for r in FaultHandler.requests[before:] if r.get("model") == "qa-ok"
                    ]
                    assert captured and any(
                        m.get("role") == "assistant" and m.get("content") == "RECOVERY_OK"
                        for m in captured[-1].get("messages", [])
                    ), "The second request lost the first assistant turn"
                    assert marker.read_bytes() == b"Human content must remain unchanged.\n"
                    row = {
                        "fault": fault,
                        "status": "passed",
                        "failure_state": failed["status"],
                        "failure_exit_code": failed.get("exit_code"),
                        "recovery_turns": 2,
                        "assistant_history_preserved": True,
                        "human_file_preserved": True,
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                        "provider": "synthetic_loopback",
                        "runtime": "unsigned_frozen",
                        "runtime_sha256": runtime_sha256,
                        "fault_http_requests": fault_requests,
                    }
                    line = json.dumps(row)
                    report.write(line + "\n")
                    report.flush()
                    print(line, flush=True)
                    bridge.request("session.cancel", {"session_id": session})
            finally:
                bridge.close()
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
