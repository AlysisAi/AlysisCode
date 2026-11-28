"""Evidence-checked, incremental output from one user-facing Owl turn."""

import hashlib
import json
import re

from pydantic import Field, ValidationError

from ..llm.types import ToolCall
from .models import Input
from .reply_style import split_follow_ups
from .store import encode

NAME = "publish_reply_section"
MAX_SECTIONS = 8


class Section(Input):
    number: int = Field(ge=1, le=MAX_SECTIONS, strict=True)
    text: str = Field(min_length=1, max_length=5000)
    ready_to_finish: bool = Field(
        default=True,
        strict=True,
        description="True when evidence and tool work are finished; stream the remaining answer as text. False if more research is needed.",
    )


SCHEMA = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "For a substantial answer, deliver a finished, independently useful section as soon as "
            "its facts are established, while you continue composing the rest. Call this tool "
            "alone. Start at number 1. Each section is checked before the person sees it. "
            "Write in the person's language. Never send plans, status, drafts, reasoning, "
            "unsupported claims or repeated text. After the sections, answer with only the "
            "remaining conclusion. Set ready_to_finish false only when more tool work is necessary. "
            "For a short answer just reply normally."
        ),
        "parameters": Section.model_json_schema(),
    },
}


def _entries(db, task_id):
    return [
        json.loads(row[0])
        for row in db.execute(
            "SELECT data FROM events WHERE task_id=? AND kind='reply_section' AND seq>("
            "SELECT coalesce(max(seq),0) FROM events WHERE task_id=? AND kind IN "
            "('steer_applied','reply_sections_reset')) ORDER BY seq",
            (task_id, task_id),
        )
    ]


def _context_key(db, task):
    """Read the mutable inputs to this Owl's reply in one database snapshot.

    Only immutable IDs, revisions and change timestamps enter the digest. The content
    of private notes, answers and routines never enters the public event feed.
    """
    bot_id, task_id = task["bot_id"], task["id"]
    data = db.execute(
        "SELECT (SELECT coalesce(max(seq),0) FROM events WHERE task_id=? AND kind='steer_applied'), "
        "(SELECT coalesce(max(seq),0) FROM memory_changes WHERE bot_id=? AND (status='active' OR previous_status='active')), "
        "(SELECT count(*) FROM working_notes WHERE bot_id=? AND source_task_id IS NULL), "
        "(SELECT coalesce(sum(revision),0) FROM working_notes WHERE bot_id=? AND source_task_id IS NULL), "
        "(SELECT coalesce(max(a.seq),0) FROM note_consolidations a JOIN working_notes n ON n.id=a.source_note_id "
        "WHERE n.bot_id=? AND n.source_task_id IS NULL AND a.restored_at IS NULL), "
        "(SELECT coalesce(max(updated_at),0) FROM clarifications WHERE bot_id=?), "
        "(SELECT coalesce(max(updated_at),0) FROM schedules WHERE bot_id=?), "
        "(SELECT count(*) FROM schedules WHERE bot_id=?), "
        "(SELECT coalesce(max(updated_at),0) FROM routine_suggestions WHERE bot_id=?), "
        "(SELECT coalesce(max(updated_at),0) FROM bot_triggers WHERE bot_id=?), "
        "(SELECT count(*) FROM bot_triggers WHERE bot_id=?)",
        (task_id, *([bot_id] * 10)),
    ).fetchone()
    return hashlib.sha256(encode([data[i] for i in range(11)]).encode()).hexdigest()


def reset(store, task_id, *, expected=None):
    with store.connect(write=True) as db:
        current = _entries(db, task_id)
        if current and (expected is None or current == expected):
            store._event(db, task_id, "reply_sections_reset", {})


def context_at_start(store, task):
    """Withdraw earlier public sections when a correction arrives, including after a restart."""
    with store.connect() as db:
        key = _context_key(db, task)
        entries = _entries(db, task["id"])
    changed = bool(entries and entries[-1].get("context") != key)
    if changed:
        reset(store, task["id"], expected=entries)
    return key, changed, [] if changed else [entry["text"] for entry in entries]


def room_for_calls(runtime, task_id, ceiling, needed):
    return runtime.store.has_model_calls(
        runtime.owner, task_id, ceiling, runtime.limits.max_calls, needed
    )


def with_published(runtime, task_id, final):
    with runtime.store.connect() as db:
        sections = [item["text"] for item in _entries(db, task_id)]
    if not sections:
        return final
    # A model can repeat the opening verbatim when finishing. Keep it once.
    remaining = final.strip()
    for part in sections:
        if remaining.startswith(part):
            remaining = remaining[len(part) :].lstrip()
    return "\n\n".join([*sections, *([remaining] if remaining else [])])


