"""Allow multiple committed pause points and one final candidate per task."""


def migrate_checkpoints(db):
    # SQLite cannot drop an inline UNIQUE constraint. Rebuild only these two
    # leaf tables, atomically, retaining every ID, reference and timestamp.
    db.execute("SAVEPOINT checkpoint_schema")
    try:
        for table in ("workspace_objects", "browser_states"):
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                continue
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if "settled" not in columns:
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1))"
                )
            unique_task = any(
                row[2]
                and [part[2] for part in db.execute(f'PRAGMA index_info("{row[1]}")')]
                == ["task_id"]
                for row in db.execute(f"PRAGMA index_list({table})")
            )
            if not unique_task:
                continue
            nullability = "NOT NULL" if table == "browser_states" else ""
            db.execute(
                f"CREATE TABLE {table}_v35 (id TEXT PRIMARY KEY,task_id TEXT {nullability} REFERENCES tasks(id),bot_id TEXT NOT NULL REFERENCES bots(id),ref TEXT NOT NULL,created_at REAL NOT NULL,settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1)))"
            )
            db.execute(
                f"INSERT INTO {table}_v35 SELECT id,task_id,bot_id,ref,created_at,settled FROM {table}"
            )
            old = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if db.execute(f"SELECT count(*) FROM {table}_v35").fetchone()[0] != old:
                raise RuntimeError("Checkpoint migration could not preserve all records.")
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {table}_v35 RENAME TO {table}")
        db.execute("RELEASE SAVEPOINT checkpoint_schema")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT checkpoint_schema")
        db.execute("RELEASE SAVEPOINT checkpoint_schema")
        raise
