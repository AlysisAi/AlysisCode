"""Actual Uvicorn lifecycle and isolated PostgreSQL worker-loss supervision.

No Azure, computer, external model or synthetic model calls. The database fault
affects only a uniquely created fixture database; the shared server stays up.
"""

import asyncio
import base64
import json
import os
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_bots_backup import databases  # noqa: F401
from test_bots_shared_scheduler_recovery import until

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.serving import serve
from alysis_code.bots.worker_lease import WorkerLease, WorkerLeaseLost


def child(config):
    import uvicorn

    store = PostgresStore(
        config["dsn"],
        objects=LocalObjectStorage(Path(config["objects"])),
        state_directory=Path(config["scratch"]),
    )
    calls = 0
    lifespan_completed = False

    def no_model(_):
        nonlocal calls
        calls += 1
        raise AssertionError("This supervision study must not request a model.")

    settings = Settings(
        Path(config["scratch"]) / "local-config",
        config["token"],
        "unused-fixture-placeholder",
        "https://example.invalid/v1",
        "fixture-no-model",
        owner=config["owner"],
        max_calls=0,
    )
    app = create_app(settings, task_store=store, client_factory=no_model)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def measured_lifespan(app):
        nonlocal lifespan_completed
        async with original_lifespan(app):
            yield
        lifespan_completed = True
        store.close()
        # Uvicorn may re-raise SIGTERM after normal shutdown. Emit from the
        # completed real lifespan so signal exit cannot hide cleanup evidence.
        print(
            json.dumps(
                {
                    "kind": "lifespan",
                    "lifespan_completed": True,
                    "pool_closed": store._pool.closed,
                    "model_factory_calls": calls,
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
            if config["supervised"]:
                asyncio.run(serve(server, app.state.runtime, sockets=[listener]))
            else:
                # Test-only before-fix control, never a production setting.
                asyncio.run(server.serve(sockets=[listener]))
        finally:
            store.close()
            # Unlike a successful-lifespan receipt, this only confirms owned
            # local resources closed. The durable lease can remain uncertain.
            print(
                json.dumps(
                    {
                        "kind": "resources",
                        "lifespan_completed": lifespan_completed,
                        "pool_closed": store._pool.closed,
                        "model_factory_calls": calls,
                    }
                ),
                flush=True,
            )


@pytest.mark.parametrize("failure", [None, "startup", "shutdown"])
def test_actual_server_lifecycle_is_honest(failure):
    import uvicorn

    async def scenario():
        runtime = SimpleNamespace(is_running=False)
        events = []

        @asynccontextmanager
        async def lifespan(_):
            events.append("startup")
            if failure == "startup":
                raise RuntimeError("Explicit startup failure fixture")
            runtime.is_running = True
            yield
            runtime.is_running = False
            events.append("shutdown")
            if failure == "shutdown":
                raise RuntimeError("Explicit shutdown failure fixture")

        app = FastAPI(lifespan=lifespan)

        @app.get("/")
        def status():
            return {"ready": runtime.is_running}

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical"))
            if failure == "startup":
                # Some Uvicorn versions raise SystemExit themselves; others
                # return without started and let the wrapper reject startup.
                with pytest.raises((RuntimeError, SystemExit)) as caught:
                    async with asyncio.timeout(5):
                        await serve(server, runtime, sockets=[listener])
                if isinstance(caught.value, SystemExit):
                    assert caught.value.code != 0
                else:
                    assert "service lifecycle" in str(caught.value)
                assert events == ["startup"] and not server.started
                assert runtime.is_running is False
                return
            task = asyncio.create_task(serve(server, runtime, sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        assert not task.done(), "Server failed before becoming ready."
                        await asyncio.sleep(0.01)
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"http://127.0.0.1:{listener.getsockname()[1]}/")
                assert response.status_code == 200 and response.json() == {"ready": True}
                # An orderly shutdown changes runtime state during lifespan
                # exit and must not be mistaken for a failed worker.
                server.should_exit = True
                if failure:
                    with pytest.raises(RuntimeError, match="service lifecycle"):
                        await asyncio.wait_for(task, 5)
                else:
                    await asyncio.wait_for(task, 5)
                assert events == ["startup", "shutdown"]
                assert runtime.is_running is False
            finally:
                server.should_exit = True
                if not task.done():
                    await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


@pytest.mark.parametrize("persistent_outage", [False, True], ids=["restored", "persistent"])
def test_hosted_worker_loss_exits_after_cleanup_and_fresh_controller_recovers(
    databases,  # noqa: F811
    tmp_path,
    persistent_outage,
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
        "owner": "hosted-supervision-" + uuid4().hex,
        "token": secrets.token_urlsafe(40),
        "scratch": str(tmp_path / "original"),
        "objects": str(objects),
        "supervised": not bool(os.environ.get("ALYSIS_BOTS_SUPERVISION_BASELINE")),
    }
    processes, clients, verified_stops = [], [], set()
    receipt = {
        "passed": False,
        "kind": "Actual Uvicorn/HTTP/PostgreSQL worker-loss supervision",
        "supervised": config["supervised"],
        "database_unavailable_through_shutdown": persistent_outage,
        "model_calls": 0,
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
            assert selector.select(20), "Hosted fixture did not report its loopback port."
            line = process.stdout.readline()
        assert line, "Hosted fixture stopped before binding."
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{json.loads(line)['port']}",
            headers={"Authorization": "Bearer " + config["token"]},
            timeout=12,
        )
        clients.append(client)

        def ready():
            assert process.poll() is None, "Hosted fixture stopped during startup."
            try:
                response = client.get("/v1/status")
                return response.status_code == 200 and response.json()["worker_ready"]
            except httpx.HTTPError:
                return False

        until(ready, seconds=25)
        return process, client

    def collect(process, *, failed=False, completed_lifespan=True):
        code = process.wait(timeout=25)
        results = [json.loads(line) for line in process.stdout.read().splitlines()]
        lifespan = [result for result in results if result["kind"] == "lifespan"]
        if completed_lifespan:
            assert len(lifespan) == 1 and lifespan[0]["lifespan_completed"]
            assert lifespan[0]["pool_closed"] and lifespan[0]["model_factory_calls"] == 0
        else:
            assert not lifespan, "Failed shutdown must not confirm a completed lifespan."
        if failed:
            assert code == 1
            assert results[-1] == {"kind": "exit", "error_type": "RuntimeError"}
            resources = [result for result in results if result["kind"] == "resources"]
            assert len(resources) == 1 and resources[0]["pool_closed"]
            assert resources[0]["lifespan_completed"] is completed_lifespan
            assert resources[0]["model_factory_calls"] == 0
        else:
            assert code in (0, -signal.SIGTERM)
        receipt["children"].append({"exit_code": code, "events": results})
        verified_stops.add(process)

    try:
        original, client = start()
        bot = api(
            client,
            "POST",
            "/v1/bots",
            {"name": "Keep Αθήνα", "purpose": "Supervision fixture"},
            201,
        )
        payload = "Saved before worker loss — Αθήνα 東京\n".encode()
        saved = api(
            client,
            "POST",
            f"/v1/bots/{bot['id']}/files/binary",
            {"name": "saved.txt", "data_base64": base64.b64encode(payload).decode()},
            201,
        )
        with store.connect() as db:
            old_lease = tuple(
                db.execute(
                    "SELECT worker_id,epoch,expires FROM worker_leases WHERE owner=?",
                    (config["owner"],),
                )
                .fetchone()
                .values()
            )
        connections(False)
        outage_at = time.monotonic()
        # Normal worker idle polling can start one second after the fault;
        # leave its five-second DB pool allowance enough time to expire too.
        if persistent_outage:
            # Keep the database disabled through the actual supervised exit,
            # including its unsuccessful attempt to release the durable lease.
            original.wait(timeout=25)
            collect(original, failed=True, completed_lifespan=False)
            receipt["original_lifespan_completed"] = False
        else:
            time.sleep(7)
        receipt["database_unavailable_seconds"] = time.monotonic() - outage_at
        connections(True)
        restored_at = time.monotonic()
        if not persistent_outage:
            try:
                original.wait(timeout=15)
            except subprocess.TimeoutExpired:
                receipt["original_still_serving_after_worker_loss"] = original.poll() is None
                status = api(client, "GET", "/v1/status")
                receipt["original_worker_ready"] = status["worker_ready"]
                raise AssertionError(
                    "Hosted HTTP process remained alive after permanent worker loss."
                ) from None
            receipt["exit_after_database_restore_seconds"] = time.monotonic() - restored_at
            collect(original, failed=True)
            receipt["original_lifespan_completed"] = True
        with store.connect() as db:
            lease = tuple(
                db.execute(
                    "SELECT worker_id,epoch,expires FROM worker_leases WHERE owner=?",
                    (config["owner"],),
                )
                .fetchone()
                .values()
            )
            remaining = lease[2] - WorkerLease.now(db)
            assert (lease == old_lease and remaining > 0) if persistent_outage else lease[2] == 0
            assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM provider_calls").fetchone()[0] == 0
        if persistent_outage:
            assert 0 < remaining <= 60
            receipt["lease_seconds_remaining_after_exit"] = remaining
            with pytest.raises(WorkerLeaseLost, match="Another Bots worker"):
                WorkerLease(store, config["owner"]).acquire()
            receipt["replacement_denied_before_expiry"] = True
            waiting_at = time.monotonic()
            while remaining > 0:
                assert time.monotonic() - waiting_at < 61, "Normal lease did not expire."
                time.sleep(min(remaining + 0.05, 1))
                with store.connect() as db:
                    unchanged = tuple(
                        db.execute(
                            "SELECT worker_id,epoch,expires FROM worker_leases WHERE owner=?",
                            (config["owner"],),
                        )
                        .fetchone()
                        .values()
                    )
                    assert unchanged == old_lease, "No lease timestamp or owner may be rewritten."
                    remaining = unchanged[2] - WorkerLease.now(db)
            receipt["normal_expiry_wait_seconds"] = time.monotonic() - waiting_at
        # A completed lifespan releases immediately; an uncertain one requires
        # the unmodified normal lease expiry before a fresh manual controller.
        config["scratch"] = str(tmp_path / "replacement")
        _, replacement = start()
        assert api(replacement, "GET", "/v1/bots")[0]["name"] == bot["name"]
        download = api(replacement, "GET", f"/v1/bots/{bot['id']}/files/{saved['id']}/download")
        assert base64.b64decode(download["data_base64"]) == payload
        assert api(replacement, "GET", "/v1/status")["worker_ready"] is True
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM provider_calls").fetchone()[0] == 0
            assert (
                db.execute(
                    "SELECT epoch FROM worker_leases WHERE owner=?", (config["owner"],)
                ).fetchone()[0]
                == old_lease[1] + 1
            )
        receipt.update(
            preserved_file=True,
            preserved_bot=True,
            lease_released=not persistent_outage,
            fresh_worker_ready=True,
            passed=True,
        )
    finally:
        failures = []
        try:
            connections(True)
            receipt["database_connections_restored"] = True
        except Exception as error:
            receipt["database_connections_restored"] = False
            failures.append(type(error).__name__)
        for process in processes:
            if process in verified_stops:
                continue
            try:
                if process.poll() is None:
                    process.terminate()
                collect(process)
            except Exception as error:
                failures.append(type(error).__name__)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
        for client in clients:
            client.close()
        receipt["all_processes_stopped"] = all(process.poll() is not None for process in processes)
        receipt["fixture_cleanup_complete"] = not failures
        receipt["graceful_cleanup"] = not failures and not persistent_outage
        if failures:
            receipt["passed"] = False
        if output := os.environ.get("ALYSIS_BOTS_SUPERVISION_RECEIPT"):
            path = Path(output)
            if persistent_outage:
                path = path.with_stem(path.stem + "-persistent")
            path.write_text(json.dumps(receipt, indent=2) + "\n")
        assert not failures, "Hosted supervision fixture cleanup failed."


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    try:
        child(json.loads(sys.stdin.readline()))
    except Exception as error:
        print(json.dumps({"kind": "exit", "error_type": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
