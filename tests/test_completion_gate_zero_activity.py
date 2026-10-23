"""Zero-tool-call turns must not lose their answers to the completion gate.

A turn that used no tools at all has produced no work the gate could verify.
Its reply is an answer or a refusal, and it must be displayed - never replaced
by a change summary for a change that did not happen. Jurisdiction comes from
observed tool facts; the only judgment about *text* (does the reply claim
completed work?) is delegated to a model call so it works in every language,
never to English pattern matching.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.session_store import read_session_events

# An explicit out-of-workspace output that the model refuses without tools.
# The earlier "ls ... and paste the output" fixture only created a hard artifact
# obligation because a later output noun incorrectly captured the earlier path.
# Keep exercising the disposition gate with a real missing-output obligation.
INSTRUCTION = "write a report to /home/nowhere/report.txt and paste the output"

ANSWER = (
    "I can't write /home/nowhere/report.txt: it is outside this workspace. "
    "Bind it as a workspace first and I'll inspect it."
)
RESTATEMENT = (
    "Workspace unchanged. Current implementation checks URLs sequentially. npm test passes."
)

# Must match the system prompt of the zero-activity disposition check.
DISPOSITION_MARKER = "classify one assistant reply"


class _ZeroActivityClient:
    """No tool calls ever; scripted replies plus a scripted disposition verdict."""

    model = "test-model"
    temperature = 0.2

    def __init__(
        self,
        *,
        disposition_reply: str = "REFUSAL",
        disposition_error: Exception | None = None,
    ) -> None:
        self.agent_calls = 0
        self.disposition_calls = 0
        self.disposition_reply = disposition_reply
        self.disposition_error = disposition_error

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = tools, stream, on_text_delta, temperature
        joined = "\n".join(
            str(message.get("content") or "") for message in messages if isinstance(message, dict)
        )
        if DISPOSITION_MARKER in joined:
            self.disposition_calls += 1
            if self.disposition_error is not None:
                raise self.disposition_error
            return LLMResponse(content=self.disposition_reply, tool_calls=[], raw={})
        self.agent_calls += 1
        content = ANSWER if self.agent_calls == 1 else RESTATEMENT
        return LLMResponse(content=content, tool_calls=[], raw={})


def _session(tmp_path: Path) -> Any:
    (tmp_path / "package.json").write_text(
        '{"name":"t","version":"1.0.0","scripts":{"test":"node test.js"}}'
    )
    (tmp_path / "checkUrl.js").write_text(
        "const DEFAULT_TIMEOUT_MS = 10000;\n"
        "module.exports = { checkUrl: (u, t = DEFAULT_TIMEOUT_MS) => t };\n"
    )
    cfg = AppConfig(model="test-model")
    return create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
        enable_chat_turn_step_budget=True,
    )


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def _run(session: Any, client: Any) -> Path:
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn(INSTRUCTION)
        log_path = session.store.path
    finally:
        session.close()
    assert exit_code == 0
    return log_path


def test_zero_activity_refusal_survives_completion_gate(tmp_path: Path, monkeypatch) -> None:
    """The displaced-answer regression: the refusal must be the displayed final answer."""
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_GATE_DOWNGRADE", raising=False)
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_DISPOSITION_CHECK", raising=False)
    client = _ZeroActivityClient(disposition_reply="REFUSAL")
    log_path = _run(_session(tmp_path), client)

    finals = _event_payloads(log_path, "final")
    assert len(finals) == 1
    assert ANSWER in str(finals[0]["content"])
    assert RESTATEMENT not in str(finals[0]["content"])
    # The gate never entered a repair round: one agent call, no nudges.
    assert client.agent_calls == 1
    assert _event_payloads(log_path, "completion_gate_nudge") == []
    dispositions = _event_payloads(log_path, "zero_activity_disposition")
    assert len(dispositions) == 1
    assert dispositions[0]["disposition"] == "refusal"
    assert dispositions[0]["downgraded"] is True


def test_interactive_reply_does_not_scan_git_for_an_unused_one_shot_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from alysis_code.agent.turn import core

    calls: list[Path] = []
    original = core.inspect_workspace_git_diff

    def inspect(root: Path, **kwargs: Any) -> Any:
        calls.append(root)
        return original(root, **kwargs)

    monkeypatch.setattr(core, "inspect_workspace_git_diff", inspect)
    client = _ZeroActivityClient(disposition_reply="REFUSAL")
    log_path = _run(_session(tmp_path), client)
    assert calls == []
    assert _event_payloads(log_path, "final")
    assert client.disposition_calls == 1, "reply verification remains enabled"


def test_zero_activity_work_claim_stays_gated(tmp_path: Path, monkeypatch) -> None:
    """A zero-tool reply that claims completed work keeps execute-gate scrutiny."""
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_GATE_DOWNGRADE", raising=False)
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_DISPOSITION_CHECK", raising=False)
    client = _ZeroActivityClient(disposition_reply="WORK_CLAIM")
    log_path = _run(_session(tmp_path), client)

    dispositions = _event_payloads(log_path, "zero_activity_disposition")
    assert dispositions and dispositions[0]["disposition"] == "work_claim"
    assert dispositions[0]["downgraded"] is False
    # The gate proceeded to fail the candidate and nudge for repair.
    assert _event_payloads(log_path, "interactive_completion_gate_failed")
    assert client.agent_calls >= 2
    # Append-only invariant: the displaced first reply is preserved in the final.
    finals = _event_payloads(log_path, "final")
    assert len(finals) == 1
    assert ANSWER in str(finals[0]["content"])


def test_disposition_check_disabled_downgrades_without_llm_call(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_GATE_DOWNGRADE", raising=False)
    monkeypatch.setenv("ALYSIS_ZERO_ACTIVITY_DISPOSITION_CHECK", "off")
    client = _ZeroActivityClient()
    log_path = _run(_session(tmp_path), client)

    assert client.disposition_calls == 0
    finals = _event_payloads(log_path, "final")
    assert ANSWER in str(finals[0]["content"])
    dispositions = _event_payloads(log_path, "zero_activity_disposition")
    assert dispositions and dispositions[0]["source"] == "check_disabled"
    assert dispositions[0]["downgraded"] is True


def test_downgrade_kill_switch_restores_legacy_gating(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_ZERO_ACTIVITY_GATE_DOWNGRADE", "off")
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_DISPOSITION_CHECK", raising=False)
    client = _ZeroActivityClient()
    log_path = _run(_session(tmp_path), client)

    assert client.disposition_calls == 0
    assert _event_payloads(log_path, "zero_activity_disposition") == []
    assert _event_payloads(log_path, "interactive_completion_gate_failed")


def test_disposition_check_failure_fails_open(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_GATE_DOWNGRADE", raising=False)
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_DISPOSITION_CHECK", raising=False)
    client = _ZeroActivityClient(disposition_error=RuntimeError("provider down"))
    log_path = _run(_session(tmp_path), client)

    finals = _event_payloads(log_path, "final")
    assert ANSWER in str(finals[0]["content"])
    dispositions = _event_payloads(log_path, "zero_activity_disposition")
    assert dispositions and dispositions[0]["source"] == "fail_open_error"
    assert dispositions[0]["downgraded"] is True


def test_unparseable_disposition_fails_open(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_GATE_DOWNGRADE", raising=False)
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_DISPOSITION_CHECK", raising=False)
    client = _ZeroActivityClient(disposition_reply="cannot tell, sorry")
    log_path = _run(_session(tmp_path), client)

    finals = _event_payloads(log_path, "final")
    assert ANSWER in str(finals[0]["content"])
    dispositions = _event_payloads(log_path, "zero_activity_disposition")
    assert dispositions and dispositions[0]["source"] == "fail_open_unparsed"
