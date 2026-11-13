# Aster Notes CLI

A small, dependency-free (Python 3 standard library only) command-line tool
for storing notes in a local SQLite database.

## Usage

    python notes.py --db PATH import INPUT.jsonl
    python notes.py --db PATH list
    python notes.py --db PATH export OUTPUT.jsonl

Input is JSONL: each non-blank line is an object with exactly the keys
`id`, `client`, and `text`, all non-empty strings. Unicode is preserved exactly.

- **import** is atomic. If any line is invalid or any ID conflicts with stored
  or in-batch content, the command exits nonzero and no rows change.
  Re-importing identical rows (including repeats within one file) is a no-op.
- **list** prints a JSON array sorted by `id`.
- **export** writes JSONL sorted by `id`.
- Missing files, malformed JSON, and database errors produce an `error:`
  message on stderr and a nonzero exit code, never a traceback.

## Files

- `notes.py` – CLI entry point and argument handling.
- `storage.py` – validation, parsing, and SQLite storage logic.
- `test_notes.py` – unittest suite.

## Tests

    python -m unittest -v test_notes
