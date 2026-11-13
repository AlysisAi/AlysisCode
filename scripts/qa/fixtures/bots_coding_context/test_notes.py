import io
import json
import os
import contextlib
import tempfile
import unittest

import notes
from storage import NotesStore, ValidationError, parse_import_lines


def rec(id_, client="acme", text="hello"):
    return json.dumps({"id": id_, "client": client, "text": text}, ensure_ascii=False)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def import_lines(self, lines):
        with NotesStore(self.db) as s:
            return s.import_records(parse_import_lines(lines))

    def test_parse_rejects_extra_and_missing_keys(self):
        with self.assertRaises(ValidationError):
            parse_import_lines([json.dumps({"id": "1", "client": "a"})])
        with self.assertRaises(ValidationError):
            parse_import_lines([json.dumps({"id": "1", "client": "a", "text": "t", "x": 1})])

    def test_parse_rejects_empty_and_non_string(self):
        with self.assertRaises(ValidationError):
            parse_import_lines([rec("1", text="")])
        with self.assertRaises(ValidationError):
            parse_import_lines([json.dumps({"id": 1, "client": "a", "text": "t"})])

    def test_parse_rejects_malformed_json_with_line_number(self):
        with self.assertRaises(ValidationError) as cm:
            parse_import_lines([rec("1"), "{not json"])
        self.assertIn("line 2", str(cm.exception))

    def test_blank_lines_skipped(self):
        recs = parse_import_lines(["", rec("1"), "   "])
        self.assertEqual(len(recs), 1)

    def test_import_and_list_sorted(self):
        self.import_lines([rec("b"), rec("a")])
        with NotesStore(self.db) as s:
            self.assertEqual([n["id"] for n in s.all_notes()], ["a", "b"])

    def test_identical_reimport_is_noop(self):
        self.import_lines([rec("1")])
        self.assertEqual(self.import_lines([rec("1")]), 0)
        with NotesStore(self.db) as s:
            self.assertEqual(len(s.all_notes()), 1)

    def test_identical_duplicates_within_batch(self):
        self.assertEqual(self.import_lines([rec("1"), rec("1")]), 1)

    def test_conflict_with_db_is_atomic(self):
        self.import_lines([rec("1", text="orig")])
        with self.assertRaises(ValidationError):
            self.import_lines([rec("2"), rec("1", text="changed")])
        with NotesStore(self.db) as s:
            ids = [n["id"] for n in s.all_notes()]
        self.assertEqual(ids, ["1"])

    def test_conflict_within_batch_is_atomic(self):
        with self.assertRaises(ValidationError):
            self.import_lines([rec("1", text="a"), rec("2"), rec("1", text="b")])
        with NotesStore(self.db) as s:
            self.assertEqual(s.all_notes(), [])

    def test_unicode_preserved_exactly(self):
        text = "Café ☕ 日本語 🚀 e\u0301"
        self.import_lines([rec("u1", client="Zoë", text=text)])
        with NotesStore(self.db) as s:
            note = s.all_notes()[0]
        self.assertEqual(note["text"], text)
        self.assertEqual(note["client"], "Zoë")
        self.assertIn("☕", notes_dump(note))

    def test_whitespace_only_fields_rejected(self):
        for bad in ["   ", "\t", "\n", " \u3000 ", "\u00a0"]:
            with self.assertRaises(ValidationError, msg=repr(bad)):
                parse_import_lines([rec("1", text=bad)])
            with self.assertRaises(ValidationError, msg=repr(bad)):
                parse_import_lines([rec("1", client=bad)])
            with self.assertRaises(ValidationError, msg=repr(bad)):
                parse_import_lines([rec(bad)])

    def test_whitespace_preserved_in_nonblank_values(self):
        text = "  leading and trailing\t\n"
        recs = parse_import_lines([rec("1", text=text, client=" Acme ")])
        self.import_lines([rec("1", text=text, client=" Acme ")])
        with NotesStore(self.db) as s:
            note = s.all_notes()[0]
        self.assertEqual(note["text"], text)
        self.assertEqual(note["client"], " Acme ")
        self.assertEqual(recs[0][1]["text"], text)

    def test_client_filter_nfc_equivalence(self):
        composed = "Caf\u00e9"
        decomposed = "Cafe\u0301"
        self.import_lines([rec("1", client=composed), rec("2", client=decomposed)])
        with NotesStore(self.db) as s:
            self.assertEqual([n["id"] for n in s.all_notes(client_query=composed)], ["1", "2"])
            self.assertEqual([n["id"] for n in s.all_notes(client_query=decomposed)], ["1", "2"])

    def test_client_filter_casefold_and_no_accent_stripping(self):
        self.import_lines([rec("1", client="Straße"), rec("2", client="Zoë"), rec("3", client="Zoe")])
        with NotesStore(self.db) as s:
            # casefold: "ß" folds to "ss", so STRASSE matches Straße
            self.assertEqual([n["id"] for n in s.all_notes(client_query="STRASSE")], ["1"])
            self.assertEqual([n["id"] for n in s.all_notes(client_query="STRAße")], ["1"])
            # accents are not removed: "zoe" does not match "Zoë"
            self.assertEqual([n["id"] for n in s.all_notes(client_query="zoe")], ["3"])
            self.assertEqual([n["id"] for n in s.all_notes(client_query="ZOË")], ["2"])

    def test_client_filter_empty_query_matches_all(self):
        self.import_lines([rec("2"), rec("1", client="other")])
        with NotesStore(self.db) as s:
            self.assertEqual([n["id"] for n in s.all_notes(client_query="")], ["1", "2"])

    def test_client_filter_sql_metacharacters_literal(self):
        self.import_lines([
            rec("1", client="100%_done"),
            rec("2", client="100 percent"),
            rec("3", client="a'b\"c; DROP TABLE notes;--"),
            rec("4", client="x_y"),
        ])
        with NotesStore(self.db) as s:
            self.assertEqual([n["id"] for n in s.all_notes(client_query="%")], ["1"])
            self.assertEqual([n["id"] for n in s.all_notes(client_query="_")], ["1", "4"])
            self.assertEqual([n["id"] for n in s.all_notes(client_query="'")], ["3"])
            self.assertEqual([n["id"] for n in s.all_notes(client_query="; drop")], ["3"])
            self.assertEqual([n["id"] for n in s.all_notes(client_query="%%")], [])
            # table still intact
            self.assertEqual(len(s.all_notes()), 4)

    def test_client_filter_results_sorted_by_id(self):
        self.import_lines([rec("c", client="acme"), rec("a", client="acme"), rec("b", client="other")])
        with NotesStore(self.db) as s:
            self.assertEqual([n["id"] for n in s.all_notes(client_query="ACME")], ["a", "c"])

    def test_client_filter_does_not_mutate_stored_text(self):
        self.import_lines([rec("1", client="Zoe\u0308", text="Ab\u0301")])
        with NotesStore(self.db) as s:
            s.all_notes(client_query="ZOË")
            note = s.all_notes()[0]
        self.assertEqual(note["client"], "Zoe\u0308")
        self.assertEqual(note["text"], "Ab\u0301")


