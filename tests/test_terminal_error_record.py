"""The single terminal-failure boundary on ``AgentSession.run_turn``.

Locks the observability-spine contract: when a turn dies, one authoritative,
redacted, joinable ``terminal_error`` record is written to the default-on per-run
store AND (when enabled) to the opt-in crash-diagnostic log, so an autonomous
fix-loop can reconstruct *why* a build failed from artifacts alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from alysis_code.agent.session import AgentSession
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.crash_diagnostics import build_crash_diagnostic_logger
from alysis_code.failure_category import FailureCategory
from alysis_code.llm.openai_compat import LLMError
from alysis_code.session_store import read_session_events


class _FakeStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def append(self, event_type: str, payload: dict[str, object]) -> None:
        self.events.append((event_type, dict(payload)))


def test_emit_terminal_error_writes_redacted_joinable_record(tmp_path: Path) -> None:
    diag_path = tmp_path / "diag.jsonl"
    logger = build_crash_diagnostic_logger(path=diag_path, run_id="r", session_id="s")
    store = _FakeStore()
    session = SimpleNamespace(store=store, crash_diagnostics=logger)
    error = LLMError("Incorrect API key provided: sk-ABCDEFGHIJKLMNOP1234")
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")
    error.__cause__ = httpx.HTTPStatusError(
        "Request rejected", request=request, response=httpx.Response(401, request=request)
    )

    # Unbound call with a duck-typed self keeps the test off the heavy session factory.
    AgentSession._emit_terminal_error(session, error)

    # Default-on store record (suppressed only by --no-log).
    assert store.events, "terminal_error must be recorded to the per-run store by default"
    event_type, payload = store.events[-1]
    assert event_type == "terminal_error"
    assert payload["failure_category"] == "provider_error"
    assert payload["provider_status_code"] == 401
    assert payload["operation"] == "run_turn"
    assert "sk-ABCDEFGHIJKLMNOP1234" not in payload["error_summary"]

    # Opt-in crash-diagnostic durable event carries the same redacted, joinable block.
    diag_event = json.loads(diag_path.read_text(encoding="utf-8").splitlines()[-1])
    assert diag_event["event_type"] == "terminal_error"
    assert diag_event["payload"]["failure_category"] == "provider_error"
    assert "sk-ABCDEFGHIJKLMNOP1234" not in json.dumps(diag_event)


def test_emit_terminal_error_without_crash_log_still_records_to_store(tmp_path: Path) -> None:
    store = _FakeStore()
    session = SimpleNamespace(store=store, crash_diagnostics=None)

    AgentSession._emit_terminal_error(session, ValueError("boom in agent code"))

    event_type, payload = store.events[-1]
    assert event_type == "terminal_error"
    # No provider signal -> a genuine agent-side failure, not a provider outage.
    assert payload["failure_category"] == "implementation_failed"
    assert "provider_status_code" not in payload


class _FailingClient:
    """A client whose every call raises a transient provider failure."""

    model = "test-model"
    temperature = 0.2

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta: Any = None,
        temperature: float | None = None,
    ) -> Any:
        _ = messages, tools, stream, on_text_delta, temperature
        raise LLMError("LLM request failed") from httpx.ReadTimeout("Fixture deadline")


def test_run_turn_records_terminal_error_to_store_end_to_end(tmp_path: Path) -> None:
    # A real run_turn driven by a failing client must leave one durable, joinable
    # terminal_error in the default-on per-run store JSONL -- the artifact an
    # autonomous fix-loop reads to learn why the build died.
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="terminal-e2e",
    )
    session.client = _FailingClient()  # type: ignore[assignment]
    try:
        with pytest.raises(LLMError):
            session.run_turn("Refactor src/app.py and explain the change.")
    finally:
        session.close()

    events = list(read_session_events(sessions_dir / "terminal-e2e.jsonl"))
    terminal = [e.get("payload", {}) for e in events if e.get("type") == "terminal_error"]
    assert terminal, "run_turn must record one terminal_error to the per-run store"

    payload = terminal[-1]
    assert payload["operation"] == "run_turn"
    # A typed transport timeout is a transient provider outage across modes.
    assert payload["failure_category"] == FailureCategory.PROVIDER_UNAVAILABLE.value
    assert payload["failure_category"] in {member.value for member in FailureCategory}
    assert "error_summary" in payload
