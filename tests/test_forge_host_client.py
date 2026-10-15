from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from alysis_code.forge_host_client import ForgeHostInProcessClient
from alysis_code.forge_host_protocol.codec import validate_envelope
from alysis_code.forge_host_protocol.models import (
    EventEnvelope,
    Identity,
    PlannerAnswer,
    PlannerGoal,
    PlannerInput,
    PlannerQuestion,
    RequestEnvelope,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parents[1] / "schemas" / "forge-host-v2" / "fixtures" / "valid"


def _fixture_event(name: str) -> EventEnvelope:
    value = validate_envelope(json.loads((FIXTURES / name).read_text(encoding="utf-8")))
    assert isinstance(value, EventEnvelope)
    return value


class _PlannerService:
    def __init__(self) -> None:
        self._identity = Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        )
        self.calls: list[RequestEnvelope] = []

    @property
    def identity(self) -> Identity:
        return self._identity

    def handle_request(self, envelope: RequestEnvelope):  # type: ignore[no-untyped-def]
        self.calls.append(envelope)
        self._identity = self._identity.model_copy(update={"revision": self._identity.revision + 1})
        if len(self.calls) == 1:
            event = EventEnvelope(
                protocol="alysis.forge-host",
                protocol_version="2.0",
                message_id="msg_question001",
                operation_id=envelope.operation_id,
                sent_at=NOW,
                kind="event",
                sequence=1,
                identity=self._identity,
                payload=PlannerQuestion(
                    type="planner.question",
                    question_id="question_abcdefgh",
                    prompt="Which file should Forge change?",
                    required=True,
                ),
            )
            return SimpleNamespace(events=(event,))
        return SimpleNamespace(
            events=(
                _fixture_event("plan-ready-event.json"),
                _fixture_event("approval-required-event.json"),
            )
        )


def test_in_process_client_owns_request_mechanics_but_no_planner_decisions() -> None:
    service = _PlannerService()
    counter = 0

    def next_id(prefix: str) -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}_value{counter:08d}"

    questions: list[PlannerQuestion] = []
    client = ForgeHostInProcessClient(
        service,
        clock=lambda: NOW,
        id_factory=next_id,
    )
    planned = client.plan_until_ready(
        "Build the scoped feature.",
        answer_provider=lambda question: questions.append(question) or "src/feature.py",
    )

    assert len(service.calls) == 2
    first = service.calls[0].payload
    second = service.calls[1].payload
    assert isinstance(first, PlannerInput)
    assert isinstance(first.input, PlannerGoal)
    assert first.input.text == "Build the scoped feature."
    assert isinstance(second, PlannerInput)
    assert isinstance(second.input, PlannerAnswer)
    assert second.input.question_id == "question_abcdefgh"
    assert second.input.text == "src/feature.py"
    assert [question.prompt for question in questions] == ["Which file should Forge change?"]
    assert planned.plan_ready.type == "plan.ready"
    assert planned.approval_event.payload.type == "approval.required"
    assert len(planned.commands) == 2


def test_in_process_client_rejects_empty_clarification_without_new_request() -> None:
    service = _PlannerService()
    client = ForgeHostInProcessClient(service, clock=lambda: NOW)

    try:
        client.plan_until_ready("Build it.", answer_provider=lambda _question: "   ")
    except ValueError as exc:
        assert "requires an answer" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an empty planner answer was accepted")
    assert len(service.calls) == 1
