"""Shared PostgreSQL 18+ store. No host path is used for bot state.

The small adapter binds existing SQL parameters and supplies SQLite-like rows;
queries and schema differences remain explicit. Write transactions currently
serialize through a database advisory lock, preserving the store's multi-query
invariants. This is a correctness baseline, not a high-throughput fleet claim.
"""

from contextlib import contextmanager
from pathlib import Path

from .files import FILE_CHUNK_BYTES
from .object_storage import ObjectStorageError
from .schema import SCHEMA_VERSION, STEER_COLUMNS, STEER_INDEX, schema
from .store import Store
from .task_events import TaskSignals

# Stable application lock, scoped to the connected database by PostgreSQL.
WRITE_LOCK = 0x414C59534953424F


class SharedDatabaseError(RuntimeError):
    """Database access failed; the caller must not infer or replay success."""


def parameters(sql):
    """Translate qmark bindings outside SQL literals/comments, without values."""
    result, i, quote = [], 0, None
    while i < len(sql):
        c = sql[i]
        if quote:
            result.append(c)
            if c == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    result.append(quote)
                    i += 1
                else:
                    quote = None
        elif c in ("'", '"'):
            quote = c
            result.append(c)
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            end = len(sql) if end < 0 else end
            result.append(sql[i:end])
            i = end - 1
        elif sql.startswith("/*", i):
            end, depth = i + 2, 1
            while depth and end < len(sql):
                if sql.startswith("/*", end):
                    depth += 1
                    end += 2
                elif sql.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            if depth:
                raise ValueError("Unclosed SQL comment.")
            result.append(sql[i:end])
            i = end - 1
        elif c == "?":
            result.append("\x00")
        else:
            result.append(c)
        i += 1
    if quote:
        raise ValueError("Unclosed SQL literal.")
    return "".join(result).replace("%", "%%").replace("\x00", "%s")


class Row(dict):
    def __init__(self, names, values):
        self._values = tuple(values)
        super().__init__()
        for name, value in zip(names, values, strict=True):
            self.setdefault(name, value)

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else super().__getitem__(key)


