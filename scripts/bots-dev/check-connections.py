#!/usr/bin/env python3
"""Offline Linux HTTP integration study. Both provider and GitHub data are fixtures.

Runs a real Bots HTTP service and a separate loopback GitHub-shaped HTTP server.
No personal connection, external network, secret, or live model is used.
"""

from __future__ import annotations

import json
import platform
import secrets
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import uvicorn

from alysis_code.bots.app import Settings, create_app
from alysis_code.cloud_connections import (
    BoundConnections,
    ConnectionUnavailable,
    GitHubBinding,
    GitHubReader,
    GrantInput,
    InstallationToken,
)
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall


def main():
    marker = "日本語-" + secrets.token_hex(4)
    calls = []
    entered, release = threading.Event(), threading.Event()

    class GitHubFixture(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            assert self.path == "/repos/synthetic/project/issues/42"
            assert self.headers.get("Authorization") == "Bearer synthetic-installation"
            calls.append(self.path)
            if len(calls) == 3:
                entered.set()
                assert release.wait(10)
            payload = json.dumps(
                {
                    "number": 42,
                    "title": "Synthetic review",
                    "body": marker,
                    "repository_url": "https://api.github.com/repos/synthetic/project",
                    "state": "open",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), GitHubFixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()

    class FixtureTransport(httpx.BaseTransport):
        def __init__(self):
            self.transport = httpx.HTTPTransport(trust_env=False)

        def handle_request(self, request):
            assert request.url.scheme == "https" and request.url.host == "api.github.com"
            target = request.url.copy_with(
                scheme="http", host="127.0.0.1", port=fixture.server_port
            )
            return self.transport.handle_request(
                httpx.Request(request.method, target, headers=request.headers)
            )

        def close(self):
            self.transport.close()

    with (
        tempfile.TemporaryDirectory() as directory,
        httpx.Client(transport=FixtureTransport(), timeout=10) as transport,
    ):
        settings = Settings(
            Path(directory) / "bots.db",
            secrets.token_hex(20),
            "provider-fixture",
            "https://example.invalid/v1",
            "provider-fixture",
            max_calls=4,
        )
        reader = GitHubReader(
            lambda _: InstallationToken("synthetic-installation", time.time() + 600),
            client=transport,
        )
        identity = None

        class ProviderFixture:
            model = "provider-fixture"
            supports_tool_calling = True

            def __init__(self):
                self.count = 0

            def chat(self, **kwargs):
                self.count += 1
                if self.count == 1:
                    assert "github_read_issue" in [t["function"]["name"] for t in kwargs["tools"]]
                    return LLMResponse(
                        "",
                        [
                            ToolCall(
                                "issue",
                                "github_read_issue",
                                {"connection_id": identity, "issue_number": 42},
                            )
                        ],
                        {},
                        usage=LLMUsage(1, 1, 2),
                    )
                payload = json.loads(kwargs["messages"][-1]["content"])
                assert payload["body"] == marker
                return LLMResponse(
                    "Fixture received " + payload["body"], [], {}, usage=LLMUsage(1, 1, 2)
                )

        app = create_app(settings, github_reader=reader, client_factory=lambda _: ProviderFixture())
        store = app.state.connections
        identity = store.register_github(
            settings.owner, GitHubBinding(123, 456, "synthetic/project", "Synthetic")
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            with httpx.Client(
                base_url=f"http://127.0.0.1:{sock.getsockname()[1]}",
                headers={"Authorization": "Bearer " + settings.token},
                timeout=10,
            ) as client:

                def api(method, path, body=None):
                    response = client.request(method, path, json=body)
                    assert response.is_success, response.status_code
                    return response.json()

                bots = [
                    api("POST", "/v1/bots", {"name": name, "purpose": "Synthetic connection study"})
                    for name in ("One", "Two")
                ]
                base = f"/v1/bots/{bots[0]['id']}/apps"
                api("PATCH", base + "/" + identity, {"enabled": True, "expected_revision": 0})
                assert not api("GET", f"/v1/bots/{bots[1]['id']}/apps")[0]["granted"]
                chat = api("POST", f"/v1/bots/{bots[0]['id']}/chats", {})
                task = api(
                    "POST",
                    f"/v1/chats/{chat['id']}/tasks",
                    {
                        "message": "Read the assigned synthetic issue",
                        "request_id": "connection-study",
                    },
                )
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    result = api("GET", f"/v1/tasks/{task['id']}")
                    if result["status"] in ("completed", "failed"):
                        break
                    time.sleep(0.02)
                assert result["status"] == "completed" and marker in result["output"]
                # Main-agent tool uses the same record with its own explicit grant.
                store.set_grant(
                    settings.owner,
                    "agent:study",
                    identity,
                    GrantInput(enabled=True, expected_revision=0),
                )
                bound = BoundConnections(store, settings.owner, "agent:study", reader)
                assert (
                    bound.tools()["github_read_issue"].run(
                        {"connection_id": identity, "issue_number": 42}
                    )["body"]
                    == marker
                )
                bot_bound = BoundConnections(store, settings.owner, "bot:" + bots[0]["id"], reader)
                with ThreadPoolExecutor() as pool:
                    future = pool.submit(
                        bot_bound.read_issue, {"connection_id": identity, "issue_number": 42}
                    )
                    assert entered.wait(10)
                    api("PATCH", base + "/" + identity, {"enabled": False, "expected_revision": 1})
                    release.set()
                    try:
                        future.result(10)
                        raise AssertionError("Revoked read returned content")
                    except ConnectionUnavailable:
                        pass
                assert bound.catalog()[0]["granted"]
                assert len(calls) == 3
                usage = api("GET", "/v1/status")["usage"]
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "platform": platform.system(),
                            "model": "fixture only",
                            "github": "loopback HTTP fixture only",
                            "external_calls": 0,
                            "fixture_model_calls": usage["calls"],
                            "fixture_http_reads": len(calls),
                            "checks": [
                                "Real HTTP bot grant and provider-fixture dispatch through the Linux service",
                                "Main-agent adapter reads the same shared connection",
                                "Second bot has no grant",
                                "Revocation during HTTP withholds the result while main-agent grant remains valid",
                            ],
                        }
                    )
                )
        finally:
            release.set()
            server.should_exit = True
            thread.join(10)
            fixture.shutdown()
            fixture.server_close()
            sock.close()
        assert not thread.is_alive()


if __name__ == "__main__":
    main()
