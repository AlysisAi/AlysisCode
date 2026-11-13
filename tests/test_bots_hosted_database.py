"""Opt-in actual PostgreSQL pilot bootstrap, authentication and disk restart."""

import importlib.util
import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from alysis_code.bots.models import BotInput, FileInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore

spec = importlib.util.spec_from_file_location(
    "pilot_database", Path(__file__).parents[1] / "scripts/qa/bots_hosted_database.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.mark.parametrize("mount_kind", ["volume", "native_bind"])
def test_real_pilot_database_restricts_access_and_survives_restart(tmp_path, mount_kind):
    if os.environ.get("ALYSIS_BOTS_TEST_DOCKER") != "1":
        pytest.skip("Set ALYSIS_BOTS_TEST_DOCKER=1 for an isolated real PostgreSQL container")
    import psycopg
    from psycopg.conninfo import make_conninfo

    name = "alysis-bots-pg-check-" + uuid4().hex[:12]
    volume = name + "-data"
    password = secrets.token_urlsafe(36)
    store = None
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        host_port = reserved.getsockname()[1]

    def docker(*args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError("Isolated database Docker operation failed.")
        return result.stdout + result.stderr if args[0] == "logs" else result.stdout

    def ready():
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                setup.run(name, ["pg_isready", "-U", "postgres"])
                return
            except RuntimeError:
                time.sleep(0.2)
        raise AssertionError("Isolated PostgreSQL did not become ready")

    try:
        docker("volume", "create", volume)
        mount = "type=volume,source=" + volume + ",target=/var/lib/postgresql"
        if mount_kind == "native_bind":
            # Keep the fixture directory on the daemon's Linux filesystem. A
            # macOS shared bind can bypass Linux traversal checks, and an empty
            # named-volume mount can copy the image's ownership on attachment.
            native_root = json.loads(docker("volume", "inspect", volume))[0]["Mountpoint"]
            prepared = docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "root",
                "--entrypoint",
                "bash",
                "--mount",
                "type=volume,source=" + volume + ",target=/fixture,volume-nocopy",
                setup.IMAGE,
                "-ceu",
                "mkdir /fixture/private; chown root:root /fixture/private; "
                "chmod 0700 /fixture/private; "
                "printf 'preserve unrelated bytes' > /fixture/private/preserved.txt; "
                "chmod 0600 /fixture/private/preserved.txt; "
                "stat -c '%U:%G %a' /fixture/private",
            )
            assert prepared.strip() == "root:root 700"
            mount = "type=bind,source=" + native_root + "/private,target=/var/lib/postgresql"
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--cpus",
            "1",
            "--memory",
            "768m",
            "--pids-limit",
            "128",
            "--publish",
            f"127.0.0.1:{host_port}:5432",
            "--mount",
            mount,
            "--env",
            "PGDATA=" + setup.PGDATA,
            "--entrypoint",
            "bash",
            setup.IMAGE,
            "-ceu",
            "sleep 2; " + setup.server_command()[2],
        )
        waiting_started = time.monotonic()
        setup.wait_ready(name)
        assert time.monotonic() - waiting_started >= 1
        info = json.loads(docker("inspect", name))[0]
        actual_mount = next(m for m in info["Mounts"] if m["Destination"] == "/var/lib/postgresql")
        assert actual_mount["Type"] == ("bind" if mount_kind == "native_bind" else "volume")
        port = info["NetworkSettings"]["Ports"]["5432/tcp"][0]
        assert port["HostIp"] == "127.0.0.1"
        params = dict(host="127.0.0.1", port=port["HostPort"], connect_timeout=2)
        # No unauthenticated TCP admin access exists, even before setup.
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(**params, user="postgres", dbname="postgres")
        result = setup.configure(name, password)
        assert result["prepared"] and result["auth"] == "scram-sha-256"
        for bad in ("", "incorrect-password"):
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(**params, user="alysis", dbname="alysis_bots", password=bad)
        dsn = make_conninfo(**params, user="alysis", dbname="alysis_bots", password=password)
        with psycopg.connect(dsn) as db:
            assert (
                db.execute(
                    "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                    "FROM pg_roles WHERE rolname=current_user"
                ).fetchone()
                == (False,) * 5
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute("CREATE ROLE forbidden_superuser SUPERUSER")
            db.rollback()
        store = PostgresStore(
            dsn, objects=LocalObjectStorage(tmp_path / "objects"), state_directory=tmp_path / "one"
        )
        bot = store.save_bot("pilot", BotInput(name="Αθήνα 東京", purpose="Durable pilot"))
        file = store.save_file("pilot", bot["id"], FileInput(name="日本語.txt", content="kept Ω"))
        docker("restart", name)
        ready()
        # The real pool replaces connections to the old server; disk records and
        # file references remain usable. Objects here are local, not Azure proof.
        assert store.bot("pilot", bot["id"])["name"] == "Αθήνα 東京"
        assert b"".join(store.file_chunks("pilot", bot["id"], file["id"])) == "kept Ω".encode()
        if mount_kind == "native_bind":
            assert docker(
                "exec",
                "--user",
                "root",
                name,
                "stat",
                "-c",
                "%U:%G %a",
                "/var/lib/postgresql",
                "/var/lib/postgresql/18",
            ).splitlines() == ["postgres:postgres 700", "postgres:postgres 700"]
            assert (
                docker(
                    "exec",
                    "--user",
                    "root",
                    name,
                    "stat",
                    "-c",
                    "%U:%G %a",
                    "/var/lib/postgresql/preserved.txt",
                ).strip()
                == "root:root 600"
            )
            assert (
                docker(
                    "exec",
                    "--user",
                    "root",
                    name,
                    "cat",
                    "/var/lib/postgresql/preserved.txt",
                )
                == "preserve unrelated bytes"
            )
        if password in docker("inspect", name) or password in docker("logs", name):
            # Pytest assertion introspection must not reveal a leaked password.
            raise AssertionError("A bootstrap credential was persisted by the container.")
        # Explicit rotation preserves data and invalidates the previous password.
        rotated = secrets.token_urlsafe(36)
        setup.configure(name, rotated)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(dsn)
        with psycopg.connect(**params, user="alysis", dbname="alysis_bots", password=rotated) as db:
            assert (
                db.execute("SELECT name FROM bots WHERE id=%s", (bot["id"],)).fetchone()[0]
                == "Αθήνα 東京"
            )
        if rotated in docker("logs", name):
            raise AssertionError("A rotated credential appeared in database logs.")
    finally:
        if store:
            store.close()
        # These uniquely named resources contain only this test's synthetic data.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=30)


@pytest.mark.parametrize("layout", ["nonempty", "symlink"])
def test_private_bind_refuses_uninitialized_data_or_symlink_without_overwriting(layout):
    if os.environ.get("ALYSIS_BOTS_TEST_DOCKER") != "1":
        pytest.skip("Set ALYSIS_BOTS_TEST_DOCKER=1 for an isolated real PostgreSQL container")
    name = "alysis-bots-pg-guard-" + uuid4().hex[:12]
    volume = name + "-fixture"

    def docker(*args):
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def fixture(command):
        result = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "root",
            "--entrypoint",
            "bash",
            "--mount",
            "type=volume,source=" + volume + ",target=/fixture,volume-nocopy",
            setup.IMAGE,
            "-ceu",
            command,
        )
        assert result.returncode == 0, "Private bind fixture operation failed."
        return result.stdout.strip()

    try:
        assert docker("volume", "create", volume).returncode == 0
        inspected = docker("volume", "inspect", volume)
        assert inspected.returncode == 0
        native = json.loads(inspected.stdout)[0]["Mountpoint"] + "/private"
        prefix = "mkdir -p /fixture/private/18; chmod 0700 /fixture/private; "
        if layout == "symlink":
            prefix += "mkdir /fixture/private/other; ln -s ../other /fixture/private/18/pilot; "
        else:
            prefix += "mkdir /fixture/private/18/pilot; "
        fixture(
            prefix + "printf 'retained Ω bytes' > /fixture/private/18/pilot/retained.txt; "
            "chmod 0600 /fixture/private/18/pilot/retained.txt"
        )
        attempted = docker(
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--entrypoint",
            "bash",
            "--mount",
            "type=bind,source=" + native + ",target=/var/lib/postgresql",
            "--env",
            "PGDATA=" + setup.PGDATA,
            setup.IMAGE,
            *setup.server_command()[1:],
        )
        assert attempted.returncode != 0, "Uninitialized private data was not refused."
        assert fixture(
            "test ! -e /fixture/private/18/pilot/PG_VERSION; "
            "cat /fixture/private/18/pilot/retained.txt; echo; "
            "stat -c '%U:%G %a' /fixture/private/18/pilot/retained.txt"
        ).splitlines() == ["retained Ω bytes", "root:root 600"]
        if layout == "symlink":
            assert fixture("readlink /fixture/private/18/pilot") == "../other"
    finally:
        docker("rm", "-f", name)
        assert docker("volume", "rm", volume).returncode == 0


@pytest.mark.parametrize("password", ["short", "x" * 32 + "'", "x" * 129, None])
def test_reject_bootstrap_password_before_running_commands(password):
    with pytest.raises(ValueError, match="random URL-safe"):
        setup.configure("alysis-bots-test", password)
