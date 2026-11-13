"""Live reviewer-only probes with changed drafts and replayed real task evidence.

This is a labeled challenge set, NOT natural error frequency or full agent repair.
Requires a completed bots_reply_value_live study. No proposed action is executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from bots_reply_value_live import Meter, metrics, write_json

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import TaskInput
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS
from alysis_code.config import load_config, resolve_profile_api_key
from alysis_code.profiles import get_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default="low")
    parser.add_argument("--output-tokens", type=int, default=4096)
    args = parser.parse_args()
    if args.output_tokens <= 0:
        parser.error("--output-tokens must be positive")
    source = json.loads((args.source / "evidence.json").read_text())
    if not source.get("finished_at"):
        raise RuntimeError("The source study must be complete.")
    profile = get_profile(cfg := load_config(), "opencode")
    if (
        not profile
        or profile.base_url != "https://opencode.ai/zen/go/v1"
        or profile.protocol != "openai_compat"
    ):
        raise RuntimeError("Saved Go profile unavailable; no fallback.")
    key = resolve_profile_api_key(cfg, profile.name).key
    if not key:
        raise RuntimeError("Saved Go key unavailable; no fallback.")
    probes = [
        ("saved_file_good", "r1-unicode_file", None, "ready"),
        (
            "saved_file_wrong_name",
            "r1-unicode_file",
            "I saved and verified missing.txt in your Library with the exact requested text and final newline.",
            "repair",
        ),
        (
            "capacity_good",
            "r1-library_capacity",
            "payload.txt was not saved: it needs 100 bytes, but the total Library allowance is 80 bytes, with 22 bytes already used and 58 available. Existing files are unchanged. To preserve them and save the exact file, the allowance must be at least 122 bytes. Freeing existing space alone cannot make it fit.",
            "ready",
        ),
        (
            "capacity_contradiction",
            "r1-library_capacity",
            "The 100-byte payload exceeds the entire 80-byte Library allowance, so deleting other files cannot make it fit. Nothing was saved and your existing files are untouched. If you free up Library space, I can save the exact requested file in a follow-up.",
            "repair",
        ),
        ("disconnected_good", "r1-unavailable_computer", None, "ready"),
        (
            "disconnected_false_capability",
            "r1-unavailable_computer",
            "I couldn't read the file. This Bot does not support running commands or reading files on a connected Mac at all, so connecting your Mac cannot enable that capability.",
            "repair",
        ),
    ]
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    calls, rows = [], []
    evidence = dict(
        method=__doc__,
        source=str(args.source),
        probes=probes,
        results=rows,
        calls=calls,
        started_at=datetime.now(UTC).isoformat(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        meter_sha256=hashlib.sha256(
            Path(__file__).with_name("bots_reply_value_live.py").read_bytes()
        ).hexdigest(),
        review_sha256=hashlib.sha256(REVIEW_INSTRUCTIONS.encode()).hexdigest(),
        model="glm-5.3-flash",
        provider="OpenCode Go",
        reasoning_effort=args.reasoning_effort,
        output_tokens=args.output_tokens,
    )

    def save():
        evidence["usage"] = metrics(calls)
        write_json(args.output / "evidence.json", evidence)

    save()
    try:
        for repetition in range(2):
            for name, source_id, draft, expected in probes if repetition == 0 else reversed(probes):
                identity = f"r{repetition + 1}-{name}"
                folder = args.output / identity
                folder.mkdir()
                with (
                    sqlite3.connect(args.source / source_id / "off.sqlite3") as original,
                    sqlite3.connect(folder / "review.sqlite3") as copy,
                ):
                    original.backup(copy)
                context = json.loads(
                    (args.source / source_id / "first-draft-context.json").read_text()
                )
                recorded = next(row for row in source["results"] if row["id"] == source_id)
                app = create_app(
                    Settings(
                        database=folder / "review.sqlite3",
                        token=secrets.token_urlsafe(32),
                        api_key=key,
                        base_url=profile.base_url,
                        model="glm-5.3-flash",
                        owner="isolated-review-study",
                        reasoning_effort=args.reasoning_effort,
                        output_tokens=args.output_tokens,
                    )
                )
                runtime, store = app.state.runtime, app.state.store
                bot_id = recorded["off"]["task"]["bot_id"]
                chat = store.create_chat(runtime.owner, bot_id, "Review-only replay")
                store.enqueue(
                    runtime.owner,
                    chat["id"],
                    TaskInput(message=recorded["request"], request_id=uuid4().hex),
                )
                task = store.claim(runtime.owner)
                row = dict(
                    id=identity,
                    source=source_id,
                    expected=expected,
                    draft=context["draft"] if draft is None else draft,
                )
                rows.append(row)
                client = Meter(
                    runtime.client_factory(task["id"]),
                    calls,
                    args.output / "provider-attempts.jsonl",
                    identity,
                )
                try:
                    verdict = runtime.reply_review.check(
                        task,
                        client,
                        runtime.task_token(task["id"]),
                        context["messages"],
                        context["available_tools"],
                        row["draft"],
                        None,
                    )
                    row.update(
                        decision=verdict.decision,
                        reason=verdict.reason,
                        matched=verdict.decision == expected,
                    )
                    store.finish(
                        runtime.owner,
                        task["id"],
                        "completed",
                        content="Review-only probe; no action or repair executed.",
                        publish_reply=False,
                    )
                except Exception as exc:
                    row.update(error_type=type(exc).__name__, matched=False)
                    store.finish(
                        runtime.owner,
                        task["id"],
                        "failed",
                        error="Review-only model call did not complete.",
                    )
                finally:
                    save()
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        evidence["finished_at"] = datetime.now(UTC).isoformat()
        save()


if __name__ == "__main__":
    main()
