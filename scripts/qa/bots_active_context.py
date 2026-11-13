"""Explicit live-model study of paging old tool output during one Bots task."""

import json
from uuid import uuid4

from alysis_code.bots.app import create_app
from alysis_code.bots.models import BotInput, FileInput, TaskInput


def check(settings, output):
    app = create_app(settings)
    store, runtime = app.state.store, app.state.runtime
    bot = store.save_bot(
        settings.owner,
        BotInput(
            name="Context verification",
            purpose="Read supplied reference files and verify exact original figures.",
        ),
    )
    chat = store.create_chat(settings.owner, bot["id"], "Context verification")
    names = []
    expected = []
    for index in range(3):
        name = f"reference-{index + 1}.txt"
        amount = f"{int(uuid4().hex[:5], 16)}.25"
        code = uuid4().hex[:12]
        # Different real input content on every run; the model must read it.
        filler = "\n".join(
            f"Field note {j}: routine observations are provisional and contain no settlement amount."
            for j in range(210)
        )
        text = (
            filler[:9600]
            + f"\nSettlement 東京-{index + 1}: {amount}; confirmation {code}\n"
            + filler[9600:]
        )
        store.save_file(settings.owner, bot["id"], FileInput(name=name, content=text))
        names.append(name)
        expected.append([name, amount, code])
    message = (
        "Read all three supplied reference files in full (use length 20000 for each read_file) before making a comparison: "
        + ", ".join(names)
        + ". Then create verified-settlements.csv with columns file,amount,confirmation, one row for each original Settlement line. Read original source data to verify exact values. If an earlier result has moved out of context, use read_tool_result to recover its Settlement line without repeating the original read. Keep your reply brief."
    )
    queued = store.enqueue(
        settings.owner, chat["id"], TaskInput(message=message, request_id=uuid4().hex)
    )
    task = store.claim(settings.owner)
    assert task["id"] == queued["id"]
    evidence = {"passed": False, "mode": "real saved Z.ai Coding Plan, generated synthetic input"}
    try:
        runtime.run(task, runtime.task_token(task["id"]))
        final = store.task(settings.owner, task["id"])
        with store.connect() as db:
            events = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM events WHERE task_id=? ORDER BY rowid", (task["id"],)
                )
            ]
            archives = [
                dict(row)
                for row in db.execute(
                    "SELECT id,tool_name,length(content) AS characters FROM task_tool_results WHERE task_id=?",
                    (task["id"],),
                )
            ]
            files = [
                dict(row)
                for row in db.execute("SELECT id,name FROM files WHERE bot_id=?", (bot["id"],))
            ]
        artifact = next(f for f in files if f["name"] == "verified-settlements.csv")
        content = store.file(settings.owner, bot["id"], artifact["id"])["content"]
        import csv
        import io

        rows = list(csv.reader(io.StringIO(content)))
        assert rows[0] == ["file", "amount", "confirmation"]
        assert sorted(rows[1:]) == sorted(expected)
        recovered = [
            e
            for e in events
            if e["kind"] == "tool_completed"
            and json.loads(e["data"]).get("name") == "read_tool_result"
        ]
        assert archives and recovered
        assert final["status"] == "completed", final["error"]
        evidence.update(
            passed=True,
            task_id=task["id"],
            status=final["status"],
            archives=archives,
            recovered_calls=len(recovered),
            artifact_verified=True,
            expected=expected,
            artifact=artifact,
        )
    except BaseException as exc:
        evidence.update(
            error_type=type(exc).__name__,
            task_id=task["id"],
            task=store.task(settings.owner, task["id"]),
        )
        raise
    finally:
        runtime.stop()
        evidence["usage"] = store.usage(settings.owner)
        (output / "context-receipt.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )
    return evidence
