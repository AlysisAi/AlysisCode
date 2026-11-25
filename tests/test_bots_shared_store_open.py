"""A shared store opened beside the running controller (backups, recovery, release and maintenance
tools) only reads that the schema is this release's. Setting the schema up takes exclusive locks
on the tables even when nothing changes, so running owl work would wait behind it and stop with
"storage briefly unavailable". Only the controller's own start sets it up.

The first tests record what a store sends to the database through a stand-in connection pool; the
last one (opt-in, like the other PostgreSQL 18 tests) shows the locks on a real server."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from alysis_code.bots import hosted_operations
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.schema import SCHEMA_VERSION

psycopg_pool = pytest.importorskip("psycopg_pool")
from alysis_code.bots.postgres import PostgresStore, SharedDatabaseError  # noqa: E402


class Cursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.description = [SimpleNamespace(name="value")]

    def fetchone(self):
        return self._rows[0] if self._rows else None


class Database:
    """A PostgreSQL server as a store sees it: what it was sent, and what it answers."""

    def __init__(self, *, version=SCHEMA_VERSION, schema_table=True):
        self.version, self.schema_table = version, schema_table
        self.sent: list[str] = []
        self.closed = False

    def answer(self, sql):
        self.sent.append(" ".join(sql.split()))
        if sql.startswith("SHOW server_encoding"):
            return [("UTF8",)]
        if "to_regclass('bots_schema')" in sql:
            return [(self.schema_table,)]
        if sql.startswith("SELECT version FROM bots_schema"):
            return [] if self.version is None else [(self.version,)]
        return []


class Raw:
    def __init__(self, database):
        self.database = database
        self.info = SimpleNamespace(server_version=180001)

    @contextmanager
    def transaction(self):
        yield

    def execute(self, sql, args=None):
        return Cursor(self.database.answer(sql))


@pytest.fixture
def database(monkeypatch):
    database = Database()

    class Pool:
        check_connection = staticmethod(lambda connection: None)

        def __init__(self, *args, **kwargs):
            pass

        def getconn(self):
            return Raw(database)

        def putconn(self, raw):
            pass

        def close(self):
            database.closed = True

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", Pool)
    return database


def opened(tmp_path, **options):
    return PostgresStore(
        "postgresql://stand-in/bots",
        objects=LocalObjectStorage(tmp_path / "objects"),
        state_directory=tmp_path / "state",
        **options,
    )


CHANGES = ("CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ", "DROP ", "pg_advisory")


def test_a_store_opened_beside_the_controller_only_reads_the_schema(tmp_path, database):
    store = opened(tmp_path, set_up_schema=False)
    store.close()
    assert database.sent and not [s for s in database.sent if any(c in s for c in CHANGES)]
    assert any(s.startswith("SELECT version FROM bots_schema") for s in database.sent)


def test_the_controller_still_sets_the_schema_up_as_it_starts(tmp_path, database):
    opened(tmp_path).close()
    assert any(s.startswith("ALTER TABLE files") for s in database.sent)
    assert any("pg_advisory_xact_lock" in s for s in database.sent)


@pytest.mark.parametrize(
    "state",
    [
        {"version": SCHEMA_VERSION - 1},
        {"version": SCHEMA_VERSION + 1},
        {"version": None},
        {"schema_table": False},
    ],
    ids=["older", "newer", "no-version", "empty-database"],
)
def test_a_database_not_set_up_for_this_release_is_refused_unchanged(tmp_path, database, state):
    for key, value in state.items():
        setattr(database, key, value)
    with pytest.raises(RuntimeError, match="not set up for this release"):
        opened(tmp_path, set_up_schema=False)
    assert database.closed
    assert not [s for s in database.sent if any(c in s for c in CHANGES)]


def test_maintenance_tools_open_the_store_without_setting_the_schema_up(tmp_path, monkeypatch):
    seen = {}

    class Recorded:
        def __init__(self, dsn, **options):
            seen.update(options, dsn=dsn)

        def close(self):
            seen["closed"] = True

    class Blobs:
        def __init__(self, *args):
            pass

        def close(self):
            pass

    monkeypatch.setattr("alysis_code.bots.postgres.PostgresStore", Recorded)
    monkeypatch.setattr("alysis_code.bots.object_storage_azure.AzureBlobStorage", Blobs)
    values = {
        "hosted": {
            "database_dsn": "postgresql://stand-in/bots",
            "storage_url": "https://storage.invalid",
            "storage_container": "bots",
            "state_directory": str(tmp_path),
        }
    }
    with hosted_operations.maintenance_store(values, credential=None):
        pass
    assert seen["set_up_schema"] is False and seen["closed"]


def test_running_work_does_not_wait_while_a_maintenance_store_opens(tmp_path):
    dsn = os.environ.get("ALYSIS_BOTS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set ALYSIS_BOTS_TEST_POSTGRES_DSN for disposable PostgreSQL 18 tests")
    objects = LocalObjectStorage(tmp_path / "objects")
    controller = PostgresStore(dsn, objects=objects, state_directory=tmp_path / "controller")
    reading, release = threading.Event(), threading.Event()

    def running_work():
        # A turn of an owl reading its Library, in a transaction still open.
        with controller.connect() as db:
            db.execute("SELECT count(*) FROM files").fetchone()
            reading.set()
            release.wait(30)

    work = threading.Thread(target=running_work)
    work.start()
    try:
        assert reading.wait(10)
        began = time.monotonic()
        PostgresStore(
            dsn, objects=objects, state_directory=tmp_path / "maintenance", set_up_schema=False
        ).close()
        assert time.monotonic() - began < 3
        # Setting the schema up would have waited for that work's table lock, and given up.
        with pytest.raises(SharedDatabaseError):
            PostgresStore(dsn, objects=objects, state_directory=tmp_path / "setup")
    finally:
        release.set()
        work.join(30)
        controller.close()
