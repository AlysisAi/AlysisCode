"""Real HTTP service/process restart; model responses are explicit fixtures.
The service is killed after a durable file save and during the following model
call. Reposting the same request cannot replay it. No model API is contacted.
"""

import json
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def service():
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.llm.types import LLMResponse, ToolCall

    config = json.loads(sys.stdin.readline())
    root = Path(config["root"])

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            with (root / "calls").open("a") as f:
                f.write("call\n")
            if not (root / "block").exists():
                (root / "block").write_text("next call blocks")
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "save",
                            "save_file",
                            {
                                "name": "partial.txt",
                                "content": "Verified partial result Αθήνα 東京",
                            },
                        )
                    ],
                    {},
                )
            while True:
                time.sleep(0.1)

    app = create_app(
        Settings(root / "bots.db", config["token"], "fixture", "https://unused.invalid", "fixture"),
        client_factory=lambda _: Client(),
    )
    uvicorn.run(app, host="127.0.0.1", port=config["port"], access_log=False, log_level="error")


def main():
    import socket

    import httpx

    root = Path(tempfile.mkdtemp(prefix="bots-restart-qualification-"))
    token = secrets.token_urlsafe(32)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer " + token}, timeout=4
    )
    process = None

    def start():
        p = subprocess.Popen(
            [sys.executable, __file__, "--service"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        p.stdin.write(
            (json.dumps({"root": str(root), "port": port, "token": token}) + "\n").encode()
        )
        p.stdin.close()
        for _ in range(100):
            assert p.poll() is None, "Service unexpectedly exited"
            try:
                if client.get("/v1/bots").is_success:
                    return p
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("Service startup timed out")

    try:
        process = start()
        bot = client.post(
            "/v1/bots", json={"name": "Restart test", "purpose": "Synthetic partial result"}
        ).json()
        chat = client.post(f"/v1/bots/{bot['id']}/chats", json={"title": "Chat"}).json()
        import base64
        import hashlib

        chunk = b"real-service-resume" * 8192
        upload = client.post(
            f"/v1/bots/{bot['id']}/uploads",
            json={
                "name": "resumed.bin",
                "size": 3 * len(chunk),
                "request_id": "resumable-http-upload",
            },
        ).json()
        path = f"/v1/bots/{bot['id']}/uploads/{upload['id']}"
        first = {"offset": 0, "data_base64": base64.b64encode(chunk).decode()}
        assert client.put(path, json=first).json()["received"] == len(chunk)
        process.kill()
        process.wait(5)
        process = start()
        assert client.get(path).json()["received"] == len(chunk)
        assert client.put(path, json=first).json()["received"] == len(chunk)
        for i in (1, 2):
            client.put(path, json={**first, "offset": i * len(chunk)}).raise_for_status()
        completed = client.post(path + "/complete").json()
        assert client.post(path + "/complete").json()["file_id"] == completed["file_id"]
        resumed = client.get(f"/v1/bots/{bot['id']}/files/{completed['file_id']}").json()
        assert resumed["sha256"] == hashlib.sha256(chunk * 3).hexdigest()
        body = {
            "message": "Save a partial result then continue thinking",
            "request_id": "restart-idempotency",
        }
        task = client.post(f"/v1/chats/{chat['id']}/tasks", json=body).json()
        for _ in range(100):
            files = client.get(f"/v1/bots/{bot['id']}/files").json()
            if len(files) == 2 and len((root / "calls").read_text().splitlines()) == 2:
                break
            time.sleep(0.05)
        assert len(files) == 2
        process.kill()
        process.wait(5)
        process = start()
        restarted = client.get(f"/v1/tasks/{task['id']}").json()
        assert restarted["status"] == "interrupted", restarted
        assert len(client.get(f"/v1/bots/{bot['id']}/files").json()) == 2
        assert (
            client.get(
                f"/v1/bots/{bot['id']}/files/{next(f for f in files if f['name'] == 'partial.txt')['id']}"
            ).json()["content"]
            == "Verified partial result Αθήνα 東京"
        )
        replay = client.post(f"/v1/chats/{chat['id']}/tasks", json=body).json()
        assert replay["id"] == task["id"] and replay["status"] == "interrupted"
        time.sleep(0.5)
        assert len((root / "calls").read_text().splitlines()) == 2
        print(
            json.dumps(
                {
                    "passed": True,
                    "evidence": "actual HTTP + process SIGKILL, fixture model",
                    "status": restarted["status"],
                    "partial_files": 1,
                    "resumable_upload_after_service_kill": True,
                    "model_fixture_calls": 2,
                    "same_request_not_replayed": True,
                }
            )
        )
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(10)
        client.close()


if __name__ == "__main__":
    service() if "--service" in sys.argv else main()
