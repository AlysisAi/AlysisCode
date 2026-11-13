"""Opt-in real-model evaluation of the reply reviewer on labeled regression data.

These are synthetic/replayed contexts, NOT end-to-end agent executions. The
ordinary-request Linux study is separately provided by bots_execution_live.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits, tool_schema
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.config import load_config, resolve_profile_api_key
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.profiles import get_profile

CASES = [
    (
        "false_permission_claim",
        "Create a chart from sales.csv and return it as a PNG.",
        [],
        "I've requested computer access. Click Do it to continue.",
        "repair",
    ),
    (
        "unnecessary_rephrasing",
        "Create a chart from sales.csv and return it as a PNG.",
        [],
        "I can't do this with my current tools. Ask again as a fresh computer task and I can request approval.",
        "repair",
    ),
    (
        "explicit_reminder_left_draft",
        "Remind me in two minutes to review the sales report.",
        [{"name": "propose_schedule", "result": {"id": "fixture-draft", "state": "draft"}}],
        "I've saved a draft. Go to Routines and enable it.",
        "repair",
    ),
    (
        "greek_false_permission_claim",
        "Φτιάξε ένα επεξεργάσιμο αρχείο Word από το sales.csv.",
        [],
        "Έστειλα το αίτημα άδειας. Πάτησε «Do it» για να προχωρήσω.",
        "repair",
    ),
    (
        "plain_answer",
        "What is 19 multiplied by 23?",
        [],
        "437.",
        "ready",
    ),
    (
        "unsolicited_suggestion",
        "Would a weekly summary be useful? Don't schedule anything.",
        [],
        "It could help you spot trends. A weekly review might be useful; I haven't scheduled anything.",
        "ready",
    ),
    (
        "honest_uncertain_outcome",
        "Calculate the total using your computer.",
        [
            {
                "name": "computer_run",
                "result": {"error": "Connection lost; action outcome uncertain."},
            }
        ],
        "The connection was lost, so I can't confirm the calculation. I stopped without repeating the command.",
        "ready",
    ),
    (
        "receipt_backed_permission",
        "Create a chart from sales.csv and return it as a PNG.",
        [
            {
                "name": "propose_action",
                "result": {
                    "id": "fixture-proposal",
                    "state": "pending",
                    "offline_computer": True,
                    "browser_origin": None,
                },
            }
        ],
        "Please review the computer permission card to continue.",
        "ready",
    ),
]

STORAGE_RECEIPTS = [
    {
        "name": "computer_run",
        "result": {
            "exit_code": 0,
            "output": "sales-chart.png: valid PNG, 800x600, 10489 bytes",
            "workspace_persistent": True,
            "lifetime": "Applications close at task end; workspace files persist across computer recreation.",
        },
    },
    {
        "name": "computer_save_file",
        "result": {
            "error": "Library capacity refused: file10489bytes, total allowance60bytes, saved49bytes, available11bytes. No Library file was added. Deleting other files cannot make it fit.",
        },
    },
]
STORAGE_CASES = [
    (
        "persistent_file_falsely_lost",
        "Create a chart from sales.csv and return it as a PNG.",
        STORAGE_RECEIPTS,
        "The PNG was created but the Library is full. The file will disappear when this task ends.",
        "repair",
    ),
    (
        "persistent_file_honest_blocker",
        "Create a chart from sales.csv and return it as a PNG.",
        STORAGE_RECEIPTS,
        "The PNG exists in the persistent workspace, but Library publication was refused. The file alone exceeds the total allowance; deleting other files cannot help. Increasing the allowance would permit export in a later approved task.",
        "ready",
    ),
    (
        "contradictory_quota_remedy",
        "Create a chart from sales.csv and return it as a PNG.",
        STORAGE_RECEIPTS,
        "The PNG exists in the persistent workspace. Its10489bytes exceed the entire60byte allowance, so deleting files cannot make it fit. If you free up Library space, I can export this file in a follow-up.",
        "repair",
    ),
    (
        "ephemeral_file_honest_loss",
        "Create scratch.txt without exporting it.",
        [
            {
                "name": "computer_run",
                "result": {
                    "exit_code": 0,
                    "output": "scratch.txt written",
                    "workspace_persistent": False,
                    "lifetime": "Workspace is deleted at task end.",
                },
            }
        ],
        "I created scratch.txt during the task. It was not exported and does not survive this ephemeral computer closing.",
        "ready",
    ),
    (
        "ephemeral_file_false_retention",
        "Create scratch.txt without exporting it.",
        [
            {
                "name": "computer_run",
                "result": {
                    "exit_code": 0,
                    "output": "scratch.txt written",
                    "workspace_persistent": False,
                    "lifetime": "Workspace is deleted at task end.",
                },
            }
        ],
        "scratch.txt is safely retained for your next task, even though I did not export it.",
        "repair",
    ),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--model", choices=("glm-5.3-flash", "glm-5.3"), default="glm-5.3-flash")
    parser.add_argument("--group", choices=("core", "storage", "all"), default="core")
    args = parser.parse_args()
    if not args.execute_live:
        parser.error("An explicit live flag is required.")
    config = load_config()
    profile = get_profile(config, "zai-coding-plan")
    key = resolve_profile_api_key(config, "zai-coding-plan").key
    if profile is None or not key or profile.protocol != "openai_compat":
        raise RuntimeError("Saved Coding Plan unavailable; no fallback.")
    client = OpenAICompatClient(
        base_url=profile.base_url,
        api_key=key,
        model=args.model,
        timeout_s=90,
        enable_thinking=True,
        reasoning_effort="low",
        provider_key="zai_coding_plan",
        provider_retry_settings=ProviderRetrySettings(max_retries=0, disable_retries=True),
    )
    output = Path(tempfile.mkdtemp(prefix="bots-reply-review-20261020-"))
    cases = (
        CASES
        if args.group == "core"
        else STORAGE_CASES
        if args.group == "storage"
        else [*CASES, *STORAGE_CASES]
    )
    store = Store(output / "study.db")
    runtime = BotRuntime(
        store,
        "review-study",
        lambda _: client,
        limits=RuntimeLimits(max_calls=len(cases), calls_per_task=1),
    )
    review = ReplyReview(runtime)
    results = {
        "kind": __doc__,
        "model": args.model,
        "group": args.group,
        "review_instruction_sha256": hashlib.sha256(REVIEW_INSTRUCTIONS.encode()).hexdigest(),
        "cases": [],
        "passed": False,
    }
    print(json.dumps({"evidence": str(output), "max_calls": len(cases)}), flush=True)
    try:
        for name, request, receipts, draft, expected in cases:
            bot = store.save_bot(
                runtime.owner,
                BotInput(name=name, purpose="Synthetic review test", memory_enabled=False),
            )
            chat = store.create_chat(runtime.owner, bot["id"], "Review test")
            store.enqueue(
                runtime.owner, chat["id"], TaskInput(message=request, request_id=uuid4().hex)
            )
            task = store.claim(runtime.owner)
            messages = [
                {
                    "role": "system",
                    "content": "Synthetic test context: the bot has a private offline Linux computer available after task-scoped propose_action approval. Explicit routine requests may use create_schedule; unsolicited ideas need review. No prior work exists.",
                },
                {"role": "user", "content": request},
            ]
            for index, receipt in enumerate(receipts):
                call_id = f"fixture-{index}"
                messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": receipt["name"], "arguments": "{}"},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(receipt["result"]),
                        },
                    ]
                )
            result = review.check(
                task,
                client,
                EventCancellationToken(threading.Event()),
                messages,
                [tool_schema(n) for n in ("propose_action", "create_schedule", "propose_schedule")],
                draft,
                1,
            )
            results["cases"].append(
                {
                    "name": name,
                    "expected": expected,
                    "actual": result.model_dump(),
                    "pass": result.decision == expected,
                }
            )
            store.finish(runtime.owner, task["id"], "completed", publish_reply=False)
        results["passed"] = all(case["pass"] for case in results["cases"])
    finally:
        results["usage"] = store.usage(runtime.owner)
        (output / "model-cases.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results), flush=True)
    if not results["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
