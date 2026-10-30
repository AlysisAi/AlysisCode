"""Provider-failure salvage: keep persisted work when the endpoint dies.

Observed in scored runs: the provider hung mid-run, the turn raised ``LLMError``
(``provider_unavailable``), and the process exited 75 even though a complete
change could already be on disk. One-shot turns now salvage exactly when
material work persisted; a run that produced nothing keeps the loud
infrastructure failure so operators and retry machinery still see it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.llm.types import LLMError
from alysis_code.session_store import read_session_events

_PROVIDER_DOWN_MESSAGE = (
    "LLM request failed for endpoint test-endpoint: The read operation timed out"
)


class _FailingClient:
    """Answers a scripted prefix, then raises the configured LLMError."""

    model = "test-model"
    temperature = 0.2

    def __init__(self, prefix: list[LLMResponse], error_message: str) -> None:
        self._prefix = prefix
        self._error_message = error_message
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        stream: bool = False,
        on_text_delta: Any | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, tool_choice, stream, on_text_delta, temperature
        index = self.calls
        self.calls += 1
        if index < len(self._prefix):
            return self._prefix[index]
        raise LLMError(self._error_message)


def _init_git_repo_with_commit(repo: Path) -> None:
    repo.mkdir(exist_ok=True)
    for args in (
        ["init"],
        ["config", "user.name", "Test User"],
        ["config", "user.email", "test@example.com"],
    ):
        subprocess.run(
            ["git", "-C", os.fspath(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", os.fspath(repo), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
    )


def _events_of(sessions_dir: Path, session_id: str) -> list[dict[str, Any]]:
    return list(read_session_events(sessions_dir / f"{session_id}.jsonl"))


def _payloads(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event["payload"] for event in events if event.get("type") == event_type]


def _run_failing_turn(
    tmp_path: Path,
    *,
    session_id: str,
    prefix: list[LLMResponse],
    error_message: str = _PROVIDER_DOWN_MESSAGE,
) -> tuple[int, list[dict[str, Any]]]:
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="code_only",
            extra_fields={"shell_sandbox": {"mode": "off"}},
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=12,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    session.client = _FailingClient(prefix, error_message)  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Fix the typo in README.md.")
    finally:
        session.close()
    return exit_code, _events_of(sessions_dir, session_id)


def test_provider_failure_with_material_work_salvages_exit_zero(tmp_path: Path) -> None:
    _init_git_repo_with_commit(tmp_path)
    prefix = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    name="fs_write",
                    arguments={"path": "README.md", "content": "repo fixed\n"},
                )
            ],
            raw={},
        )
    ]

    exit_code, events = _run_failing_turn(tmp_path, session_id="salvage-ok", prefix=prefix)

    assert exit_code == 0
    outcome = _payloads(events, "turn_outcome")[-1]
    assert outcome["outcome"] == "provider_failure"
    assert outcome["verified_success"] is False
    salvages = _payloads(events, "provider_failure_salvage")
    assert salvages, "expected a provider_failure_salvage event"
    assert salvages[0]["material_work_persisted"] is True
    assert "README.md" in salvages[0]["salvaged_paths"]
    assert salvages[0]["failure_category"] == "provider_unavailable"
    finals = _payloads(events, "final")
    assert finals, "expected a final runtime summary"
    assert finals[-1].get("internal_fallback_kind") == "provider_failure_salvage"
    assert finals[-1].get("degraded") is True


def test_provider_failure_without_material_work_still_raises(tmp_path: Path) -> None:
    _init_git_repo_with_commit(tmp_path)

    with pytest.raises(LLMError):
        _run_failing_turn(tmp_path, session_id="salvage-none", prefix=[])


def test_dirty_repo_without_agent_edits_still_raises(tmp_path: Path) -> None:
    """Pre-existing user changes are not salvage evidence: a provider failure
    before the agent edited anything must stay a loud infrastructure failure
    even when the working tree already has a diff."""
    _init_git_repo_with_commit(tmp_path)
    (tmp_path / "README.md").write_text("user's own uncommitted change\n", encoding="utf-8")

    with pytest.raises(LLMError):
        _run_failing_turn(tmp_path, session_id="salvage-dirty-repo", prefix=[])


def test_reverted_edits_leave_no_salvage_evidence(tmp_path: Path) -> None:
    """Edit-then-revert produces a clean tree; a provider failure afterwards is
    an infrastructure failure, not a success, even though material_edit_count
    is positive and the touched-path record names README.md."""
    _init_git_repo_with_commit(tmp_path)
    prefix = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    name="fs_write",
                    arguments={"path": "README.md", "content": "temporary change\n"},
                )
            ],
            raw={},
        ),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc2",
                    name="fs_write",
                    arguments={"path": "README.md", "content": "repo\n"},
                )
            ],
            raw={},
        ),
    ]

    with pytest.raises(LLMError):
        _run_failing_turn(tmp_path, session_id="salvage-reverted", prefix=prefix)


def test_non_provider_llm_error_still_raises_despite_material_work(tmp_path: Path) -> None:
    _init_git_repo_with_commit(tmp_path)
    prefix = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    name="fs_write",
                    arguments={"path": "README.md", "content": "repo fixed\n"},
                )
            ],
            raw={},
        )
    ]

    with pytest.raises(LLMError):
        _run_failing_turn(
            tmp_path,
            session_id="salvage-nonprovider",
            prefix=prefix,
            error_message="provider rejected the request: invalid parameter shape",
        )
