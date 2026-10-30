"""Host-owned approval evidence, independent of model history compaction.

Whole user instructions are retained. Visible assistant text and tool observations
are evidence, never authority. If instructions cannot fit, automatic review stops
instead of silently removing a restriction from an otherwise valid grant.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from ..ide.protocol import redact_secrets
from ..logging_redaction import redact_log_text

MAX_INSTRUCTION_CHARS = 96_000
MAX_EVIDENCE_CHARS = 24_000
MAX_EVIDENCE_ITEM_CHARS = 6_000


class ReviewContextError(ValueError):
    pass


def safe_text(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return redact_log_text(str(redact_secrets(text)))


def message_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("display_content", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            return "\n".join(
                str(part.get("text") or "")
                for part in value
                if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
            ).strip()
    return ""


class ApprovalContext:
    def __init__(self) -> None:
        self.cursor = 0
        self.user_revision = 0
        self.instructions: list[dict[str, Any]] = []
        self.instruction_chars = 0
        self.instructions_complete = True
        self.evidence: deque[dict[str, Any]] = deque()
        self.evidence_chars = 0
        self.evidence_omitted = False
        self.last_assistant: dict[str, Any] | None = None

    def refresh(self, session: Any) -> bool:
        events_since = getattr(getattr(session, "store", None), "events_since", None)
        if not callable(events_since):
            raise ReviewContextError("The conversation evidence is unavailable.")
        events, cursor = events_since(self.cursor)
        revision = self.user_revision
        for index, event in enumerate(events, self.cursor):
            kind = event.get("type")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            source = event.get("event_id") or str(index)
            if kind in {"user_message", "user_question_response"}:
                text = (
                    message_text(payload)
                    if kind == "user_message"
                    else safe_text(
                        {
                            key: payload[key]
                            for key in ("request_id", "status", "answers")
                            if key in payload
                        }
                    )
                )
                if not text:
                    continue
                self.user_revision += 1
                instruction = {"source": source, "order": index, "text": safe_text(text)}
                if kind == "user_question_response" and payload.get("question_evidence"):
                    instruction["question_evidence"] = safe_text(payload["question_evidence"])
                # Preserve the question that gives meaning to an ordinary "yes".
                # It stays explicitly untrusted, and never grants access by itself.
                if self.last_assistant is not None:
                    instruction["preceding_assistant_evidence"] = self.last_assistant
                size = len(json.dumps(instruction, ensure_ascii=False))
                if (
                    self.instructions_complete
                    and self.instruction_chars + size <= MAX_INSTRUCTION_CHARS
                ):
                    self.instructions.append(instruction)
                    self.instruction_chars += size
                else:
                    self.instructions_complete = False
            elif kind in {"assistant_message", "tool_call", "tool_result", "approval_review"}:
                if kind == "assistant_message":
                    # Only surfaced text; never hidden reasoning or provider metadata.
                    text = safe_text(message_text(payload))
                elif kind == "tool_call":
                    text = safe_text(
                        {
                            k: payload[k]
                            for k in ("name", "arguments", "tool_call_id")
                            if k in payload
                        }
                    )
                elif kind == "tool_result":
                    text = safe_text(
                        {k: payload[k] for k in ("name", "result", "tool_call_id") if k in payload}
                    )
                else:
                    text = safe_text(payload)
                if not text:
                    continue
                entry = {"source": source, "order": index, "kind": kind, "text": text}
                if len(text) > MAX_EVIDENCE_ITEM_CHARS:
                    # Never turn a partial script or question into apparent full evidence.
                    entry["text"] = "[Whole observation omitted: exceeds the evidence budget.]"
                    entry["omitted"] = True
                    self.evidence_omitted = True
                if kind == "assistant_message":
                    self.last_assistant = entry
                size = len(json.dumps(entry, ensure_ascii=False))
                self.evidence.append(entry)
                self.evidence_chars += size
                while self.evidence_chars > MAX_EVIDENCE_CHARS:
                    self.evidence_chars -= len(
                        json.dumps(self.evidence.popleft(), ensure_ascii=False)
                    )
                    self.evidence_omitted = True
        self.cursor = cursor
        return self.user_revision != revision

    def payload(self) -> dict[str, Any]:
        if not self.instructions_complete:
            raise ReviewContextError(
                "The complete user instructions exceed the review budget; a person must review this action."
            )
        return {
            "user_instructions": self.instructions,
            "untrusted_evidence": list(self.evidence),
            "evidence_omitted": self.evidence_omitted,
        }
