"""Real loopback API processes over shared PostgreSQL; explicit model fixtures.

This qualifies the private single-account service boundary across three account
processes, not a public multi-tenant gateway, hosted service or model throughput.
No model credentials, external model calls, computers or cloud resources.
"""

import base64
import hashlib
import json
import os
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from test_bots_backup import databases  # noqa: F401
from test_bots_shared_scheduler_recovery import until

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.llm.types import LLMResponse


def child(config):
    import uvicorn

    store = PostgresStore(
        config["dsn"],
        objects=LocalObjectStorage(Path(config["objects"])),
        state_directory=Path(config["scratch"]),
    )
    calls = 0

    class Fixture:
        model = "explicit-http-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            nonlocal calls
            calls += 1
            if config["block_first"] and calls == 1:
                token = kwargs["cancellation_token"]
                while not token.wait(0.1):
                    pass
                token.throw_if_cancelled()
            return LLMResponse(config["reply"], [], {})

    settings = Settings(
        Path(config["scratch"]) / "local-config",
        config["token"],
        "unused-fixture-placeholder",
        "https://example.invalid/v1",
        Fixture.model,
        owner=config["owner"],
    )
    app = create_app(settings, task_store=store, client_factory=lambda _: Fixture())
    # This no-network fixture qualifies HTTP ownership, persistence and recovery,
    # not semantic reply quality. Its fixed answer is not a JSON review verdict.
    # ReplyReview has separate protocol tests and actual-model qualification.
    app.state.runtime.reply_review = None
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def measured_lifespan(app):
        async with original_lifespan(app):
            yield
        pool_stats = store._pool.get_stats()
        store.close()
        # Uvicorn reraises SIGTERM after graceful shutdown. Report from the
        # completed real lifespan, before that signal ends the process.
        print(
            json.dumps(
                {
                    "lifespan_completed": True,
                    "fixture_calls": calls,
                    "pool": pool_stats,
                    "pool_closed": store._pool.closed,
                }
            ),
            flush=True,
        )

    app.router.lifespan_context = measured_lifespan
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        print(json.dumps({"port": listener.getsockname()[1]}), flush=True)
        server = uvicorn.Server(
            uvicorn.Config(
                app, access_log=False, log_level="critical", timeout_graceful_shutdown=20
            )
        )
        try:
            server.run(sockets=[listener])
        finally:
            store.close()


