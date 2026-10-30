"""Tests for write-path secret redaction.

Runnable two ways:

    python3 tests/test_logging_redaction.py     # standalone, stdlib only
    pytest tests/test_logging_redaction.py

The module under test is loaded directly from its file path so that importing it
never executes ``alysis_code/__init__`` or any of the package's
dependency-heavy import chain. That keeps these tests runnable in a bare
interpreter with no third-party packages installed.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import time
import unittest
import urllib.parse
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "alysis_code" / "logging_redaction.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("_logging_redaction", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lr = _load_module()

# A realistic-looking but entirely synthetic credential.
SECRET = "sk-syl-A7bQ9zX2mK4pL8vN3cD5"
GITHUB_TOKEN = "ghp_1a2B3c4D5e6F7g8H9i0JkLmNoPqRsTuVwXyZ"

ENVIRON = {
    "ALYSIS_API_KEY": SECRET,
    "GITHUB_TOKEN": GITHUB_TOKEN,
    "ALYSIS_SESSION_SALT": "9fQ2xR7mZ4kL1pV8nT3bY6wC",
    "DB_PASSWORD": "short12",  # 7 chars: below MIN_VALUE_LENGTH
    "ALYSIS_MODEL": "claude-opus-4-20250514",  # config, not a credential
    "ALYSIS_WORKSPACE": "/home/agent/workspace",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/root",
}

REDACTED_API_KEY = "«redacted:ALYSIS_API_KEY»"
REDACTED_PATTERN = "«redacted:pattern»"


class RedactorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = lr.SecretRedactor(ENVIRON)

    def assertRedacted(self, text: str) -> str:
        """Redact, assert no captured secret survives, and return the result."""
        result = self.redactor.redact(text)
        self.assertNotIn(SECRET, result)
        return result


class TestCapture(RedactorTestCase):
    def test_captures_named_and_credential_like_variables(self) -> None:
        # SecretRedactor iterates sorted by name, so this order is the
        # documented contract rather than an accident. The ALYSIS_ variables
        # precede GITHUB_ variables alphabetically. Redaction itself is unaffected:
        # _compile orders variants by length, independently of this tuple.
        self.assertEqual(
            self.redactor.secret_names,
            ("ALYSIS_API_KEY", "ALYSIS_SESSION_SALT", "GITHUB_TOKEN"),
        )
        assert list(self.redactor.secret_names) == sorted(self.redactor.secret_names)

    def test_short_values_are_ignored(self) -> None:
        # DB_PASSWORD matches the name rule but its value is 7 chars, which is
        # far too collision-prone to blanket-replace in log prose.
        self.assertNotIn("DB_PASSWORD", self.redactor.secret_names)
        self.assertEqual(self.redactor.redact("DB_PASSWORD=short12"), "DB_PASSWORD=short12")

    def test_non_credential_alysis_config_is_not_captured(self) -> None:
        # A model id and a path are both >= 16 chars but are not credentials;
        # redacting them would degrade every log for no security benefit.
        self.assertNotIn("ALYSIS_MODEL", self.redactor.secret_names)
        self.assertNotIn("ALYSIS_WORKSPACE", self.redactor.secret_names)
        line = "ALYSIS_MODEL=claude-opus-4-20250514 cwd=/home/agent/workspace"
        self.assertEqual(self.redactor.redact(line), line)

    def test_empty_environment_still_redacts_risky_patterns(self) -> None:
        bare = lr.SecretRedactor({})
        self.assertEqual(bare.secret_names, ())
        self.assertEqual(
            bare.redact("Authorization: Bearer q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6"),
            f"Authorization: Bearer {REDACTED_PATTERN}",
        )


class TestEnvDumpScenario(RedactorTestCase):
    """The actual incident: a tool dumped the environment and it was logged."""

    def test_env_output_is_redacted_line_by_line(self) -> None:
        env_output = "\n".join(f"{name}={value}" for name, value in sorted(ENVIRON.items()))
        result = self.assertRedacted(env_output)
        self.assertIn(f"ALYSIS_API_KEY={REDACTED_API_KEY}", result)
        self.assertNotIn(GITHUB_TOKEN, result)
        # Non-secret environment entries survive untouched.
        self.assertIn("HOME=/root", result)
        self.assertIn("PATH=/usr/local/bin:/usr/bin:/bin", result)

    def test_env_dump_wrapped_in_a_session_jsonl_event(self) -> None:
        # Exactly the shape SessionStore.append writes: ensure_ascii JSON + "\n".
        line = json.dumps(
            {
                "type": "tool_result",
                "payload": {"stdout": f"HOME=/root\nALYSIS_API_KEY={SECRET}\n"},
            },
            ensure_ascii=True,
        )
        result = self.assertRedacted(line)
        # Still parseable, and the secret is gone from the decoded value too.
        decoded = json.loads(result)
        self.assertNotIn(SECRET, decoded["payload"]["stdout"])
        self.assertIn(REDACTED_API_KEY, decoded["payload"]["stdout"])


class TestConfigPrintScenario(RedactorTestCase):
    def test_config_print_with_quoted_and_bare_values(self) -> None:
        text = (
            "resolved configuration:\n"
            f'  api_key = "{SECRET}"\n'
            f"  fallback_key={GITHUB_TOKEN}\n"
            "  model = claude-opus-4-20250514\n"
        )
        result = self.assertRedacted(text)
        self.assertNotIn(GITHUB_TOKEN, result)
        self.assertIn("model = claude-opus-4-20250514", result)

    def test_json_config_dump_stays_valid_json(self) -> None:
        payload = {"api_key": SECRET, "model": "claude-opus-4-20250514"}
        result = self.assertRedacted(json.dumps(payload))
        decoded = json.loads(result)
        self.assertEqual(decoded["api_key"], REDACTED_API_KEY)
        self.assertEqual(decoded["model"], "claude-opus-4-20250514")

    def test_unknown_secret_still_caught_by_pattern_pass(self) -> None:
        # A credential that is NOT in the environment snapshot: the exact-value
        # pass cannot know it, so the risky-pattern pass is the only net.
        unknown = "Zq7Wm2Rt9Yx4Vb6Nc1Pd8Kf3"
        result = self.redactor.redact(json.dumps({"api_key": unknown}))
        self.assertNotIn(unknown, result)
        self.assertEqual(json.loads(result)["api_key"], REDACTED_PATTERN)


class TestEncodedForms(RedactorTestCase):
    def test_secret_mid_paragraph(self) -> None:
        text = (
            "The agent read the environment, then used "
            f"{SECRET} to authenticate against the provider and retried twice."
        )
        result = self.assertRedacted(text)
        self.assertIn(f"used {REDACTED_API_KEY} to authenticate", result)

    def test_json_escaped_occurrence(self) -> None:
        # Embedded in a string with escapes around it, ensure_ascii style.
        line = json.dumps({"cmd": f'export KEY="{SECRET}"\n'}, ensure_ascii=True)
        self.assertRedacted(line)

    def test_url_encoded_occurrence(self) -> None:
        quoted = urllib.parse.quote(SECRET, safe="")
        result = self.assertRedacted(f"GET /v1/models?key={quoted} HTTP/1.1")
        self.assertNotIn(quoted, result)

    def test_query_plus_encoded_occurrence(self) -> None:
        plussed = urllib.parse.quote_plus(SECRET, safe="")
        result = self.assertRedacted(f"body=token{plussed}")
        self.assertNotIn(plussed, result)

    def test_base64_of_exact_value(self) -> None:
        for encoded in (
            base64.b64encode(SECRET.encode()).decode(),
            base64.urlsafe_b64encode(SECRET.encode()).decode(),
        ):
            with self.subTest(encoded=encoded):
                result = self.redactor.redact(f"payload: {encoded}")
                self.assertNotIn(encoded.rstrip("="), result)

    def test_base64_at_shifted_alignment_is_a_documented_gap(self) -> None:
        # Encoding "xy" + SECRET shifts the secret's bytes off a 3-byte boundary,
        # so its standalone base64 spelling does not appear. This is the
        # documented limitation; the archive-time scanner remains the backstop.
        shifted = base64.b64encode(("xy" + SECRET).encode()).decode()
        result = self.redactor.redact(f"payload: {shifted}")
        self.assertEqual(result, f"payload: {shifted}")


class TestRiskyPatterns(RedactorTestCase):
    def test_case_sensitive_entropy_and_original_syntax_are_preserved(self) -> None:
        # Lowercasing the credential itself would reduce entropy below the
        # threshold and leak it. Matching may normalize case; masking may not.
        payload = "AaBbCcDd" * 4
        text = f'Prefix "AcCeSs_ToKeN": "{payload}" SUFFIX\n'
        self.assertEqual(
            self.redactor.redact(text),
            f'Prefix "AcCeSs_ToKeN": "{REDACTED_PATTERN}" SUFFIX\n',
        )
        low_entropy = "aabbccdd" * 4
        text = f"ToKeN={low_entropy}"
        self.assertEqual(self.redactor.redact(text), text)

    def test_unicode_boundaries_and_case_equivalents_are_preserved(self) -> None:
        payload = "AaBbCcDd" * 4
        cases = (
            (f"πtoken={payload}", f"πtoken={payload}"),
            (f"paſſword={payload}", f"paſſword={REDACTED_PATTERN}"),
            (f"clİent_secret={payload}", f"clİent_secret={REDACTED_PATTERN}"),
            (f"api_Key={payload}", f"api_Key={REDACTED_PATTERN}"),
            (f"TOKEN={payload}ı", f"TOKEN={REDACTED_PATTERN}"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.redactor.redact(text), expected)

    def test_known_values_and_unicode_placeholder_names_keep_offsets(self) -> None:
        payload = "AaBbCcDd" * 4
        for name in ("EXAMPLE_TOKEN", "EXAMPLE_İ_TOKEN"):
            with self.subTest(name=name):
                redactor = lr.SecretRedactor({name: SECRET})
                text = f"value={SECRET}; Authorization: Bearer {payload}; value={SECRET}"
                self.assertEqual(
                    redactor.redact(text),
                    f"value=«redacted:{name}»; Authorization: Bearer {REDACTED_PATTERN}; "
                    f"value=«redacted:{name}»",
                )

    def test_embedded_key_and_adjacent_matches_keep_their_boundaries(self) -> None:
        payload = "AaBbCcDd" * 4
        text = f"myapi-token={payload}; secret={payload}; token=aaaaaaaaaaaaaaaa"
        self.assertEqual(
            self.redactor.redact(text),
            f"myapi-token={REDACTED_PATTERN}; secret={REDACTED_PATTERN}; token=aaaaaaaaaaaaaaaa",
        )

    def test_authorization_bearer_header(self) -> None:
        result = self.redactor.redact("Authorization: Bearer q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6")
        self.assertEqual(result, f"Authorization: Bearer {REDACTED_PATTERN}")

    def test_bare_bearer_token(self) -> None:
        result = self.redactor.redact("bearer q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6")
        self.assertEqual(result, f"bearer {REDACTED_PATTERN}")

    def test_assignment_forms(self) -> None:
        payload = "q1W2e3R4t5Y6u7I8o9P0a1S2"
        for template in (
            "api_key={payload}",
            "api-key = {payload}",
            'apiKey: "{payload}"',
            '"access_token": "{payload}"',
            "client_secret='{payload}'",
        ):
            with self.subTest(template=template):
                result = self.redactor.redact(template.format(payload=payload))
                self.assertNotIn(payload, result)
                self.assertIn(REDACTED_PATTERN, result)

    def test_low_entropy_payload_is_left_alone(self) -> None:
        # A placeholder, not a credential. Redacting it would only add noise.
        line = "password=aaaaaaaaaaaaaaaaaaaa"
        self.assertEqual(self.redactor.redact(line), line)

    def test_short_payload_is_left_alone(self) -> None:
        line = "token=abc123"
        self.assertEqual(self.redactor.redact(line), line)

    def test_ordinary_prose_is_untouched(self) -> None:
        line = (
            "2026-08-21T10:00:00Z level=info msg=applied patch to "
            "src/alysis_code/agent/session.py (3 hunks, 0 conflicts)"
        )
        self.assertEqual(self.redactor.redact(line), line)


class TestSplitAcrossFields(RedactorTestCase):
    def test_secret_split_across_two_json_fields_on_one_line(self) -> None:
        """Documented behavior: split halves are NOT reassembled.

        Redaction matches exact occurrences of a captured value. A credential
        deliberately cut in half across two fields never appears verbatim, so
        neither half is replaced. Reassembling arbitrary substring pairs would
        mean matching every prefix/suffix of every secret, which is both
        expensive and wildly false-positive-prone. The archive-time scanner and
        the fact that the halves are individually useless are the mitigations.
        """
        head, tail = SECRET[:12], SECRET[12:]
        line = json.dumps({"a": head, "b": tail})
        result = self.redactor.redact(line)
        self.assertEqual(result, line)
        # The full secret genuinely is absent from the line, so nothing leaked
        # in the form the redactor promises to catch.
        self.assertNotIn(SECRET, line)

    def test_secret_split_by_a_newline_is_not_reassembled(self) -> None:
        line = f"{SECRET[:12]}\n{SECRET[12:]}"
        self.assertEqual(self.redactor.redact(line), line)

    def test_whole_secret_on_one_of_two_fields_is_redacted(self) -> None:
        line = json.dumps({"a": SECRET, "b": "harmless"})
        result = self.assertRedacted(line)
        self.assertEqual(json.loads(result)["a"], REDACTED_API_KEY)
        self.assertEqual(json.loads(result)["b"], "harmless")


class TestIdempotenceAndDeterminism(RedactorTestCase):
    def test_redacting_twice_equals_redacting_once(self) -> None:
        text = (
            f"ALYSIS_API_KEY={SECRET}\n"
            f'{{"api_key": "q1W2e3R4t5Y6u7I8o9P0a1S2"}}\n'
            "Authorization: Bearer q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6\n"
            f"url=https://x/y?k={urllib.parse.quote(SECRET, safe='')}\n"
        )
        once = self.assertRedacted(text)
        self.assertEqual(self.redactor.redact(once), once)
        # And a third pass, to be sure nothing oscillates.
        self.assertEqual(self.redactor.redact(self.redactor.redact(once)), once)

    def test_placeholder_is_never_re_redacted(self) -> None:
        line = f'{{"api_key": "{REDACTED_PATTERN}", "k": "{REDACTED_API_KEY}"}}'
        self.assertEqual(self.redactor.redact(line), line)

    def test_deterministic_across_instances(self) -> None:
        text = f"key={SECRET} and Bearer q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6"
        first = lr.SecretRedactor(ENVIRON).redact(text)
        second = lr.SecretRedactor(dict(reversed(list(ENVIRON.items())))).redact(text)
        self.assertEqual(first, second)

    def test_empty_and_clean_input(self) -> None:
        self.assertEqual(self.redactor.redact(""), "")
        self.assertEqual(self.redactor.redact("nothing to see"), "nothing to see")


class TestDefaultRedactor(unittest.TestCase):
    def tearDown(self) -> None:
        lr.reset_default_redactor()

    def test_picks_up_a_credential_exported_after_import(self) -> None:
        import os

        lr.reset_default_redactor()
        name = "ALYSIS_TEST_ONLY_API_KEY"
        late = "Lm4Kp8Qz2Xv6Nb1Tc9Rd3Wf7"
        self.assertEqual(lr.redact_log_text(f"k={late}!"), f"k={late}!")
        os.environ[name] = late
        try:
            self.assertEqual(lr.redact_log_text(f"k={late}!"), f"k=«redacted:{name}»!")
        finally:
            del os.environ[name]
        # And it stops redacting once the variable is gone.
        self.assertEqual(lr.redact_log_text(f"k={late}!"), f"k={late}!")


class TestPerformanceBudget(RedactorTestCase):
    """100MB in well under a minute, measured here at 10MB with a 6s bound."""

    TARGET_MB = 10.0
    # 100MB / 60s scaled proportionally to 10MB.
    TIME_BUDGET_SECONDS = 6.0

    def test_ten_megabytes_within_proportional_budget(self) -> None:
        block = (
            "2026-08-21T10:00:00Z level=info msg=tool_result "
            'payload={"stdout": "HOME=/root\\nSHELL=/bin/bash\\n"} '
            f"ALYSIS_API_KEY={SECRET} "
            "Authorization: Bearer q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6 "
            "ordinary log prose that must survive untouched\n"
        )
        repeats = max(1, int((self.TARGET_MB * 1024 * 1024) / len(block)))
        text = block * repeats
        actual_mb = len(text) / (1024 * 1024)

        start = time.perf_counter()
        result = self.redactor.redact(text)
        elapsed = time.perf_counter() - start

        self.assertNotIn(SECRET, result)
        self.assertLess(
            elapsed,
            self.TIME_BUDGET_SECONDS,
            f"redacting {actual_mb:.1f}MB took {elapsed:.2f}s, "
            f"budget {self.TIME_BUDGET_SECONDS}s "
            f"(projected 100MB: {elapsed * 100.0 / actual_mb:.1f}s)",
        )
        print(
            f"\n[perf] {actual_mb:.1f}MB in {elapsed:.2f}s "
            f"({actual_mb / elapsed:.1f} MB/s, "
            f"projected 100MB: {elapsed * 100.0 / actual_mb:.1f}s)"
        )

    def test_clean_text_is_not_slower_than_the_budget(self) -> None:
        # The common case: a large log with no secrets in it at all.
        block = (
            "2026-08-21T10:00:00Z level=info msg=edit_file "
            "path=src/alysis_code/agent/session.py hunks=3 conflicts=0\n"
        )
        repeats = max(1, int((self.TARGET_MB * 1024 * 1024) / len(block)))
        text = block * repeats

        start = time.perf_counter()
        result = self.redactor.redact(text)
        elapsed = time.perf_counter() - start

        self.assertEqual(result, text)
        self.assertLess(elapsed, self.TIME_BUDGET_SECONDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
