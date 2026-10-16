"""Provider-independent, thread-safe handoff between question tools and a UI.

The worker owns conversation history. This broker never edits messages, changes
permissions, or treats a highlighted option as an answer.
"""

from __future__ import annotations

import copy
import math
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

MAX_ANSWER_CHARS = 4000
QUESTION_TIMEOUT_SECONDS = 60
QUESTION_CANCELLATION_TOKEN = object()

QUESTION_PARAMETERS = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Unique short question identifier."},
                    "question": {
                        "type": "string",
                        "description": "One self-contained question and the decision it affects.",
                    },
                    "kind": {"type": "string", "enum": ["single", "multiple", "text"]},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Prefer 2-3 distinct choices with short tradeoffs (maximum 8). "
                            "Put the recommended choice first, marked '(Recommended)'. "
                            "Omit Other; custom text is built in. Use an empty list for text questions."
                        ),
                    },
                },
                "required": ["id", "question", "kind", "options"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["questions"],
    "additionalProperties": False,
}

WAIT_PARAMETERS = {
    "type": "object",
    "properties": {"request_id": {"type": "string"}},
    "required": ["request_id"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class UserQuestion:
    id: str
    question: str
    kind: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class QuestionRequest:
    id: str
    questions: tuple[UserQuestion, ...]


def _question_key(question: UserQuestion) -> tuple[str, str, tuple[str, ...]]:
    def normalize(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text).casefold()
        return re.sub(r"\s+", " ", normalized).strip().rstrip("?!.")

    return (
        normalize(question.question),
        question.kind,
        tuple(sorted(normalize(option) for option in question.options)),
    )


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be non-empty text of at most {limit} characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"{field} contains a control character")
    return value.strip()


def parse_questions(arguments: dict[str, Any]) -> tuple[UserQuestion, ...]:
    raw = arguments.get("questions")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
        raise ValueError("Provide between 1 and 3 questions")
    questions = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each question must be an object")
        identifier = _text(item.get("id"), "id", 64)
        question = _text(item.get("question"), "question", 1000)
        kind = item.get("kind")
        options = item.get("options")
        if kind not in ("single", "multiple", "text"):
            raise ValueError("kind must be single, multiple, or text")
        if not isinstance(options, list):
            raise ValueError("options must be an array")
        if (kind == "text" and options) or (kind != "text" and not 2 <= len(options) <= 8):
            raise ValueError("Use 2-8 options for choices and an empty array for text")
        labels = tuple(_text(option, "option", 200) for option in options)
        if len(set(labels)) != len(labels):
            raise ValueError("Option labels must be unique")
        questions.append(UserQuestion(identifier, question, kind, labels))
    if len({question.id for question in questions}) != len(questions):
        raise ValueError("Question ids must be unique")
    if len({_question_key(question) for question in questions}) != len(questions):
        raise ValueError("Ask each decision only once in a question batch")
    return tuple(questions)


def _normalize_answer(question: UserQuestion, answer: Any) -> dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("id") != question.id:
        raise ValueError("Answer ids must match the questions in order")
    selected = answer.get("selected", [])
    text = answer.get("text", "")
    if not isinstance(text, str) or len(text) > MAX_ANSWER_CHARS:
        raise ValueError(f"Answers must be at most {MAX_ANSWER_CHARS} characters")
    if not isinstance(selected, list) or any(
        not isinstance(item, str) or item not in question.options for item in selected
    ):
        raise ValueError("Selected options must come from the question")
    if len(selected) != len(set(selected)):
        raise ValueError("Selected options must be unique")
    if question.kind == "single" and len(selected) > 1:
        raise ValueError("Choose one option")
    if question.kind == "text" and selected:
        raise ValueError("Text questions cannot have selected options")
    if not selected and not text.strip():
        raise ValueError("Select an option or write an answer")
    return {"id": question.id, "selected": selected[:], "text": text.strip()}


class UserQuestionBroker:
    """One outstanding batch, with answers consumed once by the owning worker."""

    def __init__(
        self,
        *,
        on_change: Callable[[], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        timeout_seconds: float = QUESTION_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_timeout: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Question timeout must be a positive finite number")
        self._condition = threading.Condition()
        self._request: QuestionRequest | None = None
        self._result: dict[str, Any] | None = None
        self._delivered = False
        self._history: list[dict[str, Any]] = []
        self._on_change = on_change or (lambda: None)
        self._is_cancelled = is_cancelled or (lambda: False)
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._deadline = 0.0
        self._confirmed: dict[str, dict[str, Any]] = {}
        self._on_timeout = on_timeout or (lambda _: None)
        self._asked: dict[tuple[str, str, tuple[str, ...]], str] = {}

    def note_user_input(self) -> None:
        """New user direction may legitimately revisit a decision; keep any active request."""
        with self._condition:
            self._asked.clear()

    def _expire_locked(self) -> None:
        if self._request is None or self._result is not None or self._clock() < self._deadline:
            return
        self._result = {
            "request_id": self._request.id,
            "status": "timed_out",
            "answers": [
                copy.deepcopy(self._confirmed[q.id])
                for q in self._request.questions
                if q.id in self._confirmed
            ],
            "skipped_question_ids": [
                q.id for q in self._request.questions if q.id not in self._confirmed
            ],
            "timeout_seconds": self.timeout_seconds,
            "guidance": (
                "The user stopped responding. Use any confirmed answers and continue the task "
                "with your best judgment for the skipped questions; state material assumptions. "
                "Do not repeat the same questions or wait again. No skipped choice was selected, "
                "and a timeout never grants permission."
            ),
        }
        self._condition.notify_all()

    @property
    def remaining_seconds(self) -> int:
        with self._condition:
            self._expire_locked()
            if self._request is None or self._result is not None:
                return 0
            return max(0, math.ceil(self._deadline - self._clock()))

    def touch(self, request_id: str) -> bool:
        """Give an actively answering user another idle interval; never revive expiry."""
        with self._condition:
            self._expire_locked()
            if self._request is None or self._request.id != request_id or self._result is not None:
                return False
            self._deadline = self._clock() + self.timeout_seconds
            return True

    def confirm(self, request_id: str, answer: dict[str, Any]) -> bool:
        """Keep only an explicitly confirmed answer if the rest of a batch times out."""
        with self._condition:
            self._expire_locked()
            if self._request is None or self._request.id != request_id or self._result is not None:
                return False
            question = next((q for q in self._request.questions if q.id == answer.get("id")), None)
            if question is None:
                raise ValueError("Answer id must match a question")
            self._confirmed[question.id] = _normalize_answer(question, answer)
            self._deadline = self._clock() + self.timeout_seconds
            return True

    @property
    def active(self) -> QuestionRequest | None:
        with self._condition:
            self._expire_locked()
            return self._request if self._result is None else None

    @property
    def outstanding(self) -> bool:
        with self._condition:
            return self._request is not None and not self._delivered

    def request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            questions = parse_questions(arguments)
        except ValueError as exc:
            return {"error": str(exc), "status": "invalid"}
        with self._condition:
            if self._is_cancelled():
                return {"status": "cancelled"}
            if self._request is not None and not self._delivered:
                return {
                    "error": "Collect the existing request before asking more questions",
                    "request_id": self._request.id,
                }
            repeated = [
                question for question in questions if _question_key(question) in self._asked
            ]
            if repeated:
                return {
                    "status": "duplicate_questions",
                    "question_ids": [question.id for question in repeated],
                    "previous_request_ids": list(
                        dict.fromkeys(self._asked[_question_key(question)] for question in repeated)
                    ),
                    "guidance": (
                        "These questions were already presented since the last user message. "
                        "Use their previous answers or stated assumptions after dismissal/timeout. "
                        "Do not reword the same decision to reopen it. Request only genuinely "
                        "new consequential decisions, or continue the task."
                    ),
                }
            previous = self.snapshot()
            if previous is not None:
                self._history = [*self._history[-49:], previous]
            self._request = QuestionRequest(uuid4().hex, questions)
            self._result = None
            self._delivered = False
            self._confirmed = {}
            self._deadline = self._clock() + self.timeout_seconds
            identifier = self._request.id
            self._asked.update({_question_key(question): identifier for question in questions})
        self._on_change()
        return {
            "request_id": identifier,
            "status": "pending",
            "timeout_seconds": self.timeout_seconds,
            "guidance": (
                "Continue independent work while the user answers. Only call wait_user_input "
                "when no independent work remains or you need the answers. The request expires "
                f"after {self.timeout_seconds:g} seconds without question-panel interaction. "
                "On timed_out, use confirmed answers, state material assumptions for skipped "
                "questions, and keep going without repeating the questions."
            ),
        }

    def submit(self, request_id: str, answers: list[dict[str, Any]]) -> bool:
        with self._condition:
            self._expire_locked()
            request = self._request
            if request is None or request.id != request_id or self._result is not None:
                return False
            if not isinstance(answers, list) or len(answers) != len(request.questions):
                raise ValueError("Answer every question before submitting")
            normalized = [
                _normalize_answer(question, answer)
                for question, answer in zip(request.questions, answers, strict=True)
            ]
            self._result = {"request_id": request_id, "status": "answered", "answers": normalized}
            self._condition.notify_all()
        self._on_change()
        return True

    def dismiss(self, request_id: str) -> bool:
        with self._condition:
            self._expire_locked()
            if self._request is None or self._request.id != request_id or self._result is not None:
                return False
            self._result = {"request_id": request_id, "status": "dismissed", "answers": []}
            self._condition.notify_all()
        self._on_change()
        return True

    def collect(
        self, request_id: str, *, wait: bool = True, cancellation_token: Any = None
    ) -> dict[str, Any]:
        with self._condition:
            while True:
                if self._request is None or self._request.id != request_id:
                    return {"error": "Unknown or expired question request", "status": "unavailable"}
                if self._delivered:
                    return {"request_id": request_id, "status": "already_delivered"}
                if self._is_cancelled() or bool(getattr(cancellation_token, "is_cancelled", False)):
                    self._result = {"request_id": request_id, "status": "cancelled", "answers": []}
                self._expire_locked()
                if self._result is not None:
                    self._delivered = True
                    result = copy.deepcopy(self._result)
                    break
                if not wait:
                    return {"request_id": request_id, "status": "pending"}
                self._condition.wait(timeout=max(0, min(0.1, self._deadline - self._clock())))
        if result["status"] == "timed_out":
            self._on_timeout(copy.deepcopy(result))
        return result

    def collect_outstanding(self, *, cancellation_token: Any = None) -> dict[str, Any] | None:
        with self._condition:
            identifier = self._request.id if self._request and not self._delivered else None
        return (
            self.collect(identifier, cancellation_token=cancellation_token)
            if identifier is not None
            else None
        )

    def reset(self) -> None:
        """Expire turn-owned requests and wake waiters on interruption or exit."""
        with self._condition:
            self._request = None
            self._result = None
            self._delivered = False
            self._confirmed = {}
            self._deadline = 0.0
            self._asked.clear()
            self._history.clear()
            self._condition.notify_all()
        self._on_change()

    def snapshots(self, *, finished: bool = False) -> list[dict[str, Any]]:
        """Retain completed batches until the host has seen them, bounded per turn."""
        with self._condition:
            current = self.snapshot(finished=finished)
            return copy.deepcopy(self._history) + ([current] if current else [])

    def snapshot(self, *, finished: bool = False) -> dict[str, Any] | None:
        """Read host presentation without consuming the worker's answer."""
        with self._condition:
            self._expire_locked()
            if self._request is None:
                return None
            if self._result is None and (finished or self._is_cancelled()):
                self._result = {
                    "request_id": self._request.id,
                    "status": "cancelled",
                    "answers": [],
                }
                self._condition.notify_all()
            result = (
                copy.deepcopy(self._result)
                if self._result
                else {"request_id": self._request.id, "status": "pending", "answers": []}
            )
            result["questions"] = [
                {"id": q.id, "question": q.question, "kind": q.kind, "options": list(q.options)}
                for q in self._request.questions
            ]
            result["remaining_seconds"] = self.remaining_seconds
            return result


def question_broker(surface: Any) -> UserQuestionBroker | None:
    broker = getattr(surface, "user_questions", None)
    return broker if isinstance(broker, UserQuestionBroker) else None
