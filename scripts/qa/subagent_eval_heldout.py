"""Held-out briefs/oracles: do not inspect while tuning shared/profile guidance.

Freeze this file's hash before running development comparisons. Detailed briefs
are intentionally separate from the public development-case catalog.
"""

from __future__ import annotations

from scripts.qa.subagent_eval_cases import EvaluationCase

INVENTORY_FILES = {
    "README.md": """# Stockmoves

A dependency-free movement ledger. inventory.engine.balance(rows) summarizes
signed stock movements; python -m inventory accepts SKU:delta tokens.
Configuration belongs in inventory/settings.py. Run checks with
python3 -m unittest discover -s tests -v.
""",
    "inventory/__init__.py": "",
    "inventory/settings.py": 'SITE = "main"\n',
    "inventory/engine.py": """from .settings import SITE


def balance(rows):
    return {"site": SITE, "items": rows}
""",
    "inventory/__main__.py": """import json
import sys
from .engine import balance

print(json.dumps(balance([{"sku": s.split(":")[0], "delta": int(s.split(":")[1])} for s in sys.argv[1:]])))
""",
    "tests/test_smoke.py": """import unittest
from inventory.engine import balance
from inventory.settings import SITE


class Smoke(unittest.TestCase):
    def test_site(self):
        self.assertEqual(balance([{"sku": "nut", "delta": 1}])["site"], SITE)
""",
    ".gitignore": "__pycache__/\n*.pyc\n",
}

INVENTORY_ACCEPTANCE = """import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(os.environ["ALYSIS_EVAL_WORKSPACE"])
sys.path.insert(0, str(ROOT))
from inventory.engine import balance


class Contract(unittest.TestCase):
    def test_normalize_merge_sort(self):
        self.assertEqual(balance([{"sku": " BOLT ", "delta": 4}, {"sku": "nut", "delta": -2}, {"sku": "bolt", "delta": -1}]), {"site": "west", "items": [{"sku": "bolt", "quantity": 3}, {"sku": "nut", "quantity": -2}], "net_change": 1})

    def test_unicode_casefold(self):
        result = balance([{"sku": " Straße ", "delta": 2}, {"sku": "STRASSE", "delta": -1}, {"sku": "Δ", "delta": 1}, {"sku": "δ", "delta": 2}])
        self.assertEqual(result["items"], [{"sku": "strasse", "quantity": 1}, {"sku": "δ", "quantity": 3}])

    def test_invalid_shape_and_delta(self):
        for rows in (None, (), [], [None], [{}], [{"sku": "", "delta": 1}], [{"sku": "x", "delta": 0}], [{"sku": "x", "delta": True}], [{"sku": "x", "delta": 1.5}], [{"sku": "x", "delta": "2"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                balance(rows)

    def test_preserves_input_and_zero_balance(self):
        rows = [{"sku": " x ", "delta": 2}, {"sku": "X", "delta": -2}]
        before = json.dumps(rows)
        self.assertEqual(balance(rows)["items"], [{"sku": "x", "quantity": 0}])
        self.assertEqual(json.dumps(rows), before)

    def cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-B", "-m", "inventory", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)

    def test_cli(self):
        result = self.cli(" BOLT :4", "bolt:-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), balance([{"sku": "BOLT", "delta": 3}]))

    def test_cli_error(self):
        for token in ("x", "x:1.5", "x:0", ":1", "x:1:2"):
            with self.subTest(token=token):
                result = self.cli(token)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr.strip())
                self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(os.environ["ALYSIS_EVAL_CASE_STAGE"] == "followup", "second turn only")
    def test_omit_zero(self):
        rows = [{"sku": "x", "delta": 1}, {"sku": "X", "delta": -1}, {"sku": "z", "delta": -2}]
        self.assertEqual(balance(rows, omit_zero=True), {"site": "west", "items": [{"sku": "z", "quantity": -2}], "net_change": -2})
        result = self.cli("x:1", "x:-1", "--omit-zero")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["items"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
"""

CACHE_FILES = {
    "README.md": """# Renderkit

Renderkit translates template jobs into text. service.render_job calls a named
renderer, caching output in memory for one process. Entries never persist to disk.
""",
    "renderkit/__init__.py": "",
    "renderkit/service.py": """from .cache import CACHE
from .renderers import RENDERERS


def render_job(job):
    name = job["template"]
    key = (name, job["customer"])
    if key not in CACHE:
        CACHE[key] = RENDERERS[name](job)
    return CACHE[key]
""",
    "renderkit/cache.py": "CACHE = {}\n",
    "renderkit/renderers.py": """def receipt(job):
    return f"{job['customer']}: {job['amount']} {job['currency']}"


RENDERERS = {"receipt": receipt}
""",
    ".gitignore": "__pycache__/\n*.pyc\n",
}

REVIEW_ENGINE = """def balance(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("nonempty list required")
    result = {}
    for row in rows:
        sku = row["sku"].strip().casefold()
        delta = row["delta"]
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise ValueError("nonzero integer required")
        result[sku] = result.get(sku, 0) + delta
    return result
"""


