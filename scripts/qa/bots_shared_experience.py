"""Bounded real GLM + PostgreSQL + Blob emulator + disposable Linux experience.

No Azure resources or native preview changes. Only the saved Z.ai Coding Plan is
accepted. An independent HTTP process restarts against the same shared state;
synthetic inputs and all model/tool receipts are identified in saved evidence.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "alysis-bots-shared-state-20261020:qa"


def service(config):
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import ComputerSpec
    from alysis_code.bots.compute_docker import DockerComputers
    from alysis_code.bots.object_storage_azure import AzureBlobStorage
    from alysis_code.bots.postgres import PostgresStore

    state = Path(config["state"])
    with ExitStack() as resources:
        objects = AzureBlobStorage(
            config["storage_url"], "study", config["storage_key"], emulator=True
        )
        resources.callback(objects.close)
        store = PostgresStore(config["dsn"], objects=objects, state_directory=state)
        resources.callback(store.close)
        settings = Settings(
            state / "local-config",
            config["token"],
            config["key"],
            config["base_url"],
            config["model"],
            owner=config["owner"],
            max_calls=config["max_calls"],
            output_tokens=2048,
            provider_key="zai_coding_plan",
            reasoning_effort="low",
        )
        provider = DockerComputers(
            config["context"], namespace_profile=ROOT / "scripts/cloud-dev/seccomp.json"
        )
        factory = CloudDesktopFactory(
            settings.owner,
            provider,
            ComputerSpec(config["image"]),
            state / "leases",
            persist_workspace=True,
            store=store,
        )
        app = create_app(settings, task_store=store, cloud_computers=factory)
        if config.get("desktop_study"):
            from alysis_code.bots.compute import ComputerIdentity, ComputerRef

            @app.get("/study/computers")
            def study_computers():
                # Read-only independent guest observations, on this synthetic
                # account only. The production hosted entry point has no route.
                with store.connect() as db:
                    rows = db.execute(
                        "SELECT bot_id,record FROM computer_allocations WHERE owner=?",
                        (settings.owner,),
                    ).fetchall()
                observations = []
                for row in rows:
                    record = json.loads(row["record"])
                    identity = ComputerIdentity(
                        settings.owner, row["bot_id"], record["allocation_id"]
                    )
                    if not record.get("resource_id"):
                        continue
                    ref = ComputerRef(
                        record["provider"], record["scope"], record["resource_id"], identity
                    )
                    state = provider.status(ref).state
                    item = dict(bot_id=row["bot_id"], state=state, markers={})
                    if state == "running":
                        root = f"/tmp/alysis-cloud-{identity.allocation_id}/workspace/"
                        for name in ("phase.txt", "stop-phase.txt", "late.txt"):
                            probe = provider.run(
                                ref, ["/usr/bin/test", "-f", root + name], timeout=5
                            )
                            item["markers"][name] = probe.exit_code == 0
                    observations.append(item)
                return observations

        uvicorn.run(app, host="127.0.0.1", port=7191, access_log=False, log_level="warning")


def until(read, accept=bool, *, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if accept(value):
            return value
        time.sleep(0.25)
    raise RuntimeError("Study condition did not complete within its deadline.")


def run(output, context, app=None, model="glm-5.3-flash", max_calls=32, image=IMAGE):
    import httpx
    from azure.storage.blob import ContainerClient
    from bots_coding_context_study import validate_plan

    from alysis_code.config import load_config, resolve_profile_api_key
    from alysis_code.profiles import get_profile

    settings = load_config()
    profile = get_profile(settings, "zai-coding-plan")
    validate_plan(profile)
    key = resolve_profile_api_key(settings, "zai-coding-plan").key
    if not key:
        raise RuntimeError("Saved Coding Plan unavailable; no fallback.")
    output.mkdir(parents=True, exist_ok=False)
    config = dict(
        key=key,
        base_url=profile.base_url,
        token=secrets.token_urlsafe(40),
        owner="shared-live-" + uuid4().hex,
        context=context,
        dsn=os.environ["ALYSIS_BOTS_TEST_POSTGRES_DSN"],
        state=str(output / "first"),
        storage_key=base64.b64encode(os.urandom(32)).decode(),
        storage_url="http://127.0.0.1:7094/experiencestudy",
        desktop_study=app is not None,
        model=model,
        max_calls=max_calls,
        image=image,
    )
    name = "alysis-bots-experience-" + uuid4().hex[:10]
    receipt = dict(
        passed=False,
        kind="Real GLM, local PostgreSQL18, Azurite3.35 and isolated Linux",
        cloud_deployed=False,
        native_preview_changed=False,
        allowance=max_calls,
        model=model,
        image=image,
        checks={},
        tasks=[],
    )
    process = None
    launched = False
    client = httpx.Client(
        base_url="http://127.0.0.1:7191",
        headers={"Authorization": "Bearer " + config["token"]},
        timeout=30,
    )
    logs = (output / "service.log").open("wb")

    def save():
        (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))

    def api(method, path, data=None):
        r = client.request(method, path, **({"json": data} if data is not None else {}))
        if r.is_error:
            raise RuntimeError("Study HTTP request failed: " + str(r.status_code))
        return r.json() if r.content else None

    def start():
        child = subprocess.Popen(
            [sys.executable, __file__, "--service"],
            stdin=subprocess.PIPE,
            stdout=logs,
            stderr=logs,
        )
        child.stdin.write((json.dumps(config) + "\n").encode())
        child.stdin.close()

        def health():
            if child.poll() is not None:
                raise RuntimeError("Shared service exited during startup.")
            try:
                return api("GET", "/v1/status")["worker_ready"]
            except (httpx.HTTPError, RuntimeError):
                return False

        try:
            until(health, seconds=35)
        except BaseException:
            child.terminate()
            child.wait(20)
            raise
        return child

    def stop():
        nonlocal process
        if process is not None:
            process.terminate()
            try:
                process.wait(90)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(10)
                raise RuntimeError("Graceful shared-service shutdown was not confirmed.") from None
            process = None

    try:
        env = {**os.environ, "AZURITE_ACCOUNTS": "experiencestudy:" + config["storage_key"]}
        result = subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--publish",
                "127.0.0.1:7094:10000",
                "--env",
                "AZURITE_ACCOUNTS",
                "mcr.microsoft.com/azure-storage/azurite:3.35.0",
                "azurite-blob",
                "--blobHost",
                "0.0.0.0",
                "--silent",
                "--skipApiVersionCheck",
            ],
            env=env,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError("Isolated storage emulator did not start.")
        launched = True
        container = ContainerClient(
            config["storage_url"], "study", credential=config["storage_key"], retry_total=0
        )

        def initialize():
            try:
                container.create_container()
                return True
            except Exception:
                return False

        until(initialize, seconds=15)
        container.close()
        process = start()
        assert api("GET", "/v1/status")["state_backend"] == "postgresql"
        assert httpx.get("http://127.0.0.1:7191/v1/status").status_code == 401
        bot = api(
            "POST",
            "/v1/bots",
            dict(
                name="Analyst",
                purpose="Calculate exact totals and preserve current project decisions.",
            ),
        )
        peer = api(
            "POST",
            "/v1/bots",
            dict(
                name="Reviewer",
                purpose="Review requested work using your approved delivery constraints.",
            ),
        )
        chat = api("POST", f"/v1/bots/{bot['id']}/chats", {})
        marker = "承認-" + uuid4().hex[:12]
        api(
            "POST",
            f"/v1/bots/{bot['id']}/memories",
            dict(title="Approval marker", content="The approved marker is " + marker),
        )
        api(
            "POST",
            f"/v1/bots/{peer['id']}/memories",
            dict(title="Delivery constraint", content="Reports must use UTF-8 CSV, never JSON."),
        )
        rows = [
            (city, Decimal(secrets.randbelow(9000) + 100) / 100)
            for city in ("東京", "Αθήνα", "Zürich")
        ]
        source = "city,amount\n" + "".join(f"{city},{amount:.2f}\n" for city, amount in rows)
        total = sum((amount for _, amount in rows), Decimal(0))
        source_file = api(
            "POST", f"/v1/bots/{bot['id']}/files", dict(name="sales.csv", content=source)
        )
        if app is not None:
            browser_config = dict(
                execute_live=True,
                url=str(client.base_url),
                token=config["token"],
                bot=bot,
                peer=peer,
                chat=chat,
                source=source,
                expected_total=f"{total:.2f}",
                evidence=str(output),
                engine_cli=str(Path(sys.executable).parent / "alysis"),
                engine_source=str(ROOT / "src"),
            )
            result = subprocess.run(
                ["node", str(app / "scripts/qa/bots-shared-experience-app.mjs")],
                input=json.dumps(browser_config).encode(),
                stdout=logs,
                stderr=logs,
                cwd=app,
                timeout=900,
            )
            receipt["checks"]["isolated_desktop"] = json.loads(
                (output / "app-result.json").read_text()
            )
            assert result.returncode == 0 and receipt["checks"]["isolated_desktop"]["passed"]
            receipt["passed"] = True
            return

        def send(message):
            return api(
                "POST",
                f"/v1/chats/{chat['id']}/tasks",
                dict(message=message, request_id=uuid4().hex),
            )

        def wait(task):
            final = until(
                lambda: api("GET", f"/v1/tasks/{task['id']}"),
                lambda t: t["status"] in {"completed", "failed", "interrupted", "cancelled"},
            )
            receipt["tasks"].append(final)
            save()
            assert final["status"] == "completed", final["status"]
            return final

        requested = wait(
            send(
                "Use sales.csv from my files to calculate its exact amount total with Decimal on your private computer and return summary.txt containing only the total with two decimal places. Verify the actual file. Ask for computer permission if required."
            )
        )
        proposals = api("GET", f"/v1/bots/{bot['id']}/proposals")
        proposal = next(
            p
            for p in proposals
            if p["source_task_id"] == requested["id"] and p["status"] == "pending"
        )
        approved = api(
            "PATCH",
            f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
            dict(action="accept", expected_revision=proposal["revision"], offline_computer=True),
        )
        wait(dict(id=approved["task_id"]))
        files = api("GET", f"/v1/bots/{bot['id']}/files")
        summary = next(f for f in files if f["name"] == "summary.txt")
        encoded = api("GET", f"/v1/bots/{bot['id']}/files/{summary['id']}/download")["data_base64"]
        payload = base64.b64decode(encoded)
        assert payload.decode().strip() == f"{total:.2f}"
        (output / "summary.txt").write_bytes(payload)
        receipt["checks"]["actual_computer_file"] = dict(
            sha256=hashlib.sha256(payload).hexdigest(), exact_total=str(total)
        )
        assert api("GET", f"/v1/bots/{peer['id']}/files") == []
        review = wait(
            send(
                "Ask Reviewer whether our delivery format should be CSV or JSON, using their approved constraint, and bring their recommendation back here."
            )
        )
        exchanges = until(
            lambda: api("GET", f"/v1/bots/{bot['id']}/exchanges"),
            lambda e: bool(e) and all(r["status"] == "completed" for r in e),
        )
        receipt["checks"]["peer_review"] = exchanges
        receipt["checks"]["peer_parent_task"] = review["id"]

        # An operator sets a deliberately small synthetic account allowance.
        # Actual HTTP writes still cross PostgreSQL and the Blob adapter.
        from alysis_code.bots.object_storage_azure import AzureBlobStorage
        from alysis_code.bots.postgres import PostgresStore

        base_usage = api("GET", "/v1/status")["library_storage"]
        assert base_usage["used_bytes"] == len(source.encode()) + len(payload)
        with ExitStack() as capacity_resources:
            operator_objects = AzureBlobStorage(
                config["storage_url"], "study", config["storage_key"], emulator=True
            )
            capacity_resources.callback(operator_objects.close)
            operator = PostgresStore(
                config["dsn"], objects=operator_objects, state_directory=output / "operator"
            )
            capacity_resources.callback(operator.close)
            limit = base_usage["used_bytes"] + 50
            operator.set_library_limit(config["owner"], limit)
            first = api(
                "POST",
                f"/v1/bots/{bot['id']}/files",
                dict(name="quota-first.txt", content="界" * 10),
            )
            rejected = client.post(
                f"/v1/bots/{peer['id']}/files", json=dict(name="quota-peer.txt", content="a" * 25)
            )
            assert rejected.status_code == 429
            assert api("GET", "/v1/status")["library_storage"]["reserved_bytes"] == 0
            api("DELETE", f"/v1/bots/{bot['id']}/files/{first['id']}")
            retained = api(
                "POST",
                f"/v1/bots/{peer['id']}/files",
                dict(name="quota-peer.txt", content="a" * 25),
            )
            before_note = api("GET", "/v1/status")["library_storage"]
            api(
                "POST",
                f"/v1/bots/{peer['id']}/notes",
                dict(title="Storage study", content="This verification used synthetic files."),
            )
            assert api("GET", "/v1/status")["library_storage"] == before_note
            operator.set_library_limit(config["owner"], 0)
            assert (
                base64.b64decode(
                    api("GET", f"/v1/bots/{peer['id']}/files/{retained['id']}/download")[
                        "data_base64"
                    ]
                )
                == b"a" * 25
            )
            operator.set_library_limit(config["owner"], limit)
            usage_expected = api("GET", "/v1/status")["library_storage"]
            assert usage_expected["used_bytes"] == base_usage["used_bytes"] + 25
            receipt["checks"]["shared_library_admission"] = usage_expected
        wait(
            send(
                "In 45 seconds remind me of my exact approved marker from memory. Create that one-time reminder now."
            )
        )
        schedules = api("GET", f"/v1/bots/{bot['id']}/schedules")
        assert len(schedules) == 1 and schedules[0]["timing"]["kind"] == "once"
        usage_before = api("GET", "/v1/status")["usage"]
        stop()
        config["state"] = str(output / "replacement")
        process = start()
        assert api("GET", "/v1/status")["usage"]["calls"] >= usage_before["calls"]
        assert api("GET", "/v1/status")["library_storage"] == usage_expected
        assert api("GET", f"/v1/bots/{bot['id']}/memories")[0]["content"].endswith(marker)
        restored = base64.b64decode(
            api("GET", f"/v1/bots/{bot['id']}/files/{summary['id']}/download")["data_base64"]
        )
        assert restored == payload
        assert (
            base64.b64decode(
                api("GET", f"/v1/bots/{bot['id']}/files/{source_file['id']}/download")[
                    "data_base64"
                ]
            )
            == source.encode()
        )
        schedule = schedules[0]
        runs = until(
            lambda: api("POST", f"/v1/bots/{bot['id']}/schedules/{schedule['id']}/runs", {})["runs"]
        )
        fired = wait(dict(id=runs[0]["task_id"]))
        assert marker in fired["output"]
        assert (
            len(api("POST", f"/v1/bots/{bot['id']}/schedules/{schedule['id']}/runs", {})["runs"])
            == 1
        )
        receipt["checks"]["process_restart_files_memory_usage_schedule"] = True
        receipt["passed"] = True
    except Exception as error:
        receipt["failure_type"] = type(error).__name__
        raise
    finally:
        try:
            receipt["usage"] = api("GET", "/v1/status")["usage"]
        except Exception:
            receipt["usage_unavailable"] = True
        try:
            stop()
            receipt["service_stopped"] = True
        finally:
            # Independently reconcile only this unique synthetic account after
            # its controller exits, including a failed study's orphan computer.
            import psycopg

            from alysis_code.bots.compute import ComputerIdentity, ComputerRef, wait_for
            from alysis_code.bots.compute_docker import DockerComputers

            provider = DockerComputers(context)
            cleanup = []
            try:
                with psycopg.connect(config["dsn"]) as db:
                    allocations = db.execute(
                        "SELECT bot_id,record FROM computer_allocations WHERE owner=%s",
                        (config["owner"],),
                    ).fetchall()
                for bot_id, encoded in allocations:
                    record = json.loads(encoded)
                    identity = ComputerIdentity(config["owner"], bot_id, record["allocation_id"])
                    ref = (
                        ComputerRef(
                            record["provider"], record["scope"], record["resource_id"], identity
                        )
                        if record.get("resource_id")
                        else provider.find(identity)
                    )
                    if ref:
                        state = provider.status(ref).state
                        if state == "running":
                            provider.stop(ref)
                            wait_for(provider, ref, "stopped", timeout=30)
                            state = "stopped"
                        if state == "stopped":
                            provider.delete(ref)
                        assert provider.status(ref).state == "missing"
                    cleanup.append({"bot_id": bot_id, "computer_missing": True})
                receipt["computer_cleanup"] = cleanup
            except Exception as error:
                receipt["cleanup_error"] = type(error).__name__
                receipt["passed"] = False
            client.close()
            logs.close()
            if launched:
                removed = subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True, timeout=30
                )
                receipt["emulator_removed"] = removed.returncode == 0
            save()
        print(
            json.dumps({k: receipt[k] for k in ("passed", "checks", "usage") if k in receipt}),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--context", default="desktop-linux")
    parser.add_argument("--image", default=IMAGE, help="Explicit qualified worker image")
    parser.add_argument("--model", choices=("glm-5.3-flash", "glm-5.3"), default="glm-5.3-flash")
    parser.add_argument("--max-calls", type=int, choices=range(1, 33), default=32)
    parser.add_argument(
        "--app", type=Path, help="Run the isolated real Electron background/Stop study"
    )
    args = parser.parse_args()
    try:
        if args.service:
            service(json.loads(sys.stdin.readline()))
        elif args.execute_live and args.output:
            run(
                args.output.resolve(),
                args.context,
                args.app.resolve() if args.app else None,
                model=args.model,
                max_calls=args.max_calls,
                image=args.image,
            )
        else:
            parser.error(
                "Use --execute-live --output with disposable PostgreSQL DSN (32 calls maximum)."
            )
    except Exception as error:
        raise SystemExit("Shared experience failed: " + type(error).__name__) from None
