"""SQLite-backed storage for Aster Notes.

All mutating operations are atomic: either every row in a batch is applied,
or the database is left unchanged.
"""

import json
import sqlite3
import unicodedata

REQUIRED_FIELDS = ("id", "client", "text")


class ValidationError(Exception):
    """Raised when input data is malformed or conflicts with existing data."""


def parse_import_lines(lines):
    """Parse and validate JSONL lines. Returns a list of (lineno, record dicts).

    Blank lines are skipped. Raises ValidationError on the first problem found,
    reporting the 1-based line number.
    """
    records = []
    for lineno, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"line {lineno}: malformed JSON ({exc.msg})")
        if not isinstance(obj, dict):
            raise ValidationError(f"line {lineno}: expected a JSON object")
        if set(obj.keys()) != set(REQUIRED_FIELDS):
            raise ValidationError(
                f"line {lineno}: object must have exactly the keys "
                f"{', '.join(REQUIRED_FIELDS)}"
            )
        for field in REQUIRED_FIELDS:
            value = obj[field]
            if not isinstance(value, str) or value == "":
                raise ValidationError(
                    f"line {lineno}: field '{field}' must be a non-empty string"
                )
            if value.strip() == "":
                raise ValidationError(
                    f"line {lineno}: field '{field}' must not be whitespace-only"
                )
        records.append((lineno, {f: obj[f] for f in REQUIRED_FIELDS}))
    return records


class NotesStore:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS notes ("
            " id TEXT PRIMARY KEY,"
            " client TEXT NOT NULL,"
            " text TEXT NOT NULL)"
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def import_records(self, records):
        """Atomically import validated records.

        `records` is a list of (lineno, dict) from parse_import_lines.
        Identical duplicates (within the batch or against stored rows) are
        no-ops. Any conflicting duplicate aborts the whole import.
        Returns the number of newly inserted rows.
        """
        batch = {}
        for lineno, rec in records:
            existing = batch.get(rec["id"])
            if existing is None:
                batch[rec["id"]] = (lineno, rec)
            elif existing[1] != rec:
                raise ValidationError(
                    f"line {lineno}: id '{rec['id']}' conflicts with "
                    f"line {existing[0]} in the same batch"
                )

        conn = self.conn
        inserted = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            to_insert = []
            for rec_id, (lineno, rec) in batch.items():
                row = conn.execute(
                    "SELECT client, text FROM notes WHERE id = ?", (rec_id,)
                ).fetchone()
                if row is None:
                    to_insert.append(rec)
                elif row != (rec["client"], rec["text"]):
                    raise ValidationError(
                        f"line {lineno}: id '{rec_id}' already exists with "
                        f"different content"
                    )
            for rec in to_insert:
                conn.execute(
                    "INSERT INTO notes (id, client, text) VALUES (?, ?, ?)",
                    (rec["id"], rec["client"], rec["text"]),
                )
                inserted += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return inserted

    def all_notes(self, client_query=None):
        """Return notes as dicts sorted by id.

        If `client_query` is given, only notes whose client matches are
        returned. Matching is done in Python: both sides are NFC-normalized
        and casefolded, then a substring test is applied. No SQL pattern
        matching is used, so metacharacters such as % and _ are literal.
        An empty query matches everything. Stored values are not modified.
        """
        rows = self.conn.execute(
            "SELECT id, client, text FROM notes ORDER BY id"
        ).fetchall()
        notes = [{"id": i, "client": c, "text": t} for i, c, t in rows]
        if client_query is None:
            return notes
        needle = normalize_for_match(client_query)
        if needle == "":
            return notes
        return [n for n in notes if needle in normalize_for_match(n["client"])]


def normalize_for_match(value):
    """NFC-normalize then casefold a string for case-insensitive matching."""
    return unicodedata.normalize("NFC", value).casefold()


def dumps_note(note):
    """Serialize a note with exact Unicode preserved and fixed key order."""
    return json.dumps(
        {"id": note["id"], "client": note["client"], "text": note["text"]},
        ensure_ascii=False,
    )