def deliver(
    runtime,
    task,
    call,
    client,
    token,
    wire,
    tools,
    ceiling,
    context,
    image=None,
    *,
    during_final=False,
):
    try:
        section = Section.model_validate(call.arguments)
    except ValidationError:
        return {
            "published": False,
            "reason": "Supply one numbered section of 1 to 5000 characters.",
        }
    text = section.text.strip()
    if not text:
        return {"published": False, "reason": "The section is empty."}
    if split_follow_ups(text)[0] != text:
        return {
            "published": False,
            "reason": "Send follow-up suggestions only with the final answer.",
        }
    store, owner, task_id = runtime.store, runtime.owner, task["id"]
    with store.connect() as db:
        before = _entries(db, task_id)
        changed = _context_key(db, task) != context
    if changed:
        if before:
            reset(store, task_id, expected=before)
        return {"published": False, "reason": "The context changed. Read the latest request first."}
    if section.number <= len(before) and before[section.number - 1]["text"] == text:
        return {
            "published": True,
            "number": section.number,
            "ready_to_finish": section.ready_to_finish,
        }
    if section.number != len(before) + 1:
        return {"published": False, "reason": "The next section number is " + str(len(before) + 1)}
    if not room_for_calls(runtime, task_id, ceiling, 2 if during_final else 3):
        return {
            "published": False,
            "reason": "Finish the full answer now using the remaining calls.",
        }
    verdict = runtime.reply_review.check(
        task,
        client,
        token,
        wire,
        tools,
        text,
        ceiling,
        section=True,
        **({"connector_image": image} if image else {}),
    )
    if verdict.decision != "ready":
        return {"published": False, "reason": verdict.reason}
    token.throw_if_cancelled()
    with store.connect(write=True) as db:
        latest = store._task(db, owner, task_id)
        correction = db.execute(
            "SELECT 1 FROM queued_messages WHERE steer_target=? AND steer_state='pending' LIMIT 1",
            (task_id,),
        ).fetchone()
        changed = _context_key(db, task) != context
        current = _entries(db, task_id)
        if current and (correction or (changed and current[-1].get("context") == context)):
            store._event(db, task_id, "reply_sections_reset", {})
        if latest["status"] != "running" or correction or changed or current != before:
            return {
                "published": False,
                "reason": "The turn changed. Read the latest request first.",
            }
        store._event(
            db,
            task_id,
            "reply_section",
            {"number": section.number, "text": text, "context": context},
        )
    return {
        "published": True,
        "number": section.number,
        "ready_to_finish": section.ready_to_finish,
        "next": "Continue the answer without repeating this section.",
    }


class CheckedDeltas:
    """In a tool-free final round, release complete paragraphs as their actual model deltas arrive.

    No draft fragments or unfinished paragraphs leave the process. Each public checkpoint is an
    independent checked publication; the remainder still goes through the normal final check.
    """

    def __init__(self, runtime, task, token, wire, tools, ceiling, context, image):
        self.runtime, self.task, self.token = runtime, task, token
        self.wire, self.tools, self.ceiling, self.image = wire, tools, ceiling, image
        self.context = context
        self.buffer = ""
        self.stopped = False
        with runtime.store.connect() as db:
            self.number = len(_entries(db, task["id"])) + 1

    def add(self, delta):
        if self.stopped or not delta:
            return
        self.buffer += delta
        if len(self.buffer) > 10000:
            self.stopped = True
            return
        while boundary := re.search(r"\r?\n[ \t]*\r?\n", self.buffer):
            section = self.buffer[: boundary.start()].strip()
            self.buffer = self.buffer[boundary.end() :]
            if not section:
                continue
            # A fenced block, Markdown heading, table or list can span blank lines. Leave
            # structured output to the complete final response, preserving its exact layout.
            if (
                len(section) > 5000
                or self.number > MAX_SECTIONS
                or any(
                    line.lstrip().startswith(("#", "`", "~", "|", ">", "* ", "- "))
                    for line in section.splitlines()
                )
            ):
                self.stopped = True
                return
            try:
                # A second transport can check the paragraph while the writing connection streams.
                checker, _ = self.runtime.turn_model(self.task["id"], self.task["bot_id"])
                outcome = deliver(
                    self.runtime,
                    self.task,
                    ToolCall("stream", NAME, {"number": self.number, "text": section}),
                    checker,
                    self.token,
                    self.wire,
                    self.tools,
                    self.ceiling,
                    self.context,
                    self.image,
                    during_final=True,
                )
            except Exception:
                self.token.throw_if_cancelled()
                self.stopped = True
                return
            if not outcome["published"]:
                self.stopped = True
                return
            self.number += 1


def receipt(call, result):
    return {"role": "tool", "tool_call_id": call.id, "content": encode(result)}
