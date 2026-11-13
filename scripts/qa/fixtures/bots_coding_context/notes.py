#!/usr/bin/env python3
"""Aster Notes CLI.

Usage:
  python notes.py --db PATH import INPUT.jsonl
  python notes.py --db PATH list [--client QUERY]
  python notes.py --db PATH export OUTPUT.jsonl [--client QUERY]
"""

import argparse
import json
import os
import sqlite3
import sys

from storage import NotesStore, ValidationError, dumps_note, parse_import_lines


def cmd_import(store, args):
    try:
        with open(args.input, "r", encoding="utf-8", newline="") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read input: {exc}", file=sys.stderr)
        return 1
    records = parse_import_lines(lines)
    inserted = store.import_records(records)
    print(f"imported {inserted} new note(s); {len(records) - inserted} "
          f"duplicate line(s) ignored")
    return 0


def cmd_list(store, args):
    notes_list = store.all_notes(client_query=args.client)
    print(json.dumps(notes_list, ensure_ascii=False, indent=2))
    return 0


def cmd_export(store, args):
    notes = store.all_notes(client_query=args.client)
    out_dir = os.path.dirname(os.path.abspath(args.output))
    if not os.path.isdir(out_dir):
        print(f"error: output directory does not exist: {out_dir}", file=sys.stderr)
        return 1
    with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
        for note in notes:
            fh.write(dumps_note(note) + "\n")
    print(f"exported {len(notes)} note(s) to {args.output}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="notes.py", description="Aster Notes CLI")
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    sub = parser.add_subparsers(dest="command", required=True)
    p_imp = sub.add_parser("import", help="import notes from a JSONL file")
    p_imp.add_argument("input")
    p_list = sub.add_parser("list", help="print notes as a JSON array")
    p_list.add_argument("--client", default=None, metavar="QUERY",
                        help="only notes whose client contains QUERY "
                             "(NFC, case-insensitive)")
    p_exp = sub.add_parser("export", help="export notes to a JSONL file")
    p_exp.add_argument("output")
    p_exp.add_argument("--client", default=None, metavar="QUERY",
                       help="only notes whose client contains QUERY "
                            "(NFC, case-insensitive)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    handlers = {"import": cmd_import, "list": cmd_list, "export": cmd_export}
    try:
        with NotesStore(args.db) as store:
            return handlers[args.command](store, args)
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"error: database error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