def rows(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


class Connection:
    def __init__(self, connection):
        self.raw = connection
        # The tasks this transaction wrote events for, woken once it commits (task_events.py).
        self.event_tasks = set()

    def execute(self, sql, args=None):
        # Boolean fields use the shared INTEGER schema on both backends.
        args = None if args is None else tuple(int(v) if type(v) is bool else v for v in args)
        return self.raw.execute(parameters(sql) if args is not None else sql, args)


class PostgresStore(Store):
    """The shared bot state in PostgreSQL.

    ``set_up_schema``: create and upgrade the schema as the store opens. Only the controller's own
    start does this (and stores for a new database). Setting it up takes exclusive locks on the
    tables, even when nothing changes, so a store opened beside a running controller (backups,
    health checks, release and maintenance tools: hosted_operations.maintenance_store) only
    reads that the database is this release's: running owl work never waits behind it.
    """

    shared = True

    def __init__(self, dsn: str, *, objects, state_directory: Path, set_up_schema: bool = True):
        if objects is None:
            raise ObjectStorageError("Shared bot state requires configured object storage.")
        self._dsn, self.objects = dsn, objects
        self.task_signals = TaskSignals()
        # This directory is scratch/connection configuration only, never bot state.
        state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = state_directory / "shared-bots"
        self._fence = None
        import psycopg
        from psycopg_pool import ConnectionPool

        class PrivateConnection(psycopg.Connection):
            @classmethod
            def connect(cls, *args, **kwargs):
                try:
                    return super().connect(*args, **kwargs)
                except psycopg.Error:
                    # Pool background workers log connection exceptions. Strip
                    # upstream connection strings before they reach that logger.
                    raise psycopg.OperationalError(
                        "Shared database connection unavailable."
                    ) from None

        self._pool = ConnectionPool(
            self._dsn,
            connection_class=PrivateConnection,
            kwargs={"autocommit": True, "row_factory": rows, "connect_timeout": 5},
            min_size=0,
            max_size=8,
            max_waiting=128,
            timeout=5,
            num_workers=2,
            max_idle=30,
            check=ConnectionPool.check_connection,
            open=True,
        )
        try:
            if set_up_schema:
                self._initialize_schema()
            else:
                self._check_schema()
        except BaseException:
            self.close()
            raise

    def close(self):
        self._pool.close()

    @staticmethod
    def _check_server(db):
        if db.raw.info.server_version < 180000:
            raise RuntimeError("Shared Bots requires PostgreSQL 18+ with UTF-8 Unicode folding.")
        if db.execute("SHOW server_encoding").fetchone()[0] != "UTF8":
            raise RuntimeError("Shared Bots requires UTF-8 database encoding.")

    def _check_schema(self):
        """Read, without changing or locking anything, that the database is set up for this
        release (its controller sets it up as it starts)."""
        with self.connect() as db:
            self._check_server(db)
            version = None
            if db.execute("SELECT to_regclass('bots_schema') IS NOT NULL").fetchone()[0]:
                row = db.execute("SELECT version FROM bots_schema WHERE id=1").fetchone()
                version = row[0] if row else None
            if version != SCHEMA_VERSION:
                raise RuntimeError(
                    "The shared Bots database is not set up for this release; its controller "
                    "sets it up when it starts."
                )

    def _initialize_schema(self):
        with self.connect(write=True) as db:
            self._check_server(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS bots_schema (id INTEGER PRIMARY KEY,version INTEGER NOT NULL)"
            )
            old = db.execute("SELECT version FROM bots_schema WHERE id=1").fetchone()
            if old and old[0] not in range(19, SCHEMA_VERSION + 1):
                raise RuntimeError(
                    "Unsupported shared Bots schema; migrate before starting workers."
                )
            db.raw.execute(schema(postgres=True))
            db.execute("ALTER TABLE computer_capacity ALTER COLUMN max_active DROP NOT NULL")
            # Preserve all foreign keys and object references while upgrading
            # the legacy name-unique Library inside this write transaction.
            db.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS series_id TEXT")
            db.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS revision INTEGER")
            db.execute("UPDATE files SET series_id=id,revision=1 WHERE series_id IS NULL")
            db.execute("ALTER TABLE files ALTER COLUMN series_id SET NOT NULL")
            db.execute("ALTER TABLE files ALTER COLUMN revision SET NOT NULL")
            db.execute("ALTER TABLE files DROP CONSTRAINT IF EXISTS files_bot_id_name_key")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS files_bot_id_name_revision_key ON files(bot_id,name,revision)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS files_series ON files(bot_id,series_id,revision)"
            )
            db.execute("ALTER TABLE file_uploads ADD COLUMN IF NOT EXISTS previous_file_id TEXT")
            for table in ("workspace_objects", "browser_states"):
                db.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_task_id_key")
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1))"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS computer_allocations_active_scope "
                "ON computer_allocations ((record::jsonb->>'scope')) "
                "WHERE (record::jsonb->>'state') IS DISTINCT FROM 'deleted'"
            )
            db.execute("ALTER TABLE action_proposals ADD COLUMN IF NOT EXISTS browser_origin TEXT")
            db.execute("ALTER TABLE schedule_runs ADD COLUMN IF NOT EXISTS origin TEXT")
            db.execute(
                "ALTER TABLE action_proposals ADD COLUMN IF NOT EXISTS offline_computer INTEGER NOT NULL DEFAULT 0"
            )
            db.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS failure TEXT")
            db.execute(
                "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS restarts INTEGER NOT NULL DEFAULT 0"
            )
            db.execute("ALTER TABLE bot_action_reviews ADD COLUMN IF NOT EXISTS effect TEXT")
            db.execute("ALTER TABLE bots ADD COLUMN IF NOT EXISTS color TEXT")
            db.execute("ALTER TABLE bots ADD COLUMN IF NOT EXISTS background TEXT")
            db.execute("ALTER TABLE bots ADD COLUMN IF NOT EXISTS pinned_at DOUBLE PRECISION")
            db.execute("ALTER TABLE bots ADD COLUMN IF NOT EXISTS archived_at DOUBLE PRECISION")
            db.execute("ALTER TABLE bots ADD COLUMN IF NOT EXISTS model TEXT")
            db.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS model TEXT")
            for part, kind in STEER_COLUMNS:
                db.execute(f"ALTER TABLE queued_messages ADD COLUMN IF NOT EXISTS {part} {kind}")
            db.execute(STEER_INDEX)
            db.execute("""CREATE TABLE IF NOT EXISTS upload_parts (
                upload_id TEXT NOT NULL REFERENCES file_uploads(id) ON DELETE CASCADE,
                start_offset INTEGER NOT NULL, data BYTEA NOT NULL,
                PRIMARY KEY(upload_id,start_offset)
            )""")
            db.raw.execute("""
                CREATE OR REPLACE FUNCTION unicode_fold(text) RETURNS text
                LANGUAGE SQL IMMUTABLE STRICT AS $$
                    SELECT casefold(normalize($1,NFC) COLLATE "pg_unicode_fast")
                $$;
                CREATE OR REPLACE FUNCTION instr(text,text) RETURNS integer
                LANGUAGE SQL IMMUTABLE STRICT AS $$ SELECT strpos($1,$2) $$;
                CREATE OR REPLACE FUNCTION byte_length(text) RETURNS integer
                LANGUAGE SQL IMMUTABLE STRICT AS $$ SELECT octet_length($1) $$;
            """)
            db.execute(
                "INSERT INTO bots_schema VALUES(1,?) ON CONFLICT DO NOTHING", (SCHEMA_VERSION,)
            )
            # Additive capacity (20) and memory receipt (21) tables preserve
            # existing data and allocation intents atomically.
            if old and old[0] < SCHEMA_VERSION:
                db.execute("UPDATE bots_schema SET version=? WHERE id=1", (SCHEMA_VERSION,))
            db.execute("INSERT INTO object_storage_enabled VALUES(1) ON CONFLICT DO NOTHING")

    @contextmanager
    def connect(self, *, write=False):
        import psycopg
        from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

        # Never place the DSN/upstream exception in product errors or logs.
        try:
            raw = self._pool.getconn()
        except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests):
            raise SharedDatabaseError("Shared bot database is unavailable.") from None
        try:
            with raw.transaction():
                raw.execute("SET LOCAL statement_timeout='10s'")
                raw.execute("SET LOCAL lock_timeout='5s'")
                if write:
                    raw.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
                db = Connection(raw)
                if self._fence is not None:
                    self._fence.check(db)
                yield db
                if self._fence is not None and write:
                    self._fence.check(db)
            self.task_signals.notify(db.event_tasks)
        except psycopg.Error:
            raise SharedDatabaseError(
                "Shared bot database operation failed; no success was confirmed."
            ) from None
        finally:
            self._pool.putconn(raw)

    def allocate_upload(self, db, values):
        db.execute(
            "INSERT INTO file_uploads(id,bot_id,name,size,received,status,expires,request_id,file_id,data,previous_file_id) "
            "VALUES(?,?,?,?,?,'pending',?,?,NULL,NULL,?)",
            (*values[:-2], values[-1]),
        )

    def upload_blob(self, db, row, *, readonly=False):
        return UploadBlob(db, row["id"], readonly)

    def release_upload(self, db, upload_id):
        db.execute("DELETE FROM upload_parts WHERE upload_id=?", (upload_id,))


class UploadBlob:
    """Bounded upload ranges; never rewrite a whole 50 MiB value per chunk."""

    def __init__(self, db, upload_id, readonly):
        self.db, self.id, self.readonly = db, upload_id, readonly
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def seek(self, offset):
        self.offset = offset

    def read(self, size):
        if not 0 <= size <= FILE_CHUNK_BYTES:
            raise ValueError("Upload reads must be bounded.")
        rows = self.db.execute(
            "SELECT start_offset,data FROM upload_parts WHERE upload_id=? "
            "AND start_offset<? AND start_offset+length(data)>? ORDER BY start_offset",
            (self.id, self.offset + size, self.offset),
        ).fetchall()
        result = bytearray()
        for row in rows:
            start = self.offset + len(result) - row[0]
            if start < 0:
                raise RuntimeError("Stored upload has a missing range.")
            result.extend(row[1][start : start + size - len(result)])
        self.offset += len(result)
        return bytes(result)

    def write(self, data):
        if self.readonly or not 0 < len(data) <= FILE_CHUNK_BYTES:
            raise ValueError("Invalid upload chunk write.")
        self.db.execute("INSERT INTO upload_parts VALUES(?,?,?)", (self.id, self.offset, data))
        self.offset += len(data)
