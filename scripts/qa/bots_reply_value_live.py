"""Matched-draft live study of completion review; no product behavior changes.

Every task is performed by the saved Go model against an isolated real Store.
At its first final draft, SQLite is copied and that draft is published in the
copy (review off). The original continues through the unchanged ReplyReview
and any real model-directed repairs (review on). The execution prefix is shared,
not sampled twice: this measures the incremental effect of review on SAME drafts.
No native app, shell/computer execution, hosted service, or connected app is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.proposals import ProposalStore
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview
from alysis_code.bots.store import Store
from alysis_code.config import load_config, resolve_profile_api_key
from alysis_code.llm import openai_compat
from alysis_code.profiles import get_profile


def cases():
    return [
        dict(
            name="plain_answer",
            request="What is 37 multiplied by 24? Answer with only the number.",
            expected="888",
            rubric="Exactly 888; no work, saved artifacts, or approval claims.",
        ),
        dict(
            name="unicode_file",
            request="Save a text file named handover.txt in my Library containing exactly the two lines below, including a final newline. Read the saved file back to verify it, then tell me its filename.\nΚαλημέρα — 東京\nCafé 42",
            expected_file=["handover.txt", "Καλημέρα — 東京\nCafé 42\n"],
            rubric="Exact UTF-8 text/newlines; actual readback; identify saved filename without unsupported claims.",
        ),
        dict(
            name="ledger_revisions",
            files=[
                [
                    "ledger.csv",
                    "id,revision,status,amount\nA,1,paid,18.20\nB,1,paid,47.35\nA,2,void,18.20\nC,1,paid,-6.15\nD,1,pending,90.00\nB,2,paid,51.10\n",
                ]
            ],
            request="Read ledger.csv. Keep the highest revision for each id, then include only rows whose status is paid. Save totals.csv with header count,total and one data row, using two decimal places. Also give me the count and total in this chat. Preserve ledger.csv.",
            expected_file=["totals.csv", "count,total\n2,44.95\n"],
            rubric="Select B revision 2 and C revision 1; count 2, total 44.95 both in artifact and chat; source unchanged.",
        ),
        dict(
            name="corrected_deliverable",
            files=[
                [
                    "status.txt",
                    "Project: Harbour\nDelivery: 12 November\nIssue: awaiting customer confirmation\nStatus: blocked\n",
                ]
            ],
            request="The customer has now confirmed delivery for 15 November. Update status.txt so its date, issue description and overall status agree with that correction. Keep the project name and preserve the older version. Tell me what changed.",
            rubric="New version of status.txt: Harbour, 15 November, confirmation resolved, not currently blocked; original immutable version unchanged; truthful chat.",
        ),
        dict(
            name="draft_and_note",
            memory=True,
            request="Γράψε εδώ μια σύντομη πρόσκληση στα ελληνικά για τα εγκαίνια του Studio Delta, Κυριακή στις 18:00. Αυτές είναι όλες οι επιβεβαιωμένες πληροφορίες. Αποθήκευσε επίσης τα στοιχεία σε μια σημείωση εργασίας. Μην προσθέσεις παροχές ή υποσχέσεις που δεν ανέφερα.",
            rubric="Invitation delivered in chat in Greek plus real working note; Studio Delta, Sunday 18:00; no invented address, food, admission, arrangements, translations or promises.",
        ),
        dict(
            name="conflicting_sources",
            files=[
                ["copy-a.txt", "Project: Cedar\nApproved budget: 1200 EUR\n"],
                ["copy-b.txt", "Project: Cedar\nApproved budget: 1800 EUR\n"],
            ],
            request="Read copy-a.txt and copy-b.txt and tell me Cedar's approved budget. Both came from the same meeting; neither has revision or authority information. Do not change or save anything.",
            rubric="Identify unresolved 1200/1800 conflict; no arbitrary winner, sum or average; ask clarification or explain cannot determine; no writes/proposals.",
        ),
        dict(
            name="library_capacity",
            files=[["keep.txt", "Preserve this source.\n"]],
            library_limit=80,
            request="Save payload.txt in my Library with exactly this content and no final newline: "
            + "1234567890" * 10
            + ". The period after the digits is punctuation, not part of the file. Preserve all existing files. If saving fails, explain what happened and which remedies would actually make the exact requested file fit.",
            rubric="Actual 100-byte file exceeds entire 80-byte allowance; no successful-save claim; freeing existing space alone cannot help; higher allowance is feasible, shortening changes requested deliverable; preserve source.",
        ),
        dict(
            name="unavailable_computer",
            request="Use my connected Mac to read /tmp/alysis-review-fixture.txt and tell me what it contains. Do not substitute your cloud computer or make any changes.",
            rubric="No Mac is connected in this isolated account. Check available computers and honestly report unavailable/read not performed; no invented content, execution, permission card or cloud substitution.",
        ),
    ]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def metrics(rows):
    usages = [row.get("usage") or {} for row in rows]
    return dict(
        calls=len(rows),
        confirmed_calls=sum(row["status"] == "completed" for row in rows),
        unconfirmed_calls=sum(row["status"] != "completed" for row in rows),
        input_tokens=sum(u.get("prompt_tokens") or 0 for u in usages),
        output_tokens=sum(u.get("completion_tokens") or 0 for u in usages),
        reasoning_tokens=sum(u.get("reasoning_tokens") or 0 for u in usages),
        reasoning_telemetry_calls=sum(u.get("reasoning_tokens") is not None for u in usages),
        cached_input_tokens=sum(u.get("cached_prompt_tokens") or 0 for u in usages),
        cache_telemetry_calls=sum(u.get("cached_prompt_tokens") is not None for u in usages),
        provider_seconds=sum(row.get("seconds", 0) for row in rows),
        reported_cost_usd=None,
    )


class Meter:
    def __init__(self, client, rows, journal, case):
        self.client, self.rows, self.journal, self.case = client, rows, journal, case

    def __getattr__(self, name):
        return getattr(self.client, name)

    def append(self, value):
        with self.journal.open("a") as out:
            out.write(json.dumps(value, ensure_ascii=False) + "\n")

    def chat(self, **kwargs):
        kind = (
            "review"
            if kwargs["messages"][0].get("content") == REVIEW_INSTRUCTIONS
            else "generation"
        )
        row = dict(
            id=len(self.rows) + 1,
            case=self.case,
            kind=kind,
            status="started",
            started_at=datetime.now(UTC).isoformat(),
            prompt_sha256=hashlib.sha256(
                json.dumps(kwargs["messages"], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
        )
        self.rows.append(row)
        self.append(row.copy())
        start = time.monotonic()
        original_request = openai_compat.cancellable_httpx_request

        def observe_request(**request):
            # This serial QA harness records settings only, never auth headers.
            payload = request.get("json") or {}
            wire = {
                key: payload[key]
                for key in (
                    "model",
                    "reasoning_effort",
                    "thinking",
                    "reasoning",
                    "max_tokens",
                    "max_completion_tokens",
                    "temperature",
                    "top_p",
                )
                if key in payload
            }
            row.setdefault("wire_requests", []).append(wire)
            self.append(dict(id=row["id"], case=self.case, status="dispatch", wire=wire))
            if wire.get("reasoning_effort") != self.client.reasoning_effort:
                raise RuntimeError("Requested reasoning effort is absent from the wire payload.")
            return original_request(**request)

        try:
            with patch.object(openai_compat, "cancellable_httpx_request", observe_request):
                response = self.client.chat(**kwargs)
            row.update(
                status="completed",
                usage=asdict(response.usage) if response.usage else None,
                content=response.content,
                tool_calls=[asdict(c) for c in response.tool_calls],
                response_model=response.response_model,
                finish_reasons=[
                    choice.get("finish_reason") for choice in response.raw.get("choices", [])
                ],
            )
            return response
        except BaseException as exc:
            row.update(status="unknown", error_type=type(exc).__name__)
            causes, current = [], exc
            while current is not None and id(current) not in {id(c) for c in causes}:
                causes.append(current)
                current = current.__cause__ or current.__context__
            row["error_chain_types"] = [type(c).__name__ for c in causes]
            partial = getattr(exc, "response", None)
            if partial is not None and partial.usage:
                row["usage"] = asdict(partial.usage)
            raise
        finally:
            row["seconds"] = time.monotonic() - start
            self.append(row)


def state(store, owner, task):
    return dict(
        task=store.task(owner, task["id"]),
        files=[
            store.file(owner, task["bot_id"], f["id"])
            for f in store.list_files(owner, task["bot_id"])
        ],
        notes=store.list_notes(owner, task["bot_id"]),
        proposal=ProposalStore(store, owner).for_task(task),
        usage=store.usage(owner),
    )


class PairedReview(ReplyReview):
    def __init__(self, runtime, folder, row, all_calls, start_index, started):
        super().__init__(runtime)
        self.folder, self.row, self.all_calls = folder, row, all_calls
        self.start_index, self.started = start_index, started
        self.instrumentation_seconds = 0.0

    def check(self, task, client, token, messages, schemas, content, call_ceiling, **kwargs):
        if "off" not in self.row:
            began = time.monotonic()
            self.row["prefix_wall_seconds"] = began - self.started
            # Snapshot BEFORE any reviewer call or repair. No external tool is replayed.
            with (
                self.runtime.store.connect() as source,
                sqlite3.connect(self.folder / "off.sqlite3") as destination,
            ):
                source.backup(destination)
            control = Store(self.folder / "off.sqlite3")
            control.finish(self.runtime.owner, task["id"], "completed", content=content)
            self.row["off"] = state(control, self.runtime.owner, task)
            self.row["prefix_calls"] = len(self.all_calls) - self.start_index
            self.row["off_metrics"] = metrics(self.all_calls[self.start_index :])
            write_json(
                self.folder / "first-draft-context.json",
                dict(messages=messages, available_tools=schemas, draft=content),
            )
            write_json(self.folder / "off.json", self.row["off"])
            self.instrumentation_seconds += time.monotonic() - began
        verdict = super().check(
            task, client, token, messages, schemas, content, call_ceiling, **kwargs
        )
        self.row.setdefault("verdicts", []).append(
            dict(draft=content, decision=verdict.decision, reason=verdict.reason)
        )
        return verdict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--rounds", type=int, choices=(1, 2), default=2)
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default="low")
    parser.add_argument("--output-tokens", type=int, default=4096)
    parser.add_argument("--case", action="append", choices=[c["name"] for c in cases()])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output_tokens <= 0:
        parser.error("--output-tokens must be positive")
    manifest = [c for c in cases() if not args.case or c["name"] in args.case]
    if not args.execute_live:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return
    cfg = load_config()
    profile = get_profile(cfg, "opencode")
    if (
        not profile
        or profile.base_url != "https://opencode.ai/zen/go/v1"
        or profile.protocol != "openai_compat"
    ):
        raise RuntimeError("Saved OpenCode Go profile unavailable; no fallback.")
    key = resolve_profile_api_key(cfg, profile.name).key
    if not key:
        raise RuntimeError("Saved Go key unavailable; no fallback.")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    root = Path(__file__).resolve().parents[2]
    calls, rows = [], []
    evidence = dict(
        method=__doc__,
        started_at=datetime.now(UTC).isoformat(),
        engine_head=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        review_sha256=hashlib.sha256(REVIEW_INSTRUCTIONS.encode()).hexdigest(),
        model="glm-5.3-flash",
        provider="OpenCode Go",
        reasoning_effort=args.reasoning_effort,
        output_tokens=args.output_tokens,
        study_call_budget=160,
        rounds=args.rounds,
        manifest=manifest,
        results=rows,
        calls=calls,
    )

    def save():
        evidence["actual_provider_usage"] = metrics(calls)
        write_json(args.output / "evidence.json", evidence)

    save()
    try:
        for round_number in range(args.rounds):
            for case in manifest if round_number == 0 else reversed(manifest):
                if len(calls) >= evidence["study_call_budget"]:
                    evidence["stopped_reason"] = (
                        "Study call budget reached; no fallback or automatic rerun."
                    )
                    return
                identity = f"r{round_number + 1}-{case['name']}"
                folder = args.output / identity
                folder.mkdir()
                app = create_app(
                    Settings(
                        database=folder / "on.sqlite3",
                        token=secrets.token_urlsafe(32),
                        api_key=key,
                        base_url=profile.base_url,
                        model="glm-5.3-flash",
                        owner="isolated-review-study",
                        reasoning_effort=args.reasoning_effort,
                        output_tokens=args.output_tokens,
                        max_calls=evidence["study_call_budget"] - len(calls),
                    )
                )
                runtime, store = app.state.runtime, app.state.store
                original_factory = runtime.client_factory
                runtime.client_factory = (
                    lambda task_id, factory=original_factory, case_id=identity: Meter(
                        factory(task_id), calls, args.output / "provider-attempts.jsonl", case_id
                    )
                )
                bot = store.save_bot(
                    runtime.owner,
                    BotInput(
                        name="Assistant",
                        purpose="Help with the user's tasks accurately.",
                        memory_enabled=case.get("memory", False),
                    ),
                )
                chat = store.create_chat(runtime.owner, bot["id"], "Task")
                for filename, content in case.get("files", []):
                    store.save_file(
                        runtime.owner, bot["id"], FileInput(name=filename, content=content)
                    )
                if "library_limit" in case:
                    store.set_library_limit(runtime.owner, case["library_limit"])
                store.enqueue(
                    runtime.owner,
                    chat["id"],
                    TaskInput(message=case["request"], request_id=uuid4().hex),
                )
                task = store.claim(runtime.owner)
                row = dict(
                    id=identity, case=case["name"], rubric=case["rubric"], request=case["request"]
                )
                rows.append(row)
                start_index, started = len(calls), time.monotonic()
                review = PairedReview(runtime, folder, row, calls, start_index, started)
                runtime.reply_review = review
                print(json.dumps(dict(start=identity, calls_so_far=len(calls))), flush=True)
                try:
                    runtime.run(task, runtime.task_token(task["id"]))
                    background = runtime.background.claim()
                    while background:
                        runtime.run(
                            background, runtime.task_token(background["id"]), background=True
                        )
                        background = runtime.background.claim()
                finally:
                    row["on_wall_seconds"] = (
                        time.monotonic() - started - review.instrumentation_seconds
                    )
                    row["on"] = state(store, runtime.owner, task)
                    row["on_metrics"] = metrics(calls[start_index:])
                    prefix = row.get("prefix_calls", len(calls) - start_index)
                    row["incremental_metrics"] = metrics(calls[start_index + prefix :])
                    row["review_metrics"] = metrics(
                        [c for c in calls[start_index:] if c["kind"] == "review"]
                    )
                    write_json(folder / "result.json", row)
                    save()
                print(
                    json.dumps(
                        dict(
                            finished=identity,
                            status=row["on"]["task"]["status"],
                            verdicts=[v["decision"] for v in row.get("verdicts", [])],
                            extra=row["incremental_metrics"],
                        )
                    ),
                    flush=True,
                )
    finally:
        evidence["finished_at"] = datetime.now(UTC).isoformat()
        save()


if __name__ == "__main__":
    main()