def notes_dump(note):
    from storage import dumps_note
    return dumps_note(note)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "cli.db")
        self.inp = os.path.join(self.tmp.name, "in.jsonl")
        self.out = os.path.join(self.tmp.name, "out.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, lines):
        with open(self.inp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = notes.main(["--db", self.db, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_import_list_export_roundtrip(self):
        self.write([rec("2", text="β"), rec("1", text="α")])
        self.assertEqual(self.run_cli("import", self.inp)[0], 0)
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertEqual([n["id"] for n in json.loads(out)], ["1", "2"])
        self.assertEqual(self.run_cli("export", self.out)[0], 0)
        with open(self.out, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], ["1", "2"])
        self.assertIn("β", lines[1])

    def test_missing_input_file(self):
        code, _, err = self.run_cli("import", os.path.join(self.tmp.name, "nope.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_malformed_json_nonzero_no_traceback(self):
        self.write([rec("1"), "{bad"])
        code, _, err = self.run_cli("import", self.inp)
        self.assertEqual(code, 1)
        self.assertIn("malformed JSON", err)
        self.assertNotIn("Traceback", err)
        code, out, _ = self.run_cli("list")
        self.assertEqual(json.loads(out), [])

    def test_conflict_exits_nonzero_and_changes_nothing(self):
        self.write([rec("1", text="a")])
        self.run_cli("import", self.inp)
        self.write([rec("9"), rec("1", text="b")])
        code, _, err = self.run_cli("import", self.inp)
        self.assertEqual(code, 1)
        _, out, _ = self.run_cli("list")
        self.assertEqual([n["id"] for n in json.loads(out)], ["1"])

    def test_bad_output_directory(self):
        self.write([rec("1")])
        self.run_cli("import", self.inp)
        code, _, err = self.run_cli("export", os.path.join(self.tmp.name, "no", "x.jsonl"))
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)

    def test_list_and_export_client_filter(self):
        self.write([rec("1", client="Acme Corp"), rec("2", client="Globex"), rec("3", client="acme labs")])
        self.run_cli("import", self.inp)
        code, out, _ = self.run_cli("list", "--client", "ACME")
        self.assertEqual(code, 0)
        self.assertEqual([n["id"] for n in json.loads(out)], ["1", "3"])
        code, _, _ = self.run_cli("export", self.out, "--client", "globex")
        self.assertEqual(code, 0)
        with open(self.out, encoding="utf-8") as fh:
            self.assertEqual([json.loads(l)["id"] for l in fh.read().splitlines()], ["2"])

    def test_list_without_filter_unchanged(self):
        self.write([rec("2", client="x"), rec("1", client="y")])
        self.run_cli("import", self.inp)
        _, out, _ = self.run_cli("list")
        self.assertEqual([n["id"] for n in json.loads(out)], ["1", "2"])

    def test_list_empty_client_query_matches_all(self):
        self.write([rec("1"), rec("2", client="zz")])
        self.run_cli("import", self.inp)
        _, out, _ = self.run_cli("list", "--client", "")
        self.assertEqual(len(json.loads(out)), 2)

    def test_import_rejects_whitespace_only_via_cli(self):
        self.write([rec("1", text="   ")])
        code, _, err = self.run_cli("import", self.inp)
        self.assertEqual(code, 1)
        self.assertIn("whitespace-only", err)
        _, out, _ = self.run_cli("list")
        self.assertEqual(json.loads(out), [])


if __name__ == "__main__":
    unittest.main()