def heldout_cases() -> dict[str, EvaluationCase]:
    investigation = EvaluationCase(
        id="hold-render-cache",
        split="heldout",
        family="investigation-multiturn",
        files=CACHE_FILES,
        dirty_files={},
        turns=(
            "Orient me in this small application: what it does and the request path. "
            "Use verified file references and stay concise. Inspect only; do not edit or run tests.",
            "Explain renderer selection and result caching as separate concerns. Give one "
            "concrete source-backed risk for each and an example request sequence that "
            "demonstrates the cache risk. Reuse your first-turn context. No edits or tests.",
        ),
        acceptance=None,
        acceptance_stages=("readonly", "readonly"),
        rubric=(
            "Identify service.render_job -> named RENDERERS function and process-local CACHE.",
            "Unknown template is direct dictionary indexing and raises KeyError, not fallback.",
            "Cache key omits amount/currency: same template/customer with a later changed "
            "amount or currency returns stale prior receipt. Supply a concrete two-call sequence.",
            "Do not claim disk persistence, expiry, shared multiprocess state or automatic "
            "invalidation. Cite real paths; preserve all files and avoid running tests.",
        ),
        readonly=True,
    )
    coding = EvaluationCase(
        id="hold-stock-chat",
        split="heldout",
        family="coding-multiturn",
        files=INVENTORY_FILES,
        dirty_files={
            "inventory/settings.py": 'SITE = "west"\n',
            "user-note.txt": "Keep my regional site.\n",
        },
        turns=(
            "Implement inventory.engine.balance(rows) and the inventory CLI. rows is a "
            "nonempty list of dictionaries with sku (nonempty string) and delta (nonzero "
            "signed integer; reject bool, strings and fractional numbers). Raise ValueError "
            "for invalid input. Trim SKU whitespace and normalize with Unicode casefold, "
            "merge repeated SKUs and sort them. Return site, items [{sku, quantity}], and "
            "net_change, where quantity sums deltas and net_change sums all quantities. "
            "Negative and zero resulting balances are valid. Do not change caller inputs. "
            "python -m inventory SKU:delta ... must emit this JSON; bad input exits 2 with "
            "helpful stderr and no traceback. Keep my existing uncommitted site setting. "
            "Add useful tests, run relevant checks and summarize the result. Do not commit.",
            "Add omit_zero=True to the API and --omit-zero to the CLI to exclude zero "
            "balances. Default behavior and net_change stay unchanged. Reuse the existing "
            "implementation, preserve my site setting, add tests and verify the result. "
            "Summarize what changed and the checks that actually ran, including any limitation.",
        ),
        acceptance=INVENTORY_ACCEPTANCE,
        acceptance_stages=("natural", "followup"),
        rubric=(
            "Check Unicode casefold/merging, signed integer validation, retained zero/negative "
            "balances, sorted output and unchanged caller input; no ASCII-specific lookup fixes.",
            "Second turn must integrate API/CLI omit-zero without changing default behavior "
            "or net_change; preserve dirty site/note, staged state and HEAD.",
            "Assess actual tests and final reporting, including malformed CLI and zero-balance "
            "edge coverage. Do not substitute code acceptance for a complete user handoff.",
        ),
    )
    review = EvaluationCase(
        id="hold-dirty-review",
        split="heldout",
        family="review-dirty-work",
        files={
            "README.md": """# Movement normalizer

balance takes a nonempty list of movements and combines Unicode-casefolded SKUs.
Deltas must be nonzero signed integers; bool, fractional numbers and numeric
strings are invalid and must raise ValueError. Review the uncommitted patch.
""",
            "engine.py": REVIEW_ENGINE,
            "settings.py": 'SITE = "main"\n',
            ".gitignore": "__pycache__/\n*.pyc\n",
        },
        dirty_files={
            "engine.py": REVIEW_ENGINE.replace(".casefold()", ".lower()").replace(
                'delta = row["delta"]', 'delta = int(row["delta"])'
            ),
            "settings.py": 'SITE = "west"\n',
        },
        turns=(
            "Review the current uncommitted patch against the documented contract. Identify "
            "actionable correctness issues with useful file/line references and concrete "
            "examples. The site configuration is my unrelated local change; preserve it. "
            "This is review only: do not modify files or run tests.",
        ),
        acceptance=None,
        acceptance_stages=("readonly",),
        rubric=(
            "Identify int coercion admitting bool/numeric strings and truncating fractional "
            "deltas before the type guard, with a concrete example.",
            "Identify lower versus required Unicode casefold as a merge regression, e.g. "
            "Straße/STRASSE. Treat dirty site setting as unrelated, not a requested revert.",
            "Prioritize actual diff regressions; no invented issues, blanket rewrite or changes/tests.",
        ),
        readonly=True,
    )
    return {case.id: case for case in (investigation, coding, review)}
