from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_computer_use_recovery import _ScriptedComputerUseClient

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


class _Surface(NoopSurface):
    computer_use_evidence_version = 1

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.evidence: list[dict[str, Any]] = []

    def emit_message_end(self, text: str = "", **_kwargs: Any) -> None:
        self.messages.append(text)

    def bind_computer_use_evidence_emitter(self):
        return self.evidence.append


def _turn(
    tmp_path: Path, text: str, mode: str = "host_sanitized_text", *, observe=True, computer_use=True
):
    surface = _Surface()
    calls: list[str] = []

    def host(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {"snapshot_id": "fixture-1", "source": "chrome-dom", "elements": []}

    responses = []
    if observe:
        responses.append(
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-1", name="computer_state", arguments={})],
                raw={},
            )
        )
    responses.append(LLMResponse(content=text, tool_calls=[], raw={}))
    if not observe:
        responses *= 4  # Retain the production bounded forced-observation attempts.
    client = _ScriptedComputerUseClient(responses)
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="fixture-key",
        surface=surface,
        sensitive_media=True,
        verification_enabled=False,
        computer_use_host_request=host if computer_use else None,
        computer_use_response_mode=mode,
    )
    session.client = client  # type: ignore[assignment]
    try:
        code = session.run_turn("Read the page and report what remains.")
        return (
            code,
            surface,
            calls,
            next((item for item in surface.evidence if item.get("kind") == "terminal"), {}),
            dict(session.last_turn_outcome),
        )
    finally:
        session.close()


@pytest.mark.parametrize(
    "text",
    [
        '{"outcome":"completed","summary":"UNDECLARED_REPORT"}',
        '{"outcome":"completed_unverified","summary":"UNDECLARED_REPORT","extra":1}',
        '{"outcome":"blocked","summary":"UNDECLARED_REPORT","summary":"duplicate"}',
        '{"outcome":"blocked","summary":"UNDECLARED_REPORT"',
        '```json\n{"outcome":"blocked","summary":"UNDECLARED_REPORT"}\n',
        '```python\n{"outcome":"blocked","summary":"UNDECLARED_REPORT"}\n```',
        '[{"outcome":"blocked","summary":"UNDECLARED_REPORT"}]',
        '\u00a0{"outcome":"blocked","summary":"UNDECLARED_REPORT"}',
    ],
    ids=[
        "old-outcome",
        "extra-key",
        "duplicate",
        "torn",
        "torn-fence",
        "language",
        "array",
        "non-json-space",
    ],
)
def test_malformed_report_attempt_shows_host_status_without_raw_report(tmp_path, text):
    code, surface, calls, evidence, outcome = _turn(tmp_path, text)
    assert code == 0 and calls == ["computer_state"]
    assert surface.messages == ["Computer Use ended without a completed goal report."]
    assert "UNDECLARED_REPORT" not in json.dumps(surface.evidence)
    assert outcome["outcome"] != "verified_success"


@pytest.mark.parametrize(
    "text",
    [
        "Only the first field was read; the second still needs checking.",
        'The page contains {"outcome":"blocked"}; that is page text.',
        "The page shows inline ```example``` text.",
    ],
)
def test_ordinary_prose_keeps_existing_transient_display_without_certifying(tmp_path, text):
    code, surface, calls, evidence, outcome = _turn(tmp_path, text)
    assert code == 0 and calls == ["computer_state"]
    assert surface.messages == [text]
    assert outcome["outcome"] != "verified_success"


@pytest.mark.parametrize("fenced", [False, True])
@pytest.mark.parametrize("outcome", ["completed_unverified", "incomplete", "blocked"])
def test_valid_report_displays_summary_and_preserves_reported_outcome(tmp_path, outcome, fenced):
    summary = "Read the first field; no independent completion certificate."
    text = json.dumps({"outcome": outcome, "summary": summary})
    if fenced:
        text = f"```json\n{text}\n```"
    code, surface, calls, evidence, final = _turn(tmp_path, text)
    assert code == 0 and calls == ["computer_state"]
    assert surface.messages == [summary]
    assert (
        evidence["reportedOutcome"]
        == {"completed_unverified": "completed", "incomplete": "failed", "blocked": "blocked"}[
            outcome
        ]
    )
    assert final["outcome"] != "verified_success"


@pytest.mark.parametrize(
    "text",
    [
        "Ordinary model prose.",
        '{"outcome":"completed_unverified","summary":"A model assessment."}',
        '{"outcome":"completed","summary":"UNDECLARED_REPORT"}',
    ],
)
def test_status_only_keeps_all_model_text_out_of_display(tmp_path, text):
    code, surface, calls, _evidence, _outcome = _turn(tmp_path, text, "status_only")
    assert code == 0 and calls == ["computer_state"]
    assert surface.messages
    assert all(
        text not in message and "UNDECLARED_REPORT" not in message for message in surface.messages
    )


def test_report_without_a_trusted_host_call_cannot_bypass_existing_rejection(tmp_path):
    text = '{"outcome":"completed_unverified","summary":"A model assessment."}'
    code, surface, calls, _evidence, outcome = _turn(tmp_path, text, observe=False)
    assert code == 1 and calls == []
    assert surface.messages and all(
        "A model assessment." not in message for message in surface.messages
    )
    assert outcome["outcome"] != "verified_success"


@pytest.mark.parametrize(
    "text",
    [
        "Ordinary chat text.",
        '{"outcome":"completed","summary":"This is ordinary chat JSON."}',
        '```json\n{"example": 1}\n```',
    ],
)
def test_other_chat_turns_keep_their_original_text(tmp_path, text):
    code, surface, calls, _evidence, _outcome = _turn(
        tmp_path, text, observe=False, computer_use=False
    )
    assert code == 0 and calls == []
    assert surface.messages == [text]