def test_independent_account_apis_share_state_without_sharing_authority(
    databases,  # noqa: F811
    tmp_path,
):
    objects = tmp_path / "objects"
    store = databases(objects=LocalObjectStorage(objects))
    processes, clients, configs, groups, task_ids = [], [], [], [], []
    expected_replies, verified_stops = {}, set()
    receipt = {
        "passed": False,
        "kind": "Actual HTTP processes and PostgreSQL; explicit model fixture",
        "accounts": 3,
        "bots_per_account": 6,
        "external_model_calls": 0,
        "computer_calls": 0,
        "cloud_calls": 0,
        "children": [],
    }

    def start(config):
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        processes.append(process)
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(20), "API process did not bind within its startup allowance."
            line = process.stdout.readline()
        assert line, "API process stopped before reporting its loopback port."
        port = json.loads(line)["port"]
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + config["token"]},
            timeout=10,
        )
        clients.append(client)
        expected_replies[client] = config["reply"]

        def ready():
            assert process.poll() is None, "API process stopped during startup."
            try:
                response = client.get("/v1/status")
                return response.status_code == 200 and response.json()["worker_ready"]
            except httpx.HTTPError:
                return False

        until(ready, seconds=25)
        return process, client

    def stop(process):
        if process.poll() is None:
            process.terminate()
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            raise AssertionError("Account API did not shut down gracefully.") from None
        remaining = process.stdout.read().strip()
        assert remaining, "Account API did not confirm completion of its real lifespan."
        result = json.loads(remaining)
        assert result["lifespan_completed"] and code in (0, -signal.SIGTERM)
        assert result["pool_closed"]
        assert result["pool"]["pool_size"] <= 8
        receipt["children"].append(result)
        verified_stops.add(process)

    def api(client, method, path, body=None, expected=200):
        response = client.request(method, path, **({"json": body} if body is not None else {}))
        assert response.status_code == expected, "Unexpected account API HTTP status."
        assert response.headers["cache-control"] == "no-store"
        return response.json() if response.content else None

    def counts():
        with store.connect() as db:
            return {
                name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("bots", "tasks", "messages", "files", "provider_calls")
            }

    def all_completed(items):
        results = [(client, api(client, "GET", f"/v1/tasks/{task}")) for client, task in items]
        for client, task in results:
            if task["status"] == "completed":
                assert task["output"] == expected_replies[client], "Reply crossed account boundary."
        return all(task["status"] == "completed" for _, task in results)

    try:
        for index in range(receipt["accounts"]):
            config = {
                "dsn": store._dsn,
                "owner": "http-study-" + uuid4().hex,
                "token": secrets.token_urlsafe(40),
                "scratch": str(tmp_path / f"account-{index}"),
                "objects": str(objects),
                "block_first": index == 0,
                "reply": "Explicit account fixture 東京 " + uuid4().hex,
            }
            configs.append(config)
            start(config)
        active = clients[:]

        for index, client in enumerate(active):
            assert (
                client.get("/v1/status", headers={"Authorization": "Bearer wrong"}).status_code
                == 401
            )
            wrong = configs[(index + 1) % len(configs)]["token"]
            response = client.get("/v1/status", headers={"Authorization": "Bearer " + wrong})
            assert response.status_code == 401
            group = []
            for number in range(receipt["bots_per_account"]):
                bot = api(
                    client,
                    "POST",
                    "/v1/bots",
                    {"name": f"Αθήνα 東京 {number}", "purpose": "Synthetic HTTP study"},
                    201,
                )
                chat = api(client, "POST", f"/v1/bots/{bot['id']}/chats", {}, 201)
                group.append((bot["id"], chat["id"]))
            groups.append(group)
            assert len(api(client, "GET", "/v1/bots")) == receipt["bots_per_account"]

        files = []
        for index, client in enumerate(active):
            bot_id = groups[index][0][0]
            payload = (f"Account {index}: Ελληνικά 日本語\n" * 1000).encode()
            file = api(
                client,
                "POST",
                f"/v1/bots/{bot_id}/files/binary",
                {"name": "account.bin", "data_base64": base64.b64encode(payload).decode()},
                201,
            )
            files.append((bot_id, file["id"], payload))
        for index, client in enumerate(active):
            bot, file_id, payload = files[(index + 1) % len(files)]
            api(client, "GET", f"/v1/bots/{bot}/files/{file_id}/download", expected=404)
            api(
                client,
                "PUT",
                f"/v1/bots/{bot}",
                {"name": "Denied", "purpose": "Must not mutate"},
                404,
            )
            api(
                client,
                "POST",
                f"/v1/chats/{groups[(index + 1) % len(groups)][0][1]}/tasks",
                {"message": "Denied cross-account request", "request_id": uuid4().hex},
                404,
            )

        blocked = api(
            active[0],
            "POST",
            f"/v1/chats/{groups[0][0][1]}/tasks",
            {"message": "Explicit first-call blocking fixture", "request_id": uuid4().hex},
            202,
        )
        until(lambda: api(active[0], "GET", f"/v1/tasks/{blocked['id']}")["calls"] == 1)
        for client in active[1:]:
            api(client, "GET", f"/v1/tasks/{blocked['id']}", expected=404)
            api(client, "POST", f"/v1/tasks/{blocked['id']}/cancel", expected=404)

        started = time.monotonic()

        def enqueue(index):
            client = active[index]
            submitted = []
            for _, chat_id in groups[index]:
                for _ in range(2):
                    body = {"message": "Explicit HTTP fixture task", "request_id": uuid4().hex}
                    task = api(client, "POST", f"/v1/chats/{chat_id}/tasks", body, 202)
                    assert (
                        api(client, "POST", f"/v1/chats/{chat_id}/tasks", body, 202)["id"]
                        == task["id"]
                    )
                    submitted.append((client, task["id"]))
            return submitted

        with ThreadPoolExecutor(max_workers=3) as executor:
            task_ids = list(executor.map(enqueue, range(3)))
        until(lambda: all_completed(task_ids[1] + task_ids[2]), seconds=40)
        receipt["other_accounts_seconds"] = round(time.monotonic() - started, 3)
        assert api(active[0], "GET", f"/v1/tasks/{blocked['id']}")["status"] == "running"
        assert all(
            api(client, "GET", f"/v1/tasks/{task}")["status"] == "queued"
            for client, task in task_ids[0]
        )
        cancelled_at = time.monotonic()
        api(active[0], "POST", f"/v1/tasks/{blocked['id']}/cancel")
        until(lambda: api(active[0], "GET", f"/v1/tasks/{blocked['id']}")["status"] == "cancelled")
        receipt["cancellation_seconds"] = round(time.monotonic() - cancelled_at, 3)
        until(lambda: all_completed(task_ids[0]), seconds=40)
        receipt["burst_seconds"] = round(time.monotonic() - started, 3)

        stop(processes[1])
        offline_tasks = []
        for index in (0, 2):
            task = api(
                active[index],
                "POST",
                f"/v1/chats/{groups[index][0][1]}/tasks",
                {"message": "Other account API is offline", "request_id": uuid4().hex},
                202,
            )
            offline_tasks.append((active[index], task["id"]))
        until(lambda: all_completed(offline_tasks))
        receipt["completed_while_other_api_offline"] = len(offline_tasks)
        before_restart = counts()
        configs[1]["scratch"] = str(tmp_path / "replacement-scratch")
        _, replacement = start(configs[1])
        time.sleep(2)
        assert counts() == before_restart, "Restart duplicated durable work or replies."
        for index, client in enumerate([active[0], replacement, active[2]]):
            bot, file_id, payload = files[index]
            encoded = api(client, "GET", f"/v1/bots/{bot}/files/{file_id}/download")["data_base64"]
            assert base64.b64decode(encoded) == payload
            assert all(
                api(client, "GET", f"/v1/chats/{chat}")["bot_id"] == bot_id
                for bot_id, chat in groups[index]
            )
            for _, chat_id in groups[index]:
                messages = api(client, "GET", f"/v1/chats/{chat_id}")["messages"]
                assert all(
                    message["content"] == expected_replies[client]
                    for message in messages
                    if message["role"] == "assistant"
                )
        followup = api(
            replacement,
            "POST",
            f"/v1/chats/{groups[1][0][1]}/tasks",
            {"message": "After replacement", "request_id": uuid4().hex},
            202,
        )
        until(lambda: all_completed([(replacement, followup["id"])]))
        usage = [
            api(client, "GET", "/v1/status")["usage"]
            for client in [active[0], replacement, active[2]]
        ]
        receipt.update(
            completed_tasks=39,
            cancelled_tasks=1,
            final_counts=counts(),
            isolated_fixture_usage=usage,
            preserved_file_sha256=[hashlib.sha256(payload).hexdigest() for _, _, payload in files],
        )
        assert receipt["final_counts"] == {
            "bots": 18,
            "tasks": 40,
            "messages": 79,
            "files": 3,
            "provider_calls": 40,
        }
        assert sum(row["calls"] for row in usage) == 40
        assert sum(row["unconfirmed_calls"] for row in usage) == 1
        assert [row["calls"] for row in usage] == [14, 13, 13]
        assert [row["unconfirmed_calls"] for row in usage] == [1, 0, 0]
        with store.connect() as db:
            publication = db.execute(
                "SELECT t.id,t.status,count(m.id) FROM tasks t LEFT JOIN messages m "
                "ON m.task_id=t.id AND m.role='assistant' GROUP BY t.id,t.status"
            ).fetchall()
        assert len(publication) == 40
        assert all(row[2] == (1 if row[1] == "completed" else 0) for row in publication)
        receipt["passed"] = True
    finally:
        failures = []
        for process in processes:
            if process not in verified_stops:
                try:
                    stop(process)
                except Exception as error:
                    # One malformed receipt or failed child must not prevent
                    # termination and checking of all the other owned children.
                    failures.append(type(error).__name__)
        for client in clients:
            client.close()
        receipt["all_processes_stopped"] = all(p.poll() is not None for p in processes)
        receipt["graceful_cleanup"] = not failures
        receipt["cleanup_errors"] = failures
        if failures:
            receipt["passed"] = False
        if output := os.environ.get("ALYSIS_BOTS_HTTP_RECEIPT"):
            Path(output).write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        assert not failures, "Account API cleanup failed."


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    try:
        child(json.loads(sys.stdin.readline()))
    except Exception:
        raise SystemExit("Isolated HTTP worker failed; no configuration printed.") from None
