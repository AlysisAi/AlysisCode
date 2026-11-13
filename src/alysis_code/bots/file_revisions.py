"""Immutable library revisions; existing file IDs remain exact attachment snapshots."""

import sqlite3

FILE_TABLE = """CREATE TABLE files (
    id TEXT PRIMARY KEY, bot_id TEXT NOT NULL REFERENCES bots(id),
    name TEXT NOT NULL, content TEXT NOT NULL, source_task_id TEXT REFERENCES tasks(id),
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    series_id TEXT NOT NULL, revision INTEGER NOT NULL,
    UNIQUE(bot_id,name,revision)
)"""


def migrate(connection: sqlite3.Connection) -> None:
    """Rebuild the legacy name-unique table atomically without rewriting file IDs.

    Called on the initialization connection before foreign keys are enabled.
    Disabling FK actions here preserves payloads and all source references while
    replacing the table; foreign_key_check validates the resulting database.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(files)")}
    if not columns or "series_id" in columns:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(FILE_TABLE.replace("CREATE TABLE files", "CREATE TABLE files_migration"))
        connection.execute(
            "INSERT INTO files_migration SELECT id,bot_id,name,content,source_task_id,"
            "created_at,updated_at,id,1 FROM files"
        )
        connection.execute("DROP TABLE files")
        connection.execute("ALTER TABLE files_migration RENAME TO files")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("File revision migration failed its reference check.")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
