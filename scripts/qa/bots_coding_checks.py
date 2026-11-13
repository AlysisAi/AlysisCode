"""Black-box acceptance checks, mounted outside the model's workspace."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

STAGE = int(sys.argv.pop(1)) if len(sys.argv) > 1 else 1
CLI = Path("/workspace/notes.py")


class NotesChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "notes.sqlite3"

    def run_cli(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(CLI), "--db", str(self.db), *args],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
        return result

    def put(self, rows, success=True):
        source = self.root / "input.jsonl"
        content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
        source.write_text(content)
        result = self.run_cli("import", str(source), success=success)
        self.assertEqual(source.read_text(), content)
        return result

    def rows(self, *args):
        return json.loads(self.run_cli("list", *args).stdout)

    def test_unicode_roundtrip_and_order(self):
        values = [
            {"id": "Ω-2", "client": "Ελένη", "text": "  πρώτη\n日本語 👩🏽‍💻\t  "},
            {"id": "a-1", "client": "O'Reilly", "text": 'quote " and \\ and <script>'},
        ]
        self.put(values)
        expected = sorted(values, key=lambda row: row["id"])
        self.assertEqual(self.rows(), expected)
        target = self.root / "export.jsonl"
        self.run_cli("export", str(target))
        self.assertEqual([json.loads(line) for line in target.read_text().splitlines()], expected)

    def test_identical_duplicate_is_noop(self):
        row = {"id": "same", "client": "Cliente", "text": "hello"}
        self.put([row, row])
        self.put([row])
        self.assertEqual(self.rows(), [row])

    def test_conflict_rolls_back_whole_batch(self):
        old = {"id": "old", "client": "A", "text": "original"}
        self.put([old])
        self.put(
            [{"id": "new", "client": "B", "text": "new"}, {**old, "text": "changed"}], success=False
        )
        self.assertEqual(self.rows(), [old])

    def test_internal_conflict_rolls_back(self):
        old = {"id": "a", "client": "A", "text": "original"}
        self.put([old, {**old, "client": "B"}], success=False)
        self.assertEqual(self.rows(), [])

    def test_bad_records_are_atomic(self):
        good = {"id": "keep", "client": "A", "text": "untouched"}
        self.put([good])
        invalids = [
            None,
            [],
            {"id": "x", "client": "a"},
            {"id": "x", "client": "a", "text": "", "extra": "x"},
            {"id": "x", "client": 123, "text": "x"},
            {
                "id": "x",
                "client": "a",
                "text": "",
            },
            {"id": "x", "client": "a", "text": "fine", "extra": 1},
        ]
        for invalid in invalids:
            with self.subTest(invalid=invalid):
                self.put([{"id": "new", "client": "N", "text": "new"}, invalid], success=False)
                self.assertEqual(self.rows(), [good])

    def test_malformed_and_missing_inputs(self):
        source = self.root / "malformed.jsonl"
        source.write_text('{"id":"x","client":"a","text":"b"}\n{bad\n')
        self.run_cli("import", str(source), success=False)
        self.assertEqual(self.rows(), [])
        self.run_cli("import", str(self.root / "missing.jsonl"), success=False)

    def test_blank_lines(self):
        source = self.root / "empty.jsonl"
        source.write_text("\n  \n\t\n")
        self.run_cli("import", str(source))
        self.assertEqual(self.rows(), [])

    @unittest.skipIf(STAGE < 2, "Follow-up requirement")
    def test_unicode_normalized_filter(self):
        values = [
            {"id": "2", "client": "Straße CAFÉ", "text": "two"},
            {"id": "1", "client": "STRASSE Cafe\u0301", "text": "one"},
            {"id": "3", "client": "Cafe", "text": "three"},
        ]
        self.put(values)
        expected = sorted(values[:2], key=lambda row: row["id"])
        self.assertEqual(self.rows("--client", "strasse"), expected)
        self.assertEqual(self.rows("--client", "cafe\u0301"), expected)
        self.assertEqual(self.rows("--client", "CAFÉ"), expected)
        self.assertEqual(self.rows("--client", ""), sorted(values, key=lambda row: row["id"]))
        target = self.root / "filtered.jsonl"
        self.run_cli("export", str(target), "--client", "CAFÉ")
        self.assertEqual([json.loads(line) for line in target.read_text().splitlines()], expected)

    @unittest.skipIf(STAGE < 2, "Follow-up requirement")
    def test_filter_metacharacters_are_literal(self):
        values = [
            {"id": "a", "client": "100%_sure ' OR 1=1 --", "text": "one"},
            {"id": "b", "client": "100 percent", "text": "two"},
        ]
        self.put(values)
        self.assertEqual(self.rows("--client", "%_"), values[:1])
        self.assertEqual(self.rows("--client", "' OR 1=1 --"), values[:1])

    @unittest.skipIf(STAGE < 2, "Follow-up requirement")
    def test_blank_values_rejected_without_trimming_data(self):
        for field in ("id", "client", "text"):
            with self.subTest(field=field):
                bad = {"id": "a", "client": "A", "text": "text", field: " \t\n\u2003"}
                self.put([bad], success=False)
                self.assertEqual(self.rows(), [])
        value = {"id": " padded ", "client": " Name ", "text": " text\n"}
        self.put([value])
        self.assertEqual(self.rows(), [value])


if __name__ == "__main__":
    unittest.main(verbosity=2)
