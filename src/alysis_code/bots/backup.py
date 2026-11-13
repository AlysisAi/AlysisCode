"""Maintenance-only PostgreSQL and immutable-object backup/restore.

Stop every controller/API/retention worker first, and fence the old deployment
before starting a restored one. The application write lock prevents cooperating
writers racing the snapshot; it is not a distributed deployment switch. No
worker is started, lease cleared, task replayed or computer ownership rewritten.
Archives are trusted operator data, not user uploads. Protect/encrypt the backup
destination like the original database. This module does not provision storage.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .object_storage import LocalObjectStorage, ObjectRef
from .postgres import WRITE_LOCK
from .schema import SCHEMA_VERSION


class BackupError(RuntimeError):
    """Operator error without connection strings, credentials or database output."""


def _fingerprint(path):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(262144), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def postgres_environment(dsn):
    """Map libpq's own connection options to their documented environment names."""
    from psycopg.pq import Conninfo

    environment = {}
    for option in Conninfo.parse(dsn.encode()):
        if option.val is not None:
            if not option.envvar:
                raise BackupError("A connection option cannot be passed privately to pg_dump.")
            environment[option.envvar.decode()] = option.val.decode()
    environment["PGCONNECT_TIMEOUT"] = "5"
    return environment


def _run(command, dsn, **streams):
    # Never put credentials in argv or forward upstream stderr (which may include
    # connection details or SQL data). PGDATABASE alone does not expand a DSN.
    try:
        result = subprocess.run(
            command,
            env={**os.environ, **postgres_environment(dsn)},
            stderr=subprocess.DEVNULL,
            timeout=300,
            **streams,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BackupError("PostgreSQL archive operation did not complete.") from None
    if result.returncode:
        raise BackupError("PostgreSQL archive operation failed; no success confirmed.")


def dump_database(dsn, snapshot, path, *, run=_run):
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        run(
            [
                "pg_dump",
                "--format=custom",
                "--schema=public",
                "--no-owner",
                "--no-privileges",
                "--snapshot=" + snapshot,
            ],
            dsn,
            stdout=stream,
        )
        stream.flush()
        os.fsync(stream.fileno())


def restore_database(dsn, path, *, run=_run):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = postgres_environment(dsn).get("PGDATABASE")
    if not name:
        raise BackupError("Restore requires an explicit database name.")
    # Only this maintenance client writes to the fresh target. Ordinary new
    # connections remain read-only until the operator reviews stale work and
    # reconciles provider resources created after the backup's point in time.
    options = conninfo_to_dict(dsn).get("options", "")
    maintenance_dsn = make_conninfo(dsn, options=options + " -c default_transaction_read_only=off")
    with path.open("rb") as stream:
        run(
            [
                "pg_restore",
                "--dbname",
                make_conninfo(dbname=name),
                "--single-transaction",
                "--exit-on-error",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
            ],
            maintenance_dsn,
            stdin=stream,
            stdout=subprocess.DEVNULL,
        )


@contextmanager
def _maintenance(dsn):
    import psycopg

    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as db:
            db.execute("SET statement_timeout='30s'")
            db.execute("SET idle_in_transaction_session_timeout='15min'")
            if not db.execute("SELECT pg_try_advisory_lock(%s)", (WRITE_LOCK,)).fetchone()[0]:
                raise BackupError("Database is busy; stop controllers before maintenance.")
            # The session lock is released even on failure when the connection
            # closes. Do not put it in the exported read-only transaction.
            yield db
    except psycopg.Error:
        raise BackupError("Backup database operation failed; no success confirmed.") from None


def _references(db):
    refs = {}
    for (encoded,) in db.execute(
        "SELECT ref FROM file_objects UNION SELECT ref FROM workspace_objects UNION SELECT ref FROM browser_states"
    ):
        ref = ObjectRef.decode(encoded)
        if ref.key in refs and refs[ref.key] != ref:
            raise BackupError("Database has conflicting object references.")
        refs[ref.key] = ref
    return sorted(refs.values(), key=lambda ref: ref.key)


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_backup(dsn, objects, directory, *, dump=dump_database):
    """Create a new private directory; publish its manifest only after verification.

    `dump` is the PostgreSQL client transport. The default runs installed pg_dump;
    local Docker qualification uses the same real utility inside its DB container.
    A failed backup remains incomplete, without a completion manifest. No existing
    destination, source object or database is removed/replaced.
    """
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    destination = LocalObjectStorage(directory / "objects")
    started = time.monotonic()
    with _maintenance(dsn) as db, db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        schema = db.execute("SELECT version FROM bots_schema WHERE id=1").fetchone()
        if schema != (SCHEMA_VERSION,):
            raise BackupError("Migrate the Bots schema before creating a backup.")
        if db.execute(
            "SELECT 1 FROM worker_leases WHERE expires>extract(epoch FROM clock_timestamp()) LIMIT 1"
        ).fetchone():
            raise BackupError("Stop all account workers before creating a backup.")
        snapshot = db.execute("SELECT pg_export_snapshot()").fetchone()[0]
        refs = _references(db)
        dump(dsn, snapshot, directory / "database.dump")
        for ref in refs:
            if time.monotonic() - started > 600:
                raise BackupError("Backup exceeded its maintenance window.")
            destination.copy_from(objects, ref)
        manifest = {
            "format": 1,
            "schema": SCHEMA_VERSION,
            "created_at": time.time(),
            "database": _fingerprint(directory / "database.dump"),
            "objects": [json.loads(ref.encode()) for ref in refs],
        }
        # Validate the archive header as well as its content hash. pg_restore
        # still validates the actual archive when restoring transactionally.
        with (directory / "database.dump").open("rb") as stream:
            if stream.read(5) != b"PGDMP":
                raise BackupError("PostgreSQL did not produce a custom archive.")
    encoded = json.dumps(manifest, separators=(",", ":")) + "\n"
    if len(encoded.encode()) > 16 * 1024 * 1024:
        raise BackupError("Backup manifest is too large for this pilot.")
    with (directory / "manifest.pending").open("x") as stream:
        os.chmod(stream.name, 0o600)
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.link(directory / "manifest.pending", directory / "manifest.json")
    (directory / "manifest.pending").unlink()
    _fsync_directory(directory)
    return manifest


def verify_backup(directory):
    """Read every archived byte; a missing/corrupt/incomplete backup fails closed."""
    directory = Path(directory)
    try:
        path = directory / "manifest.json"
        if path.stat().st_size > 16 * 1024 * 1024:
            raise BackupError("Backup manifest is too large for this pilot.")
        manifest = json.loads(path.read_text())
        if manifest["format"] != 1 or manifest["schema"] != SCHEMA_VERSION:
            raise BackupError("Unsupported backup format or Bots schema.")
        if _fingerprint(directory / "database.dump") != manifest["database"]:
            raise BackupError("Backup database archive failed its integrity check.")
        source = LocalObjectStorage(directory / "objects")
        keys = set()
        for item in manifest["objects"]:
            ref = ObjectRef.decode(json.dumps(item))
            if ref.key in keys:
                raise BackupError("Backup contains duplicate object identities.")
            keys.add(ref.key)
            digest = hashlib.sha256()
            for chunk in source.read(ref):
                digest.update(chunk)
            if digest.hexdigest() != ref.sha256:
                raise BackupError("Backup object failed its integrity check.")
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        raise BackupError("Backup is incomplete or its manifest is invalid.") from None


def restore_backup(dsn, objects, directory, *, restore=restore_database):
    """Restore into a fresh database and empty object destination only.

    Objects verify before database publication. The database restore is one
    transaction. Failed copies can leave unreachable destination objects; discard
    that new destination or inspect it, never retry into an existing deployment.
    The fresh database remains read-only, even after a failed restore. An operator
    must reconcile the old provider inventory and review potentially stale queued
    work/schedules before enabling writes. This is not automatic failover.
    """
    from psycopg import sql

    directory = Path(directory)
    manifest = verify_backup(directory)
    source = LocalObjectStorage(directory / "objects")
    refs = [ObjectRef.decode(json.dumps(item)) for item in manifest["objects"]]
    with _maintenance(dsn) as db:
        # Reject relations (including views/sequences), types, functions and non-system
        # schemas. An empty public schema in a freshly created DB is expected.
        occupied = db.execute("""
            SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema')
            OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                WHERE left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema')
            OR EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
                WHERE left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema')
            OR EXISTS (SELECT 1 FROM pg_namespace WHERE left(nspname,3)<>'pg_'
                AND nspname NOT IN ('public','information_schema'))
        """).fetchone()[0]
        if occupied:
            raise BackupError("Restore requires a fresh, empty database.")
        name = db.execute("SELECT current_database()").fetchone()[0]
        db.execute(
            sql.SQL("ALTER DATABASE {} SET default_transaction_read_only=on").format(
                sql.Identifier(name)
            )
        )
        for ref in refs:
            objects.copy_from(source, ref)
        restore(dsn, directory / "database.dump")
        if db.execute("SELECT version FROM bots_schema WHERE id=1").fetchone() != (SCHEMA_VERSION,):
            raise BackupError("Restored database schema does not match the backup.")
        if _references(db) != sorted(refs, key=lambda ref: ref.key):
            raise BackupError("Restored database and archived files do not match.")
    return {
        "verified": True,
        "objects": len(refs),
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "requires_operator_review": True,
    }
