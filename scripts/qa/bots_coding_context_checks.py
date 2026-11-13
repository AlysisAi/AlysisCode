"""Held-out behavior checks for the existing-repository coding study (Linux only)."""

import json
import resource
import signal
import subprocess
import sys
import unittest

STAGE = int(sys.argv.pop(1))
# Retain all ten earlier contract checks; their original follow-up is now baseline.
sys.argv.append("2")
import bots_coding_checks as previous  # noqa: E402


class ContextChecks(previous.NotesChecks):
    def test_jsonl_unicode_separators_are_data(self):
        rows = [
            {"id": "α", "client": "日本\u2028語", "text": "before\u0085middle\u2029after"},
            {"id": "b", "client": "Équipe", "text": "  Δοκιμή 🌱\n\r\t  "},
        ]
        self.put(rows)
        expected = sorted(rows, key=lambda row: row["id"])
        self.assertEqual(self.rows(), expected)
        output = self.root / "export.jsonl"
        self.run_cli("export", str(output))
        # JSONL separates records on LF, not every Unicode line separator.
        actual = [json.loads(line) for line in output.read_text().split("\n") if line.strip()]
        self.assertEqual(actual, expected)

    def test_existing_export_is_not_overwritten_by_default(self):
        self.put([{"id": "a", "client": "A", "text": "new content"}])
        output = self.root / "existing.jsonl"
        sentinel = b"Existing customer export\n\x00"
        output.write_bytes(sentinel)
        self.run_cli("export", str(output), success=False)
        self.assertEqual(output.read_bytes(), sentinel)

    @unittest.skipIf(STAGE < 2, "Follow-up policy")
    def test_force_replaces_existing_and_preserves_filter(self):
        rows = [
            {"id": "a", "client": "Cafe\u0301", "text": "exact\u2028value 🌱"},
            {"id": "b", "client": "Other", "text": "omit"},
        ]
        self.put(rows)
        output = self.root / "existing.jsonl"
        output.write_bytes(b"old version")
        self.run_cli("export", str(output), "--force", "--client", "CAFÉ")
        self.assertEqual(
            [json.loads(line) for line in output.read_text().split("\n") if line], rows[:1]
        )

    @unittest.skipIf(STAGE < 2, "Follow-up policy")
    def test_failed_force_preserves_destination_and_cleans_temporary_file(self):
        self.put([{"id": "a", "client": "A", "text": "日本語" * 5000}])
        directory = self.root / "exports"
        directory.mkdir()
        output = directory / "existing.jsonl"
        sentinel = b"previous export must survive\n"
        output.write_bytes(sentinel)

        def limit_file_size():
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (512, 512))

        result = subprocess.run(
            [
                sys.executable,
                str(previous.CLI),
                "--db",
                str(self.db),
                "export",
                str(output),
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            preexec_fn=limit_file_size,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(output.read_bytes(), sentinel)
        self.assertEqual(list(directory.iterdir()), [output])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ContextChecks)
    )
    print(
        json.dumps(
            {
                "tests_run": result.testsRun,
                "skipped": len(result.skipped),
                "failures": [test.id().rsplit(".", 1)[-1] for test, _ in result.failures],
                "errors": [test.id().rsplit(".", 1)[-1] for test, _ in result.errors],
            }
        )
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
