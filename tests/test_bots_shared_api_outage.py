"""Real loopback HTTP/isolated PostgreSQL outage; no external models or cloud.

Only the uniquely created fixture database loses connections. The server and
other databases remain running. Recovery uses the explicit HTTP model fixture.
"""

import base64
import json
import os
import secrets
import selectors
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import test_bots_shared_http as http_study
from test_bots_backup import databases  # noqa: F401
from test_bots_shared_scheduler_recovery import until

from alysis_code.bots.object_storage import LocalObjectStorage


def test_database_outage_is_honest_over_http_and_fresh_worker_recovers(
    databases,  # noqa: F811
    tmp_path,
):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    objects = tmp_path / "objects"
    store = databases(objects=LocalObjectStorage(objects))
    admin_dsn = os.environ["ALYSIS_BOTS_TEST_POSTGRES_DSN"]
    database = conninfo_to_dict(store._dsn)["dbname"]
    assert database.startswith("bots_backup_")
    assert database != conninfo_to_dict(admin_dsn)["dbname"]
    config = {
        "dsn": store._dsn,
        "owner": "http-outage-" + uuid4().hex,
        "token": secrets.token_urlsafe(40),
        "scratch": str(tmp_path / "original"),
        "objects": str(objects),
        "block_first": False,
        "reply": "Explicit recovered HTTP fixture 東京 " + uuid4().hex,
    }
    processes, clients, stopped = [], [], set()
    receipt = {
        "passed": False,
        "kind": "Actual loopback HTTP/PostgreSQL outage; explicit no-network model fixture",
        "external_model_calls": 0,
        "cloud_calls": 0,
        "computer_calls": 0,
        "children": [],
    }

    def connections(allow):
        with psycopg.connect(admin_dsn, autocommit=True) as db:
            db.execute(
                sql.SQL(
                    "ALTER DATABASE {} ALLOW_CONNECTIONS " + ("true" if allow else "false")
                ).format(sql.Identifier(database))
            )
            if not allow:
                db.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=%s AND pid<>pg_backend_pid()",
                    (database,),
                )

    def api(client, method, path, body=None, expected=200):
        response = client.request(method, path, **({"json": body} if body is not None else {}))
        assert response.status_code == expected, "Unexpected HTTP status."
        return response.json()

    def start():
        process = subprocess.Popen(
            [sys.executable, str(Path(http_study.__file__).resolve()), "--worker"],
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
            assert selector.select(20), "API process did not report its loopback port."
            line = process.stdout.readline()
        assert line, "API process stopped before binding."
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{json.loads(line)['port']}",
            headers={"Authorization": "Bearer " + config["token"]},
            timeout=12,
        )
        clients.append(client)

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
            raise AssertionError("Owned outage API did not shut down gracefully.") from None
        result = json.loads(process.stdout.read().strip())
        assert result["lifespan_completed"] and result["pool_closed"]
        assert code in (0, -signal.SIGTERM)
        receipt["children"].append(result)
        stopped.add(process)

    try:
        original, client = start()
        bot = api(
            client, "POST", "/v1/bots", {"name": "Keep Αθήνα", "purpose": "Outage fixture"}, 201
        )
        chat = api(client, "POST", f"/v1/bots/{bot['id']}/chats", {}, 201)
        payload = "Saved before the outage — Αθήνα 東京\n".encode()
        saved = api(
            client,
            "POST",
            f"/v1/bots/{bot['id']}/files/binary",
            {"name": "saved.txt", "data_base64": base64.b64encode(payload).decode()},
            201,
        )
        connections(False)
        outage_at = time.monotonic()
        invalid_at = time.monotonic()
        rejected = client.get("/v1/status", headers={"Authorization": "Bearer wrong"})
        receipt["wrong_token_seconds"] = time.monotonic() - invalid_at
        assert rejected.status_code == 401 and receipt["wrong_token_seconds"] < 2
        mutation_name = "Must not appear " + uuid4().hex
        requests = [
            ("read", "GET", "/v1/bots", None),
            ("status", "GET", "/v1/status", None),
            (
                "mutation",
                "PUT",
                f"/v1/bots/{bot['id']}",
                {"name": mutation_name, "purpose": "Rejected"},
            ),
        ]

        def request(item):
            label, method, path, body = item
            before = time.monotonic()
            response = client.request(method, path, **({"json": body} if body else {}))
            return label, response, time.monotonic() - before

        with ThreadPoolExecutor(max_workers=3) as executor:
            responses = list(executor.map(request, requests))
        receipt["outage_responses"] = [
            {
                "operation": label,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "seconds": seconds,
            }
            for label, response, seconds in responses
        ]
        # Keep the fault long enough for a worker's normal one-second idle
        # poll plus five-second pool wait to fail too. Shorter API-only faults
        # can legitimately recover before the worker observes ownership loss.
        remaining = 7 - (time.monotonic() - outage_at)
        if remaining > 0:
            time.sleep(remaining)
        receipt["database_unavailable_seconds"] = time.monotonic() - outage_at
        # Restore before assertions/teardown so even a baseline failure can
        # release the real worker lease and drop only this fixture database.
        connections(True)
        for _label, response, seconds in responses:
            assert response.status_code == 503, "Database outage must return HTTP 503."
            assert response.headers.get("content-type", "").startswith("application/json")
            assert response.headers["cache-control"] == "no-store"
            assert seconds < 10, "An outage request exceeded its bounded pool allowance."
            body = response.json()
            assert set(body) == {"detail"} and isinstance(body["detail"], str) and body["detail"]
            private = (
                config["token"],
                store._dsn,
                database,
                config["scratch"],
                "Traceback",
                "psycopg",
            )
            assert all(value not in response.text for value in private), (
                "Error exposed internal configuration."
            )

        def recovered_status():
            response = client.get("/v1/status")
            return response.json() if response.status_code == 200 else None

        status = until(recovered_status, lambda value: value is not None, seconds=20)
        assert status["worker_ready"] is False and status["usage"]["calls"] == 0
        assert api(client, "GET", "/v1/bots")[0]["name"] == bot["name"]
        assert api(client, "GET", f"/v1/chats/{chat['id']}")["messages"] == []
        stop(original)
        assert receipt["children"][-1]["fixture_calls"] == 0
        config["scratch"] = str(tmp_path / "replacement")
        _, replacement = start()
        assert api(replacement, "GET", "/v1/bots")[0]["name"] == bot["name"]
        download = api(replacement, "GET", f"/v1/bots/{bot['id']}/files/{saved['id']}/download")
        assert base64.b64decode(download["data_base64"]) == payload
        task_body = {"message": "Explicit fixture after recovery", "request_id": uuid4().hex}
        submitted = api(replacement, "POST", f"/v1/chats/{chat['id']}/tasks", task_body, 202)
        completed = until(
            lambda: api(replacement, "GET", f"/v1/tasks/{submitted['id']}"),
            lambda value: value["status"] == "completed",
        )
        assert completed["output"] == config["reply"]
        assert (
            api(replacement, "POST", f"/v1/chats/{chat['id']}/tasks", task_body, 202)["id"]
            == submitted["id"]
        )
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM bots").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM provider_calls").fetchone()[0] == 1
            assert (
                db.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0]
                == 1
            )
        assert api(replacement, "GET", "/v1/bots")[0]["name"] == bot["name"]
        receipt.update(
            preserved_file=True,
            rejected_mutation_not_replayed=True,
            old_worker_not_ready=True,
            replacement_completed_once=True,
            passed=True,
        )
    finally:
        # This is an admin connection to the separate fixture control database.
        # Always reopen the unique test DB before worker and fixture cleanup.
        failures = []
        try:
            connections(True)
            receipt["database_connections_restored"] = True
        except Exception as error:
            receipt["database_connections_restored"] = False
            failures.append(type(error).__name__)
        for process in processes:
            if process not in stopped:
                try:
                    stop(process)
                except Exception as error:
                    failures.append(type(error).__name__)
        for client in clients:
            client.close()
        receipt["all_processes_stopped"] = all(process.poll() is not None for process in processes)
        receipt["graceful_cleanup"] = not failures
        if failures:
            receipt["passed"] = False
        if output := os.environ.get("ALYSIS_BOTS_API_OUTAGE_RECEIPT"):
            Path(output).write_text(json.dumps(receipt, indent=2) + "\n")
        assert not failures, "Outage API cleanup failed."
