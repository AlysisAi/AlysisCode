"""Provider-free development fixtures and manual rubrics for live subagent work.

Held-out task contents live in a separate module. Listing and planning expose
identifiers and hashes; use ``describe`` deliberately to reveal a task's brief.
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.qa.subagent_quality_cases import ACCEPTANCE, FILES, FOLLOWUP, REQUIREMENTS


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    split: str
    family: str
    files: dict[str, str]
    dirty_files: dict[str, str]
    turns: tuple[str, ...]
    acceptance: str | None
    acceptance_stages: tuple[str, ...]
    rubric: tuple[str, ...]
    readonly: bool = False
    requires_subagents: bool = False


REPORT_RUBRIC = (
    "Judge the user's complete request, not process exit or code acceptance alone.",
    "Check substantive final answers against the actual source, patch and observed checks; "
    "flag unsupported claims, omitted limitations and missing requested deliverables.",
    "Treat tool/child reports as evidence to inspect, not authority. Distinguish source facts, "
    "observed checks, inherited evidence and inference.",
    "Assess whether delegation contributed independent useful work; do not reward child count "
    "or punish appropriate direct work. Compare duplication and meaningful overlap manually.",
    "Keep behavioral acceptance, authored test quality, final delivery, preservation and "
    "infrastructure outcomes separate. Never assign a quality score by keyword matching.",
)

RELAY_FILES = {
    "README.md": """# Relayboard

A dependency-free request dispatcher with an append-only local event log.
The public entry point is relayboard.gateway.handle_request(payload, log_path).
Handlers count values or summarize their numeric total. Routing and persistence
are separate modules so the service can add handlers without changing log I/O.
""",
    "relayboard/__init__.py": "",
    "relayboard/gateway.py": """from . import ledger, router


def handle_request(payload, log_path):
    kind = str(payload.get("kind", "summary")).strip().lower()
    values = list(payload.get("values", []))
    result = router.choose_handler(kind)(values)
    ledger.append(log_path, {"kind": kind, "result": result})
    return result
""",
    "relayboard/router.py": """from .handlers import count, summary

HANDLERS = {"count": count, "summary": summary}


def choose_handler(kind):
    return HANDLERS.get(kind, summary)
""",
    "relayboard/handlers.py": """def count(values):
    return {"count": len(values)}


def summary(values):
    return {"count": len(values), "total": sum(values)}
""",
    "relayboard/ledger.py": """import json
from pathlib import Path


def append(path, event):
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\\n")


def load(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
""",
    ".gitignore": "__pycache__/\n*.pyc\n",
}

PICKUP_TURN = (
    "Add pickup=True to quote and --pickup to the CLI. Pickup waives shipping even below "
    "the free-shipping threshold; normal delivery and coupons stay unchanged. Reuse the "
    "existing implementation and relevant findings, preserve the pre-existing currency "
    "setting, add useful tests and run relevant checks. Summarize the final behavior and "
    "exact verification results, including any checks that could not run. Do not commit."
)

QUOTE_RUBRIC = (
    "Verify list/item/quantity validation, SKU normalization and merging, two-decimal "
    "Decimal money, half-up whole-subtotal coupon discount, blank coupons and shipping.",
    "Inspect authored tests for discriminatory rounding ties, discount crossing, exact "
    "shipping boundary, malformed CLI/API input and preserved normal behavior.",
    "Confirm the pre-existing currency and user note remain byte-for-byte intact, HEAD "
    "and staged changes are preserved, and the final code is in the parent workspace.",
    "Require a substantive final description of behavior and actual verification, with "
    "unavailable or failed checks disclosed. A bare completion assertion is insufficient.",
)


def development_cases() -> dict[str, EvaluationCase]:
    dirty = {
        "orderquote/settings.py": 'CURRENCY = "EUR"\n',
        "user-note.txt": "Keep my shop configuration.\n",
    }
    inspection = EvaluationCase(
        id="dev-request-log",
        split="development",
        family="investigation-multiturn",
        files=RELAY_FILES,
        dirty_files={},
        turns=(
            "Inspect this repository and explain what the application does and how a request "
            "reaches its handler. Give a concise orientation with useful file references. "
            "Do not modify files or run tests.",
            "Now examine handler selection and event-log persistence independently. Explain "
            "each entry point and flow, and one concrete failure case the source permits in "
            "each. Reuse relevant earlier findings. Do not modify files or run tests.",
        ),
        acceptance=None,
        acceptance_stages=("readonly", "readonly"),
        rubric=(
            "Turn 1: correctly identify request dispatcher and gateway -> router -> handler "
            "-> ledger flow with actual file references.",
            "Turn 2: identify normalized kind selection, HANDLERS lookup and silent summary "
            "fallback; distinguish missing/unknown kind behavior from validation.",
            "Explain append-mode JSONL write and whole-file JSON loading; a malformed or "
            "blank record aborts load and a missing parent directory aborts append.",
            "Ground both risks in reachable source behavior; no fabricated paths, no tests "
            "or writes, and do not repeat the entire first-turn investigation.",
        ),
        readonly=True,
    )
    quote = EvaluationCase(
        id="dev-quote-chat",
        split="development",
        family="coding-multiturn",
        files=FILES,
        dirty_files=dirty,
        turns=(REQUIREMENTS, PICKUP_TURN),
        acceptance=ACCEPTANCE,
        acceptance_stages=("natural", "followup"),
        rubric=(
            *QUOTE_RUBRIC,
            "Second turn must retain context and integrate pickup without regressing delivery or coupons.",
        ),
    )
    followup = EvaluationCase(
        id="dev-retained-child",
        split="development",
        family="successful-child-followup",
        files=FILES,
        dirty_files=dirty,
        turns=(FOLLOWUP,),
        acceptance=ACCEPTANCE,
        acceptance_stages=("followup",),
        rubric=(
            *QUOTE_RUBRIC,
            "Trace initial child success before explicit pickup continuation, same retained "
            "candidate/lineage, final patch review and exactly one final application. "
            "Distinguish recovered interruptions from fresh-child replacement.",
        ),
        requires_subagents=True,
    )
    checklist = EvaluationCase(
        id="dev-check-limitation",
        split="development",
        family="completion-checklist",
        files=FILES,
        dirty_files=dirty,
        turns=(
            REQUIREMENTS + "\nAlso run python3 -m unittest discover -s tests -v and python3 -m "
            "external_shop_audit. The external_shop_audit module is not supplied in this "
            "fixture; do not invent it or install dependencies. Complete the local work "
            "and report the unavailable external audit alongside the exact checks that ran.",
        ),
        acceptance=ACCEPTANCE,
        acceptance_stages=("natural",),
        rubric=(
            *QUOTE_RUBRIC,
            "Final must preserve the implemented result and disclose the missing external audit; never claim that audit passed or replace the answer with a generic fallback.",
        ),
    )
    return {case.id: case for case in (inspection, quote, followup, checklist)}


def all_cases() -> dict[str, EvaluationCase]:
    from scripts.qa.subagent_eval_heldout import heldout_cases

    return {**development_cases(), **heldout_cases()}
