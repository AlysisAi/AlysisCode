from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.config import AppConfig, clone_cfg, get_api_key, load_config, save_config
from alysis_code.extensions.models import InstalledExtensionState
from alysis_code.ide import management_protocol, stdio_bridge
from alysis_code.ide.prompt_queue import DurablePromptQueue
from alysis_code.ide.stdio_bridge import StdioBridge
from alysis_code.ide.structured_state import DurableStructuredState
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.profiles import (
    SUBSCRIPTION_SELECTION_REQUIRED_KEY,
    ProfileSpec,
    add_profile,
    get_active_profile,
    set_active_profile,
)
from alysis_code.provider_auth import (
    ProviderAccountStatus,
    ProviderModel,
    ProviderReasoningEffort,
)
from alysis_code.surface.types import ApprovalRequest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


def test_turn_touched_path_set_records_repeated_touches_without_losing_history() -> None:
    paths = stdio_bridge._TurnTouchedPathSet({"already-touched.py"})

    paths.update({"already-touched.py", "new-this-turn.py"})

    assert paths == {"already-touched.py", "new-this-turn.py"}
    assert paths.recorded == {"already-touched.py", "new-this-turn.py"}


@pytest.mark.parametrize("verified", [False, True])
def test_chat_completion_exposes_host_verification_verdict(tmp_path, monkeypatch, verified):
    from alysis_code.run_outcome import task_outcome_record

    out = io.StringIO()
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeSession:
        def __init__(self, **kwargs):
            self.cfg = kwargs["cfg"]
            self.surface = kwargs["surface"]
            self.messages = []

        def run_turn(self, _message):
            self.last_turn_outcome = task_outcome_record(
                exit_code=0,
                reason="completed",
                state={
                    "completion_certificate": {"status": "SUFFICIENT"},
                    "accepted_verification_evidence": [{"evidence_category": "USER_EXPLICIT"}],
                    "acceptance_contract": {
                        "criteria": [
                            {
                                "id": "requested-check",
                                "enforcement": "HARD",
                                "required": True,
                                "required_for_finalization": True,
                                "status": "PASSED",
                            }
                        ],
                    },
                }
                if verified
                else None,
            )
            self.surface.emit_message_end("done")
            return 0

        def close(self):
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    created = _send_bridge_request(
        bridge, out, "session.create", {"workspace": str(tmp_path), "mode": "readonly"}
    )
    session_id = created["result"]["session_id"]
    _send_bridge_request(
        bridge, out, "chat.send", {"session_id": session_id, "message": "Report the result."}
    )
    activity = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "activity_update"
            and line.get("payload", {}).get("display_title", "").startswith("Request ")
        ),
    )
    assert activity["payload"]["status"] == ("succeeded" if verified else "completed_unverified")
    _wait_for_job_workers(bridge)
    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    result = status["result"]["last_job"]["result"]
    assert result["verified_success"] is verified
    assert result["task_outcome"]["outcome"] == (
        "verified_success" if verified else "completed_unverified"
    )


@pytest.mark.parametrize(
    "failure",
    ["provider_failure", "deadline_exhausted", "cancelled", "rejected", "post_completion_failure"],
)
def test_chat_error_outcome_retains_current_failure_but_never_prior_success(
    tmp_path, monkeypatch, failure
):
    from alysis_code.run_outcome import task_outcome_record

    out = io.StringIO()
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    success_state = {
        "material_edit_count": 1,
        "completion_certificate": {"status": "SUFFICIENT"},
        "accepted_verification_evidence": [{}],
    }

    class FakeSession:
        def __init__(self, **kwargs):
            self.cfg = kwargs["cfg"]
            self.surface = kwargs["surface"]
            self.messages = []
            self.last_turn_outcome = task_outcome_record(
                exit_code=0, reason="completed", task_id="prior-task", state=success_state
            )

        def run_turn(self, _message):
            if failure != "rejected":
                self.last_turn_outcome = task_outcome_record(
                    exit_code=0 if failure == "post_completion_failure" else 1,
                    reason="completed" if failure == "post_completion_failure" else failure,
                    task_id="current-task",
                    state={**success_state, "verification_relevant_edit_generation": 4},
                )
            if failure == "cancelled":
                raise stdio_bridge.BridgeCancellationError("cancelled_by_user")
            raise RuntimeError("test adapter boundary failure")

        def close(self):
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    created = _send_bridge_request(
        bridge, out, "session.create", {"workspace": str(tmp_path), "mode": "readonly"}
    )
    session_id = created["result"]["session_id"]
    _send_bridge_request(
        bridge, out, "chat.send", {"session_id": session_id, "message": "Run the current task."}
    )
    _wait_for_line(out, lambda line: "job_cancelled" in str(line) or "job_failed" in str(line))
    _wait_for_job_workers(bridge)
    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    result = status["result"]["last_job"]["result"]
    assert result["verified_success"] is False
    record = result["task_outcome"]
    expected = {
        "provider_failure": "provider_failure",
        "deadline_exhausted": "deadline_exceeded",
        "cancelled": "cancelled",
        "rejected": "incomplete",
        "post_completion_failure": "incomplete",
    }
    assert record["outcome"] == expected[failure]
    assert record["task_id"] == ("" if failure == "rejected" else "current-task")
    if failure != "rejected":
        assert record["generation"] == 4


def _request(method: str, params: dict[str, Any] | None = None, request_id: str = "req") -> str:
    return json.dumps(
        {
            "protocol_version": "1",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
        sort_keys=True,
    )


def _json_lines(buffer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def _send_bridge_request(
    bridge: StdioBridge,
    out: io.StringIO,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    before = len(_json_lines(out))
    bridge.process_line(_request(method, params or {}, request_id=request_id or method) + "\n")
    # A method may emit notices before its response (for example, optional Git checkpoints).
    return next(
        line for line in _json_lines(out)[before:] if line.get("id") == (request_id or method)
    )


def _wait_for_line(out: io.StringIO, predicate: Any, *, timeout: float = 30.0) -> dict[str, Any]:
    # Returns as soon as the line appears; the bound only limits a failure, and
    # bridge workers can take seconds to get there on a busy machine.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in _json_lines(out):
            if predicate(line):
                return line
        time.sleep(0.01)
    raise AssertionError("timed out waiting for protocol line")


def _wait_for_job_workers(bridge: StdioBridge, *, timeout: float = 30.0) -> None:
    """Wait until every started job worker has retired.

    A job's final status and terminal events come before its worker's final
    ownership handoff, which is when the session moves it from ``active_job`` to
    ``last_job`` (docs/ide_protocol.md). Read session or job state after this.
    """
    deadline = time.monotonic() + timeout
    for job in list(bridge._jobs.values()):
        thread = job.thread
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            assert not thread.is_alive(), f"job worker {job.job_id} did not retire"


def _isolate_alysis_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    # Managed browser state is intentionally forbidden inside the workspace.
    # Keep the test's process-global data root isolated, but place it beside the
    # temporary workspace so session creation exercises the production boundary.
    monkeypatch.setenv(
        "ALYSIS_DATA_DIR",
        os.fspath(tmp_path.parent / f"{tmp_path.name}-alysis-data"),
    )
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _create_review_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_turn: Any,
    *,
    approval_timeout_seconds: float = 300.0,
    code_review_runner: Any | None = None,
    structured_state_factory: Any | None = None,
    managed_browser_factory: Any | None = None,
) -> tuple[io.StringIO, StdioBridge, str]:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir(exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", subagents_enabled=False),
    )

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, message: str) -> int:
            return int(run_turn(self.surface, message) or 0)

        def close(self) -> None:
            pass

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: FakeSession(kwargs["surface"]),
        approval_timeout_seconds=approval_timeout_seconds,
        code_review_runner=code_review_runner,
        **(
            {"structured_state_factory": structured_state_factory}
            if structured_state_factory is not None
            else {}
        ),
        **(
            {"managed_browser_factory": managed_browser_factory}
            if managed_browser_factory is not None
            else {}
        ),
    )
    bridge.process_line(
        _request(
            "session.create",
            {"workspace": os.fspath(tmp_path), "mode": "review", "model": "test-model"},
            request_id="create",
        )
        + "\n"
    )
    session_id = _json_lines(out)[0]["result"]["session_id"]
    return out, bridge, session_id


def test_artifact_check_receipts_are_read_from_the_named_session(tmp_path, monkeypatch):
    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, lambda *_: 0)
    report = {"path": "report.docx", "documentSha256": "a" * 64, "items": []}
    session = bridge._require_session(session_id, request_id="test")
    session.agent_session.tools = {
        "artifact_check": SimpleNamespace(metadata={"artifact_reports": [report]})
    }
    try:
        response = _send_bridge_request(
            bridge,
            out,
            "session.artifactChecks",
            {"session_id": session_id, "reports": [{"forged": True}]},
        )
        assert response["ok"] is True
        assert response["result"] == {"session_id": session_id, "reports": [report]}
        missing = _send_bridge_request(
            bridge, out, "session.artifactChecks", {"session_id": "unrelated"}
        )
        assert missing["ok"] is False
    finally:
        bridge.close()


def test_optional_browser_private_root_does_not_block_unrelated_chat(tmp_path, monkeypatch):
    from alysis_code.ide.managed_browser import BrowserSecurityError

    data = tmp_path / "private-data"
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(data))
    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, lambda *_: 0)
    try:
        assert not (data / "ide-browser").exists()
        response = _send_bridge_request(
            bridge, out, "chat.send", {"session_id": session_id, "message": "Explain this term."}
        )
        assert response["ok"] is True
        _wait_for_line(
            out,
            lambda line: (
                line.get("type") == "activity_update"
                and line.get("payload", {}).get("status") == "completed_unverified"
            ),
        )
        _wait_for_job_workers(bridge)
        status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
        assert status["result"]["last_job"]["status"] == "completed"
        assert not (data / "ide-browser").exists()
        session = bridge._require_session(session_id, request_id="test")
        with pytest.raises(BrowserSecurityError, match="outside the workspace"):
            session.managed_browser.start("owner")
        assert not (
            data / "ide-browser"
        ).exists()  # No private browser data is created inside the workspace.
    finally:
        bridge.close()


def test_checkpoint_refusal_is_structured_before_dispatch_and_does_not_block_read_only_tools(
    monkeypatch,
):
    from alysis_code.agent.errors import AgentRuntimeError

    job = stdio_bridge.BridgeJob(job_id="first", session_id="session", created_at="now")
    session = SimpleNamespace(active_job=job)
    monkeypatch.setattr(stdio_bridge, "_checkpointing_supported", lambda _: True)
    ready = [False]
    monkeypatch.setattr(stdio_bridge, "_prepare_change_ledger_with_deadline", lambda _: ready[0])
    guard = stdio_bridge._CheckpointToolDispatchGuard({"session": session})
    with pytest.raises(AgentRuntimeError) as caught:
        guard.check_tool_call("fs_write", {"path": "notes.txt", "content": "new"})
    assert caught.value.result_payload["error_code"] == "checkpoint_unavailable"
    assert caught.value.result_payload["retryable"] is False
    assert caught.value.result_payload["dispatch_state"] == "not_dispatched"
    guard.check_tool_call("fs_read", {"path": "notes.txt"})
    ready[0] = True
    session.active_job = stdio_bridge.BridgeJob(
        job_id="next", session_id="session", created_at="later"
    )
    guard.check_tool_call("fs_write", {"path": "notes.txt", "content": "new"})


def test_unused_or_closed_deferred_browser_never_initializes_on_cleanup():
    created = []
    closed = []
    service = SimpleNamespace(status=lambda *_: "ready", close_all=lambda: closed.append(True))
    proxy = stdio_bridge._DeferredManagedBrowserService(lambda: created.append(True) or service)
    late_call = proxy.status
    assert created == []
    proxy.close_all()
    assert created == []
    with pytest.raises(stdio_bridge.BrowserError, match="closed"):
        late_call("owner", "session")
    assert created == []
    with pytest.raises(stdio_bridge.BrowserError, match="closed"):
        proxy.status("owner", "session")
    assert created == []
    used = stdio_bridge._DeferredManagedBrowserService(lambda: created.append(True) or service)
    assert used.status("owner", "session") == "ready"
    assert used.status("owner", "session") == "ready"
    assert created == [True]
    used.close_all()
    used.close_all()
    assert closed == [True]


@pytest.mark.parametrize("reserved", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_output_declarations_require_exact_completed_job_and_current_task(
    tmp_path, monkeypatch, reserved, nested
):
    from alysis_code.agent.acceptance_contract import build_acceptance_contract
    from alysis_code.agent.task_artifact_binding import bind_path_interpretation
    from alysis_code.agent.verification import TurnExecutionState

    project = tmp_path
    if nested:
        subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
        project = tmp_path / "project"
        project.mkdir()
    out, bridge, session_id = _create_review_session(project, monkeypatch, lambda *_: 0)
    session = bridge._require_session(session_id, request_id="test")
    instruction = "Create summary.csv."
    refs = bind_path_interpretation(
        root=session.root,
        working_directory="project" if nested else ".",
        instruction=instruction,
        prior_refs=[],
        arguments={
            "paths": [
                {
                    "name": "summary.csv",
                    "directory": "",
                    "path_base": "active_workdir",
                    "role": "required_output",
                    "evidence_quote": instruction,
                }
            ]
        },
    )
    contract = build_acceptance_contract(
        root=session.root, instruction=instruction, interpreted_path_refs=refs
    )
    contract.task_id = "current-task"
    state = TurnExecutionState(execution_requested=True, acceptance_contract=contract)
    state.turn_touched_repo_paths.add("project/summary.csv" if nested else "summary.csv")
    session.agent_session._turn_execution_state = state
    session.agent_session.last_turn_outcome = {"terminal": True, "task_id": contract.task_id}
    job = stdio_bridge.BridgeJob(
        job_id="finished-job",
        session_id=session_id,
        created_at="now",
        status="completed",
        exit_code=0,
        result={"task_outcome": dict(session.agent_session.last_turn_outcome)},
    )
    if reserved:
        session.active_job = job
    else:
        session.last_job = job
    params = {"session_id": session_id, "job_id": job.job_id}
    try:
        response = _send_bridge_request(bridge, out, "session.outputArtifacts", params)
        assert response["ok"] is True
        assert response["result"] == {**params, "task_id": "current-task", "paths": ["summary.csv"]}
        for invalid in [
            {**params, "paths": ["forged.csv"]},
            {**params, "session_id": "other"},
            {**params, "job_id": "previous"},
        ]:
            assert (
                _send_bridge_request(bridge, out, "session.outputArtifacts", invalid)["ok"] is False
            )
        for attribute, value in [
            ("status", "running"),
            ("exit_code", 130),
            ("session_id", "other"),
        ]:
            old = getattr(job, attribute)
            setattr(job, attribute, value)
            assert (
                _send_bridge_request(bridge, out, "session.outputArtifacts", params)["ok"] is False
            )
            setattr(job, attribute, old)
        session.agent_session.last_turn_outcome = {"terminal": True, "task_id": "stale"}
        assert _send_bridge_request(bridge, out, "session.outputArtifacts", params)["ok"] is False
        session.agent_session.last_turn_outcome = {"terminal": True, "task_id": "current-task"}
        session.sensitive_media = True
        assert _send_bridge_request(bridge, out, "session.outputArtifacts", params)["ok"] is False
        session.sensitive_media = False
        job.result = {"task_outcome": {"task_id": "other-task"}}
        assert _send_bridge_request(bridge, out, "session.outputArtifacts", params)["ok"] is False
        job.result = {"task_outcome": {"task_id": "current-task"}}
        session.active_job = stdio_bridge.BridgeJob(
            job_id="new-job", session_id=session_id, created_at="later"
        )
        assert _send_bridge_request(bridge, out, "session.outputArtifacts", params)["ok"] is False
        session.active_job = None
    finally:
        session.active_job = None
        bridge.close()


def test_stdio_bridge_health_and_malformed_requests() -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(_request("health", request_id="health") + "\n")
    bridge.process_line("{not-json}\n")
    lines = _json_lines(out)

    assert lines[0]["id"] == "health"
    assert lines[0]["ok"] is True
    assert lines[0]["result"]["protocol_version"] == "1"
    assert lines[1]["ok"] is False
    assert lines[1]["error"]["code"] == "malformed_json"


def test_stdio_bridge_managed_browser_protocol_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBrowser:
        def __init__(self) -> None:
            self.closed_all = False
            self.status_value = SimpleNamespace(
                session_id="browser-session-1234567890",
                product="chrome",
                state="running",
                created_at=1.0,
                allow_local_destinations=False,
                active_url=(
                    "https://browser-user:browser-password@example.test/account/"
                    "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
                    "?token=browser-query-secret#browser-fragment-secret"
                ),
                artifact_count=0,
            )

        def start(self, owner_id: str, **kwargs: Any) -> Any:
            assert owner_id
            assert kwargs["allow_local_destinations"] is False
            return self.status_value

        def navigate(self, owner_id: str, browser_session_id: str, url: str, **kwargs: Any) -> Any:
            del owner_id, kwargs
            assert browser_session_id == self.status_value.session_id
            return {
                "session_id": browser_session_id,
                "url": url,
                "result": {"detail": "token=super-secret-value"},
            }

        def snapshot(self, owner_id: str, browser_session_id: str, **kwargs: Any) -> Any:
            del owner_id, kwargs
            return {"session_id": browser_session_id, "kind": "text", "text": "page"}

        def screenshot(self, owner_id: str, browser_session_id: str, **kwargs: Any) -> Any:
            del owner_id, kwargs
            return SimpleNamespace(
                artifact_id=f"browser:{browser_session_id}:screenshot-0001-deadbeef.png",
                media_type="image/png",
                size_bytes=12,
                sha256="a" * 64,
            )

        def read_artifact(
            self, owner_id: str, browser_session_id: str, artifact_id: str, **kwargs: Any
        ) -> Any:
            del owner_id, browser_session_id, kwargs
            return {
                "artifact_id": artifact_id,
                "encoding": "base64",
                "content": "iVBORw0KGgo=",
                "offset": 0,
                "next_offset": 8,
                "size_bytes": 8,
                "truncated": False,
            }

        def diagnostics(self, owner_id: str, browser_session_id: str, **kwargs: Any) -> Any:
            del owner_id, kwargs
            return {"session_id": browser_session_id, "events": [], "truncated": False}

        def click(
            self, owner_id: str, browser_session_id: str, selector: str, **kwargs: Any
        ) -> Any:
            del owner_id, selector, kwargs
            return {"session_id": browser_session_id, "clicked": True}

        def type_text(
            self,
            owner_id: str,
            browser_session_id: str,
            selector: str,
            text: str,
            **kwargs: Any,
        ) -> Any:
            del owner_id, selector, kwargs
            return {"session_id": browser_session_id, "typed": True, "character_count": len(text)}

        def status(self, owner_id: str, browser_session_id: str) -> Any:
            del owner_id
            assert browser_session_id == self.status_value.session_id
            return self.status_value

        def list(self, owner_id: str) -> tuple[Any, ...]:
            del owner_id
            return (self.status_value,)

        def close(self, owner_id: str, browser_session_id: str, **kwargs: Any) -> bool:
            del owner_id, browser_session_id, kwargs
            return True

        def close_all(self) -> None:
            self.closed_all = True

    fake = FakeBrowser()
    out, bridge, session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        lambda _surface, _message: 0,
        managed_browser_factory=lambda **_kwargs: fake,
    )
    untrusted = _send_bridge_request(
        bridge,
        out,
        "browser.start",
        {"session_id": session_id},
        request_id="browser-untrusted",
    )
    assert untrusted["error"]["code"] == "workspace_trust_required"
    started = _send_bridge_request(
        bridge,
        out,
        "browser.start",
        {"session_id": session_id, "workspace_trusted": True},
        request_id="browser-start",
    )
    browser_session_id = started["result"]["browser_session_id"]
    assert browser_session_id == fake.status_value.session_id
    public_active_url = "https://example.test/account/<redacted>"
    assert started["result"]["active_url"] == public_active_url
    assert "browser-user" not in repr(started)
    assert "browser-password" not in repr(started)
    assert "browser-query-secret" not in repr(started)
    assert "browser-fragment-secret" not in repr(started)

    navigated = _send_bridge_request(
        bridge,
        out,
        "browser.navigate",
        {
            "session_id": session_id,
            "browser_session_id": browser_session_id,
            "url": (
                "https://nav-user:nav-password@example.test/account/"
                "sk-12345678901234567890"
                "?code=arbitrary-oauth-code#private-navigation-fragment"
            ),
            "workspace_trusted": True,
        },
        request_id="browser-navigate",
    )
    assert navigated["result"]["url"] == "https://example.test/account/<redacted>"
    assert "super-secret-value" not in repr(navigated)
    assert "nav-user" not in repr(navigated)
    assert "nav-password" not in repr(navigated)
    assert "arbitrary-oauth-code" not in repr(navigated)
    assert "private-navigation-fragment" not in repr(navigated)
    snapshot = _send_bridge_request(
        bridge,
        out,
        "browser.snapshot",
        {"session_id": session_id, "browser_session_id": browser_session_id, "kind": "text"},
        request_id="browser-snapshot",
    )
    assert snapshot["result"]["text"] == "page"
    screenshot = _send_bridge_request(
        bridge,
        out,
        "browser.screenshot",
        {"session_id": session_id, "browser_session_id": browser_session_id},
        request_id="browser-screenshot",
    )
    artifact_id = screenshot["result"]["artifact_id"]
    artifact = _send_bridge_request(
        bridge,
        out,
        "browser.artifact.read",
        {
            "session_id": session_id,
            "browser_session_id": browser_session_id,
            "artifact_id": artifact_id,
        },
        request_id="browser-artifact",
    )
    assert artifact["result"]["encoding"] == "base64"
    status = _send_bridge_request(
        bridge,
        out,
        "browser.status",
        {"session_id": session_id, "browser_session_id": browser_session_id},
        request_id="browser-status",
    )
    assert status["result"]["active_url"] == public_active_url
    listed = _send_bridge_request(
        bridge,
        out,
        "browser.list",
        {"session_id": session_id},
        request_id="browser-list",
    )
    assert listed["result"]["count"] == 1
    assert listed["result"]["browsers"][0]["active_url"] == public_active_url
    browser_payloads = (started, status, listed)
    assert all("browser-user" not in repr(payload) for payload in browser_payloads)
    assert all("browser-password" not in repr(payload) for payload in browser_payloads)
    assert all("browser-query-secret" not in repr(payload) for payload in browser_payloads)
    assert all("browser-fragment-secret" not in repr(payload) for payload in browser_payloads)
    retained = _send_bridge_request(
        bridge,
        out,
        "browser.close",
        {
            "session_id": session_id,
            "browser_session_id": browser_session_id,
            "workspace_trusted": True,
            "delete_artifacts": False,
            "confirm": True,
        },
        request_id="browser-close-retain",
    )
    assert retained["error"]["code"] == "unsupported_artifact_retention"
    unconfirmed = _send_bridge_request(
        bridge,
        out,
        "browser.close",
        {
            "session_id": session_id,
            "browser_session_id": browser_session_id,
            "workspace_trusted": True,
        },
        request_id="browser-close-unconfirmed",
    )
    assert unconfirmed["error"]["code"] == "confirmation_required"
    closed = _send_bridge_request(
        bridge,
        out,
        "browser.close",
        {
            "session_id": session_id,
            "browser_session_id": browser_session_id,
            "delete_artifacts": True,
            "confirm": True,
        },
        request_id="browser-close",
    )
    assert closed["result"]["status"] == "closed"
    bridge.close()
    assert fake.closed_all is True


def test_stdio_bridge_session_close_keeps_uncertain_cleanup_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryBrowser:
        def __init__(self) -> None:
            self.close_calls = 0

        def close_all(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("injected cleanup failure with private detail")

    browser = RetryBrowser()
    out, bridge, session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        lambda _surface, _message: 0,
        managed_browser_factory=lambda **_kwargs: browser,
    )

    failed = _send_bridge_request(
        bridge,
        out,
        "session.cancel",
        {"session_id": session_id},
        request_id="session-close-failed",
    )
    assert failed["error"] == {
        "code": "browser_cleanup_incomplete",
        "message": "Managed browser cleanup is incomplete; the session remains blocked.",
    }
    assert "private detail" not in repr(failed)

    retried = _send_bridge_request(
        bridge,
        out,
        "session.cancel",
        {"session_id": session_id},
        request_id="session-close-retry",
    )
    assert retried["error"]["code"] == "session_busy"
    assert browser.close_calls == 1
    assert bridge._sessions[session_id].cancellation_uncertain is True
    assert bridge._sessions[session_id].closed is False


def test_stdio_bridge_shutdown_retries_browser_cleanup_within_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryBrowser:
        def __init__(self) -> None:
            self.close_calls = 0

        def close_all(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("transient cleanup failure")

    browser = RetryBrowser()
    _out, bridge, _session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        lambda _surface, _message: 0,
        managed_browser_factory=lambda **_kwargs: browser,
    )

    bridge.close()

    assert browser.close_calls == 2


def test_stdio_bridge_run_bounds_and_resynchronizes_oversized_input() -> None:
    oversized = "x" * (stdio_bridge.MAX_REQUEST_BYTES + 1)
    valid = _request("health", request_id="health-after-oversized")
    out = io.StringIO()
    bridge = StdioBridge(stdin=io.StringIO(f"{oversized}\n{valid}\n"), stdout=out)

    assert bridge.run() == 0

    lines = _json_lines(out)
    assert lines[0]["error"]["code"] == "request_too_large"
    assert lines[1]["id"] == "health-after-oversized"
    assert lines[1]["ok"] is True


def test_stdio_bridge_shutdown_request_replies_then_exits_run_loop() -> None:
    out = io.StringIO()
    bridge = StdioBridge(
        stdin=io.StringIO(
            _request("bridge.shutdown", request_id="shutdown")
            + "\n"
            + _request("health", request_id="must-not-run")
            + "\n"
        ),
        stdout=out,
    )

    assert bridge.run() == 0

    lines = _json_lines(out)
    assert lines == [
        {
            "protocol_version": "1",
            "id": "shutdown",
            "ok": True,
            "result": {"status": "shutting_down"},
        }
    ]
    assert bridge._closed is True  # noqa: SLF001


def test_run_stdio_bridge_keeps_incidental_prints_off_protocol_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol_out = io.StringIO()
    diagnostics = io.StringIO()

    protocol_in = io.StringIO()

    class FakeBridge:
        def __init__(self, *, stdin: Any, stdout: Any) -> None:
            assert stdin is protocol_in
            assert stdout is protocol_out

        def run(self) -> int:
            print("provider diagnostic that is not JSON")
            protocol_out.write('{"protocol_version":"1","id":"ok","ok":true}\n')
            return 0

    monkeypatch.setattr(stdio_bridge, "StdioBridge", FakeBridge)
    # The real claim moves descriptors 0 and 1 to the null device; it has its own
    # subprocess test in test_ide_protocol_stream_isolation.py.
    monkeypatch.setattr(stdio_bridge, "claim_protocol_streams", lambda: (protocol_in, protocol_out))
    monkeypatch.setattr(sys, "stdout", protocol_out)
    monkeypatch.setattr(sys, "stderr", diagnostics)

    assert stdio_bridge.run_stdio_bridge() == 0
    assert "provider diagnostic" not in protocol_out.getvalue()
    assert "provider diagnostic" in diagnostics.getvalue()
    assert json.loads(protocol_out.getvalue())["ok"] is True


def test_stdio_bridge_rejects_unsupported_protocol_version() -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line('{"protocol_version":"2","id":"bad","method":"health","params":{}}\n')

    payload = _json_lines(out)[0]
    assert payload["id"] == "bad"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unsupported_protocol_version"


def test_generic_code_review_runs_as_bounded_session_job_and_returns_structured_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[Path, str]] = []

    def run_review(workspace: Path, _session: Any, review: Any) -> dict[str, Any]:
        observed.append((workspace, review.scope.value))
        return {
            "scope": review.scope.value,
            "findings": [],
            "summary": {
                "verdict": "approve",
                "overview": "No actionable findings.",
                "finding_counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                "changed_file_count": 1,
                "reviewed_file_count": 1,
                "omitted_file_count": 0,
                "truncated": False,
                "warnings": [],
            },
            "diff": {
                "scope": review.scope.value,
                "changed_files": ["src/demo.py"],
                "included_files": ["src/demo.py"],
                "omitted_files": [],
                "truncated": False,
                "warnings": [],
                "metadata": {},
                "byte_count": 10,
            },
        }

    out, bridge, session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        lambda _surface, _message: 0,
        code_review_runner=run_review,
    )

    started = _send_bridge_request(
        bridge,
        out,
        "code.review.start",
        {
            "session_id": session_id,
            "scope": "working_tree",
            "workspace_trusted": True,
        },
    )
    job_id = started["result"]["job_id"]
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and f"code_review_completed {job_id}" in str(line.get("payload", {}).get("message", ""))
        ),
    )
    result = _send_bridge_request(
        bridge,
        out,
        "code.review.result",
        {"job_id": job_id},
    )

    assert result["result"]["status"] == "completed"
    assert result["result"]["summary"]["verdict"] == "approve"
    assert result["result"]["diff"]["changed_files"] == ["src/demo.py"]
    assert observed == [(tmp_path.resolve(), "working_tree")]


def test_generic_code_review_rejects_untrusted_and_invalid_revision_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        lambda _surface, _message: 0,
        code_review_runner=lambda *_args: {},
    )

    untrusted = _send_bridge_request(
        bridge,
        out,
        "code.review.start",
        {"session_id": session_id, "scope": "working_tree", "workspace_trusted": False},
    )
    invalid = _send_bridge_request(
        bridge,
        out,
        "code.review.start",
        {
            "session_id": session_id,
            "scope": "commit",
            "revision": "--exec=bad",
            "workspace_trusted": True,
        },
    )

    assert untrusted["error"]["code"] == "workspace_trust_required"
    assert invalid["error"]["code"] == "invalid_review_request"


def test_stdio_bridge_failed_session_create_releases_transient_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    secret_exception_value = "sk-should-never-cross-the-protocol-boundary"

    def fail_create(**kwargs: Any) -> Any:
        kwargs["surface"].emit_info("temporary event")
        raise RuntimeError(f"create failed: {secret_exception_value}")

    bridge = StdioBridge(stdout=out, create_session_fn=fail_create)
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "session_id": "failed-create",
            },
            request_id="failed-create-request",
        )
        + "\n"
    )
    response = _response_by_id(out, "failed-create-request")

    assert response["error"]["code"] == "internal_error"
    assert response["error"]["message"] == (
        "The IDE bridge encountered an unexpected internal error."
    )
    assert secret_exception_value not in out.getvalue()
    assert "failed-create" not in bridge._sessions  # noqa: SLF001
    assert "failed-create" not in bridge._event_buffers  # noqa: SLF001
    assert "failed-create" not in bridge._event_dropped_counts  # noqa: SLF001


def test_stdio_bridge_rejects_inline_secret_without_echoing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "api_key": "must-not-leak",
            },
        )
        + "\n"
    )

    text = out.getvalue()
    payload = _json_lines(out)[0]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "inline_secret_rejected"
    assert "must-not-leak" not in text


def test_stdio_bridge_rejects_inline_secret_for_every_method() -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "getCapabilities",
            {"nested": [{"token": "must-not-leak"}]},
            request_id="caps",
        )
        + "\n"
    )

    text = out.getvalue()
    payload = _json_lines(out)[0]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "inline_secret_rejected"
    assert "must-not-leak" not in text


def test_stdio_bridge_rejects_inline_secret_for_management_method() -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "config.set",
            {
                "workspace_trusted": True,
                "key": "model",
                "value": "test-model",
                "token": "must-not-leak",
            },
            request_id="config-set",
        )
        + "\n"
    )

    text = out.getvalue()
    payload = _json_lines(out)[0]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "inline_secret_rejected"
    assert "must-not-leak" not in text


def test_management_redaction_helper_covers_nested_payloads_and_url_userinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "env-secret-value")

    redacted = management_protocol._redact_payload(
        {
            "api_key": {"present": True, "source": "env"},
            "headers": {"Authorization": "Bearer abcdefghijklmnop"},
            "items": [
                {
                    "url": "https://user:password@example.test/path",
                    "message": "value=env-secret-value DEMO_TOKEN=token-value",
                }
            ],
        }
    )

    text = json.dumps(redacted, sort_keys=True)
    assert redacted["api_key"] == {"present": True, "source": "env"}
    assert "abcdefghijklmnop" not in text
    assert "env-secret-value" not in text
    assert "token-value" not in text
    assert "user:password" not in text
    assert "https://<redacted>@example.test/path" in text


@pytest.mark.parametrize("method", sorted(management_protocol.MUTATING_MANAGEMENT_METHODS))
def test_stdio_bridge_management_mutations_require_workspace_trust(method: str) -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(_request(method, {}, request_id=method) + "\n")

    payload = _json_lines(out)[0]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "workspace_trust_required"


@pytest.mark.parametrize(
    "method",
    [method for method in management_protocol.MANAGEMENT_METHODS if method != "session.usage"],
)
def test_stdio_bridge_dispatches_management_methods(
    method: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    def fake_handle(
        actual_method: str,
        params: dict[str, Any],
        *,
        request_id: Any,
        stateful_handlers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert actual_method == method
        assert params == {"workspace_trusted": True}
        assert request_id == method
        assert stateful_handlers is not None
        assert set(stateful_handlers) == {
            "mcp.auth.login.start",
            "mcp.auth.login.status",
            "mcp.auth.login.cancel",
            "mcp.auth.logout",
        }
        return {"method": actual_method}

    monkeypatch.setattr(stdio_bridge, "handle_management_method", fake_handle)

    bridge.process_line(_request(method, {"workspace_trusted": True}, request_id=method) + "\n")

    payload = _json_lines(out)[0]
    assert payload["ok"] is True
    assert payload["result"] == {"method": method}


def test_stdio_bridge_mcp_methods_are_structured_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    monkeypatch.setenv("ALYSIS_API_KEY", "mcp-secret-value")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    class FakeMcpManager:
        closed = False

        @property
        def tool_bindings(self) -> dict[str, Any]:
            return {"demo": "mcp-secret-value"}

        def catalog_snapshot_metadata(self) -> dict[str, Any]:
            return {"prompt_enabled_server_ids": ["demo"]}

        def list_prompts(self, **kwargs: Any) -> dict[str, Any]:
            _ = kwargs
            return {"prompts": [{"name": "prompt", "description": "mcp-secret-value"}]}

        def get_prompt(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "server_id": kwargs["server_id"],
                "prompt_name": kwargs["prompt_name"],
                "messages": [{"role": "user", "content": "mcp-secret-value"}],
            }

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        management_protocol,
        "_manual_mcp_manager_for_path",
        lambda **_kwargs: FakeMcpManager(),
    )
    monkeypatch.setattr(
        management_protocol,
        "_mcp_status_payload",
        lambda **_kwargs: {
            "servers": [{"id": "demo", "detail": "mcp-secret-value"}],
            "table_rows": [["demo", "mcp-secret-value"]],
        },
    )

    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    status = _send_bridge_request(bridge, out, "mcp.status", {"workspace": os.fspath(tmp_path)})
    prompts = _send_bridge_request(
        bridge,
        out,
        "mcp.prompts.list",
        {"workspace": os.fspath(tmp_path), "server": "demo", "limit": 1},
    )
    prompt = _send_bridge_request(
        bridge,
        out,
        "mcp.prompts.get",
        {
            "workspace": os.fspath(tmp_path),
            "server_id": "demo",
            "prompt_name": "prompt",
            "arguments": {"topic": "repo"},
        },
    )

    assert status["ok"] is True
    assert "table_rows" not in status["result"]
    assert prompts["ok"] is True
    assert prompt["ok"] is True
    assert "mcp-secret-value" not in out.getvalue()
    assert "<redacted>" in out.getvalue()


def test_stdio_bridge_mcp_auth_login_start_does_not_block_and_logout_is_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    monkeypatch.setenv("ALYSIS_API_KEY", "oauth-secret-value")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    server = SimpleNamespace(
        id="demo",
        transport="http",
        url="https://resource.example/mcp",
        oauth=SimpleNamespace(
            client_id="public-client",
            authorization_server_url="https://auth.example/oauth",
            scopes=("read", "write"),
        ),
    )

    monkeypatch.setattr(
        management_protocol,
        "_manual_mcp_resolved_config_for_path",
        lambda **_kwargs: SimpleNamespace(servers=[server]),
    )
    monkeypatch.setattr(
        management_protocol,
        "_resolve_mcp_server_by_id",
        lambda **_kwargs: server,
    )
    monkeypatch.setattr(
        management_protocol,
        "_require_http_oauth_server_for_auth",
        lambda **_kwargs: server,
    )
    monkeypatch.setattr(
        management_protocol,
        "_build_mcp_auth_status_rows",
        lambda **_kwargs: [{"server": "demo", "token": "oauth-secret-value"}],
    )
    pending = SimpleNamespace(
        server_id="demo",
        state=SimpleNamespace(value="pending"),
        to_public_dict=lambda: {
            "flow_id": "flow-1",
            "server_id": "demo",
            "kind": "authorization_code",
            "state": "pending",
            "created_at": 1.0,
            "updated_at": 1.0,
            "expires_at": 301.0,
            "terminal_at": None,
            "error_code": None,
            "authorization_url": "https://auth.example/authorize?state=public-state",
        },
    )
    cancelled = SimpleNamespace(
        server_id="demo",
        state=SimpleNamespace(value="cancelled"),
        to_public_dict=lambda: {
            **pending.to_public_dict(),
            "state": "cancelled",
            "authorization_url": None,
        },
    )
    logout_result = SimpleNamespace(
        server_id="demo",
        local_credentials_removed=True,
        active_flows_cancelled=1,
        remote_revocation_attempted=False,
        remote_revocation_succeeded=None,
        error_code=None,
    )
    coordinator = SimpleNamespace(
        start_authorization_code=lambda request: pending,
        status=lambda flow_id: pending,
        cancel=lambda flow_id: cancelled,
        logout=lambda server_id: logout_result,
        close=lambda: None,
    )

    out = io.StringIO()
    bridge = StdioBridge(stdout=out, mcp_oauth_coordinator=coordinator)

    status = _send_bridge_request(
        bridge, out, "mcp.auth.status", {"workspace": os.fspath(tmp_path)}
    )
    login = _send_bridge_request(
        bridge,
        out,
        "mcp.auth.login.start",
        {
            "workspace": os.fspath(tmp_path),
            "workspace_trusted": True,
            "server_id": "demo",
        },
    )
    login_status = _send_bridge_request(
        bridge,
        out,
        "mcp.auth.login.status",
        {"workspace": os.fspath(tmp_path), "server_id": "demo", "flow_id": "flow-1"},
    )
    login_cancel = _send_bridge_request(
        bridge,
        out,
        "mcp.auth.login.cancel",
        {
            "workspace": os.fspath(tmp_path),
            "workspace_trusted": True,
            "server_id": "demo",
            "flow_id": "flow-1",
        },
    )
    logout_confirmation = _send_bridge_request(
        bridge,
        out,
        "mcp.auth.logout",
        {"workspace": os.fspath(tmp_path), "workspace_trusted": True, "server_id": "demo"},
    )
    logout = _send_bridge_request(
        bridge,
        out,
        "mcp.auth.logout",
        {
            "workspace": os.fspath(tmp_path),
            "workspace_trusted": True,
            "server_id": "demo",
            "yes": True,
        },
    )

    assert status["ok"] is True
    assert login["ok"] is True
    assert login["result"]["supported"] is True
    assert login["result"]["will_block"] is False
    assert login["result"]["flow_id"] == "flow-1"
    assert login["result"]["browser_url"].startswith("https://auth.example/")
    assert login["result"]["browser_opened_by_bridge"] is False
    assert login["result"]["tokens_in_protocol_params"] is False
    assert login["result"]["authorization_code_in_protocol"] is False
    assert login_status["result"]["state"] == "pending"
    assert login_cancel["result"]["state"] == "cancelled"
    assert login_cancel["result"]["changed"] is True
    assert logout_confirmation["result"]["action"]["kind"] == "requires_confirmation"
    assert logout["result"] == {
        "server_id": "demo",
        "removed": True,
        "changed": True,
        "active_flows_cancelled": 1,
        "remote_revocation_attempted": False,
        "remote_revocation_succeeded": None,
        "error_code": None,
        "secret_values_included": False,
    }
    assert "oauth-secret-value" not in out.getvalue()


@pytest.mark.parametrize(
    "method",
    ["mcp.auth.login.complete"],
)
def test_stdio_bridge_mcp_auth_login_lifecycle_methods_are_not_weak_placeholders(
    method: str,
) -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    payload = _send_bridge_request(bridge, out, method, {})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "method_not_found"


def test_stdio_bridge_hooks_methods_are_structured_trust_gated_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    monkeypatch.setenv("ALYSIS_API_KEY", "hooks-secret-value")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    hooks_dir = tmp_path / ".alysis"
    hooks_dir.mkdir()
    hooks_payload = {
        "schema_version": 1,
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "shell_run",
                    "hooks": [
                        {
                            "type": "command",
                            "id": "demo.hook",
                            "command": "echo hooks-secret-value",
                            "enabled": True,
                        }
                    ],
                }
            ]
        },
    }
    (hooks_dir / "hooks.local.json").write_text(json.dumps(hooks_payload), encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(json.dumps(hooks_payload), encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    artifact_path = sessions_dir / "session-1" / "hooks" / "hook_runs.jsonl"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        json.dumps({"hook_id": "demo.hook", "stdout": "hooks-secret-value"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(management_protocol, "resolve_sessions_dir", lambda _cfg: sessions_dir)

    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    workspace = os.fspath(tmp_path)

    listed = _send_bridge_request(bridge, out, "hooks.list", {"workspace": workspace})
    tested = _send_bridge_request(
        bridge,
        out,
        "hooks.test",
        {"workspace": workspace, "event": "PreToolUse", "tool": "shell_run"},
    )
    effective = _send_bridge_request(
        bridge,
        out,
        "hooks.effective",
        {"workspace": workspace, "event": "PreToolUse", "tool": "shell_run"},
    )
    trace = _send_bridge_request(
        bridge, out, "hooks.trace", {"session_id": "session-1", "limit": 10}
    )
    trust = _send_bridge_request(
        bridge,
        out,
        "hooks.trust",
        {"workspace": workspace, "workspace_trusted": True, "target": "project_config"},
    )
    untrust = _send_bridge_request(
        bridge,
        out,
        "hooks.untrust",
        {"workspace": workspace, "workspace_trusted": True, "target": "project_config"},
    )
    disable = _send_bridge_request(
        bridge,
        out,
        "hooks.disable",
        {"workspace": workspace, "workspace_trusted": True, "hook_id": "demo.hook"},
    )
    enable = _send_bridge_request(
        bridge,
        out,
        "hooks.enable",
        {"workspace": workspace, "workspace_trusted": True, "hook_id": "demo.hook"},
    )
    init_confirm = _send_bridge_request(
        bridge,
        out,
        "hooks.init",
        {"workspace": workspace, "workspace_trusted": True},
    )

    assert listed["ok"] is True
    assert listed["result"]["count"] >= 1
    assert tested["ok"] is True
    assert tested["result"]["matches"][0]["matched"] is True
    assert effective["ok"] is True
    assert effective["result"]["hooks"][0]["fires"] is True
    assert trace["ok"] is True
    assert trace["result"]["events"][0]["stdout"] == "<redacted>"
    assert trust["result"]["trusted"] is True
    assert untrust["result"]["trusted"] is False
    assert disable["result"]["enabled"] is False
    assert enable["result"]["enabled"] is True
    assert init_confirm["result"]["action"]["kind"] == "requires_confirmation"
    assert "hooks-secret-value" not in out.getvalue()


def test_stdio_bridge_conventions_and_extension_package_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "Use typed IDE methods.\nAuthorization: Bearer abcdefghijklmnop\n",
        encoding="utf-8",
    )
    registry_entry = SimpleNamespace(
        id="demo.ext",
        name="Demo Extension",
        description="Demo package",
        repo="https://example.invalid/demo.git",
        commit="abc123",
        version="1.0.0",
        tags=["demo"],
        permissions=["tools"],
    )
    installed = InstalledExtensionState(
        id="demo.ext",
        version="1.0.0",
        trust="trusted",
        commit="abc123",
        enabled=True,
        scope="user",
        installed_at="2026-01-01T00:00:00+00:00",
        component_ids={"tools": ["demo"]},
    )
    global_state = SimpleNamespace(installed={"demo.ext": installed}, enabled=["demo.ext"])
    empty_project_state = SimpleNamespace(installed={})
    project_overrides = SimpleNamespace(enabled=[], disabled=[])

    monkeypatch.setattr(
        management_protocol,
        "load_registry",
        lambda: SimpleNamespace(extensions=[registry_entry]),
    )
    monkeypatch.setattr(
        management_protocol, "search_extensions", lambda _registry, _query: [registry_entry]
    )
    monkeypatch.setattr(
        management_protocol, "find_by_id", lambda _registry, _ext_id: registry_entry
    )
    monkeypatch.setattr(management_protocol, "load_global_state", lambda: global_state)
    monkeypatch.setattr(
        management_protocol, "load_project_state", lambda _root: empty_project_state
    )
    monkeypatch.setattr(
        management_protocol, "load_project_overrides", lambda _root: project_overrides
    )
    monkeypatch.setattr(
        management_protocol,
        "resolve_active_plugins",
        lambda **_kwargs: SimpleNamespace(enabled_plugin_ids=frozenset({"demo.ext"})),
    )
    monkeypatch.setattr(
        management_protocol, "uninstall_plugin", lambda **_kwargs: {"id": "demo.ext"}
    )
    monkeypatch.setattr(management_protocol, "enable_plugin", lambda **_kwargs: {"id": "demo.ext"})
    monkeypatch.setattr(management_protocol, "disable_plugin", lambda **_kwargs: {"id": "demo.ext"})
    trust_request = {
        "plugin_id": "demo.ext",
        "plugin_name": "Demo",
        "version": "1.0.0",
        "description": "Demo extension",
        "source_url": "https://example.test/demo.ext.git",
        "commit": "a" * 40,
        "manifest_sha256": "b" * 64,
        "components": {
            "skill_ids": ["demo.skill"],
            "tool_ids": [],
            "mcp_server_ids": [],
            "hook_ids": ["demo.hook"],
        },
        "permissions_summary": {
            "network": False,
            "filesystem_write": True,
            "required_env": [],
            "mcp_scopes": [],
            "hook_events": ["PreToolUse"],
        },
        "security": None,
        "is_reinstall_with_new_commit": False,
    }
    install_trust_decisions: list[bool] = []

    def fake_install_plugin(**kwargs: Any) -> dict[str, Any]:
        decision = bool(kwargs["trust_prompt"](trust_request))
        install_trust_decisions.append(decision)
        if not decision:
            raise management_protocol.PluginInstallError("install rejected by user")
        return {"id": "demo.ext", "trust_was_prompted": True}

    monkeypatch.setattr(management_protocol, "install_plugin", fake_install_plugin)

    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    workspace = os.fspath(tmp_path)

    conventions_list = _send_bridge_request(
        bridge, out, "conventions.list", {"workspace": workspace}
    )
    conventions_render = _send_bridge_request(
        bridge,
        out,
        "conventions.render",
        {"workspace": workspace, "max_chars": 500, "max_bytes": 500},
    )
    ext_search = _send_bridge_request(bridge, out, "ext.search", {"query": "demo"})
    ext_list = _send_bridge_request(bridge, out, "ext.list", {"workspace": workspace})
    ext_info = _send_bridge_request(
        bridge, out, "ext.info", {"workspace": workspace, "ext_id": "demo.ext"}
    )
    ext_install_confirm = _send_bridge_request(
        bridge,
        out,
        "ext.install",
        {"workspace": workspace, "workspace_trusted": True, "source": "demo.ext"},
    )
    ext_install_yes_without_trust = _send_bridge_request(
        bridge,
        out,
        "ext.install",
        {"workspace": workspace, "workspace_trusted": True, "source": "demo.ext", "yes": True},
    )
    trust_approval = ext_install_confirm["result"]["action"]["required_approval"]
    ext_install = _send_bridge_request(
        bridge,
        out,
        "ext.install",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "source": "demo.ext",
            "yes": True,
            "trust_approval": trust_approval,
        },
    )
    ext_install_mismatch = _send_bridge_request(
        bridge,
        out,
        "ext.install",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "source": "demo.ext",
            "yes": True,
            "trust_approval": {
                **trust_approval,
                "manifest_sha256": "c" * 64,
            },
        },
    )
    ext_install_userinfo = _send_bridge_request(
        bridge,
        out,
        "ext.install",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "source": f"https://user:pass@example.test/repo.git#{'a' * 40}",
        },
        request_id="ext-install-userinfo",
    )
    ext_install_traversal = _send_bridge_request(
        bridge,
        out,
        "ext.install",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "source": "../demo.ext",
        },
        request_id="ext-install-traversal",
    )
    ext_uninstall = _send_bridge_request(
        bridge,
        out,
        "ext.uninstall",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "plugin_id": "demo.ext",
            "yes": True,
        },
    )
    ext_enable = _send_bridge_request(
        bridge,
        out,
        "ext.enable",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "plugin_id": "demo.ext",
            "yes": True,
        },
    )
    ext_disable = _send_bridge_request(
        bridge,
        out,
        "ext.disable",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "plugin_id": "demo.ext",
            "yes": True,
        },
    )

    assert conventions_list["ok"] is True
    assert conventions_list["result"]["count"] >= 1
    assert conventions_render["ok"] is True
    assert "Use typed IDE methods" in conventions_render["result"]["rendered"]
    assert conventions_render["result"]["secret_values_included"] is False
    assert conventions_render["result"]["redacted"] is True
    assert "abcdefghijklmnop" not in conventions_render["result"]["rendered"]
    assert ext_search["result"]["extensions"][0]["id"] == "demo.ext"
    assert ext_list["result"]["extensions"][0]["enabled_effective"] is True
    assert ext_info["result"]["enabled_effective"] is True
    assert ext_install_confirm["result"]["action"]["kind"] == "requires_extension_trust_review"
    assert ext_install_confirm["result"]["trust_request"]["plugin_id"] == "demo.ext"
    assert (
        ext_install_yes_without_trust["result"]["action"]["kind"]
        == "requires_extension_trust_review"
    )
    assert ext_install["result"]["changed"] is True
    assert ext_install_mismatch["ok"] is False
    assert ext_install_mismatch["error"]["code"] == "extension_trust_mismatch"
    assert ext_install_userinfo["ok"] is False
    assert ext_install_userinfo["error"]["code"] == "invalid_base_url"
    assert ext_install_traversal["ok"] is False
    assert ext_install_traversal["error"]["code"] == "invalid_extension_source"
    assert install_trust_decisions == [False, False, True]
    assert ext_uninstall["result"]["changed"] is True
    assert ext_enable["result"]["changed"] is True
    assert ext_disable["result"]["changed"] is True
    assert "user:pass" not in out.getvalue()


def test_stdio_bridge_workspace_required_management_methods_reject_missing_workspace() -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    response = _send_bridge_request(
        bridge,
        out,
        "ext.install",
        {"workspace_trusted": True, "source": "demo.ext"},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "missing_param"
    assert "workspace or path" in response["error"]["message"]


def test_stdio_bridge_skill_install_validates_sources_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    bundle = tmp_path / "skill"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-skill"
    outside.mkdir(exist_ok=True)
    (outside / "SKILL.md").write_text("# Outside\n", encoding="utf-8")
    symlink = tmp_path / "skill-link"
    if hasattr(os, "symlink"):
        _symlink_or_skip(symlink, outside, target_is_directory=True)

    install_sources: list[str] = []

    def fake_install_skill_bundle(**kwargs: Any) -> dict[str, str]:
        install_sources.append(str(kwargs["source"]))
        return {"installed_name": "demo", "bundle_path": str(kwargs["source"])}

    monkeypatch.setattr(management_protocol, "install_skill_bundle", fake_install_skill_bundle)

    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    workspace = os.fspath(tmp_path)

    local = _send_bridge_request(
        bridge,
        out,
        "skill.install",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "source": "skill",
            "project": True,
        },
        request_id="local",
    )
    outside_result = _send_bridge_request(
        bridge,
        out,
        "skill.install",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "source": os.fspath(outside),
            "project": True,
        },
        request_id="outside",
    )
    missing = _send_bridge_request(
        bridge,
        out,
        "skill.install",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "source": "missing",
            "project": True,
        },
        request_id="missing",
    )
    remote_without_confirmation = _send_bridge_request(
        bridge,
        out,
        "skill.install",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "source": "https://example.test/skills.git",
            "project": True,
        },
        request_id="remote-missing-confirmation",
    )
    remote_userinfo = _send_bridge_request(
        bridge,
        out,
        "skill.install",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "source": "https://user:pass@example.test/skills.git",
            "project": True,
            "allow_remote": True,
            "yes": True,
        },
        request_id="remote-userinfo",
    )
    remote = _send_bridge_request(
        bridge,
        out,
        "skill.install",
        {
            "workspace": workspace,
            "workspace_trusted": True,
            "source": "https://example.test/skills.git",
            "project": True,
            "allow_remote": True,
            "yes": True,
        },
        request_id="remote",
    )

    assert local["ok"] is True
    assert outside_result["ok"] is False
    assert outside_result["error"]["code"] == "path_outside_workspace"
    assert missing["ok"] is False
    assert missing["error"]["code"] == "skill_install_source_not_found"
    assert remote_without_confirmation["ok"] is False
    assert remote_without_confirmation["error"]["code"] == "remote_source_requires_confirmation"
    assert remote_userinfo["ok"] is False
    assert remote_userinfo["error"]["code"] == "invalid_base_url"
    assert remote["ok"] is True
    assert os.fspath(bundle.resolve()) in install_sources
    assert "https://example.test/skills.git" in install_sources
    assert "user:pass" not in out.getvalue()

    if symlink.exists() or symlink.is_symlink():
        symlink_response = _send_bridge_request(
            bridge,
            out,
            "skill.install",
            {
                "workspace": workspace,
                "workspace_trusted": True,
                "source": "skill-link",
                "project": True,
            },
            request_id="symlink",
        )
        assert symlink_response["ok"] is False
        assert symlink_response["error"]["code"] == "skill_install_symlink_rejected"


@pytest.mark.parametrize(
    "method",
    [
        "hooks.watch",
        "hooks.watch.start",
        "hooks.watch.poll",
        "hooks.watch.stop",
        "hooks.watch.status",
    ],
)
def test_stdio_bridge_hooks_watch_lifecycle_is_not_weakly_supported(method: str) -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    payload = _send_bridge_request(bridge, out, method, {})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "method_not_found"


def test_stdio_bridge_rejects_nested_inline_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "provider": {"credentials": {"token": "nested-secret"}},
            },
        )
        + "\n"
    )

    text = out.getvalue()
    payload = _json_lines(out)[0]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "inline_secret_rejected"
    assert "nested-secret" not in text


def test_stdio_bridge_rejects_invalid_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "base_url": "not-a-url",
            },
        )
        + "\n"
    )

    payload = _json_lines(out)[0]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_base_url"

    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "base_url": "https://user:password@example.test",
            },
            request_id="userinfo",
        )
        + "\n"
    )

    payload = _json_lines(out)[0]
    assert payload["id"] == "userinfo"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_base_url"

    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "base_url": "https://example.test/v1?region=test",
            },
            request_id="query",
        )
        + "\n"
    )

    payload = _json_lines(out)[0]
    assert payload["id"] == "query"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_base_url"


def test_stdio_bridge_native_endpoint_override_detaches_from_subscription_without_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    monkeypatch.setenv("ALYSIS_API_KEY", "extension-forwarded-key")
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    persisted_cfg = AppConfig(model="")
    persisted_cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        persisted_cfg,
        ProfileSpec(
            name="chatgpt-codex",
            protocol="openai_responses",
            base_url="https://chatgpt.com/backend-api/codex",
            auth_provider="openai-codex",
        ),
    )
    set_active_profile(persisted_cfg, "chatgpt-codex")
    persisted_cfg.extra_fields[SUBSCRIPTION_SELECTION_REQUIRED_KEY] = "openai-codex"
    persisted_snapshot = persisted_cfg.model_dump(mode="json")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: persisted_cfg)

    def unexpected_subscription_auth(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("native IDE endpoint override attempted subscription authentication")

    monkeypatch.setattr(
        "alysis_code.llm.factory.create_provider_auth",
        unexpected_subscription_auth,
    )
    constructed: list[tuple[Any, str, OpenAICompatClient]] = []

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            api_key = get_api_key(self.cfg)
            client = make_llm_client(cfg=self.cfg, api_key=api_key, model=self.cfg.model)
            assert isinstance(client, OpenAICompatClient)
            constructed.append((self.cfg, api_key, client))
            self.messages: list[dict[str, Any]] = []

        def close(self) -> None:
            constructed[-1][2].close()

    out = io.StringIO()
    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    response = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {
            "workspace": os.fspath(tmp_path),
            "mode": "readonly",
            "model": "mimo-v2.5-pro",
            "base_url": "https://api.xiaomimimo.com/v1",
        },
    )

    assert response["ok"] is True
    session_cfg, api_key, client = constructed[0]
    profile = get_active_profile(session_cfg)
    assert profile.name.startswith("ide-native-")
    assert profile.protocol == "openai_compat"
    assert profile.base_url == "https://api.xiaomimimo.com/v1"
    assert profile.default_model == "mimo-v2.5-pro"
    assert profile.api_key_env == "ALYSIS_API_KEY"
    assert profile.auth_provider is None
    assert profile.extra_headers == {}
    assert api_key == "extension-forwarded-key"
    assert client.api_key == "extension-forwarded-key"
    assert client.base_url == "https://api.xiaomimimo.com/v1"
    assert client.model == "mimo-v2.5-pro"
    assert persisted_cfg.model_dump(mode="json") == persisted_snapshot
    assert "ide-native-" not in json.dumps(persisted_cfg.extra_fields)
    assert "extension-forwarded-key" not in out.getvalue()

    _send_bridge_request(
        bridge,
        out,
        "session.close",
        {"session_id": response["result"]["session_id"]},
    )


def test_stdio_bridge_base_url_only_override_uses_cloned_current_model() -> None:
    persisted_cfg = AppConfig(model="subscription-default")
    persisted_cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        persisted_cfg,
        ProfileSpec(
            name="chatgpt-codex",
            protocol="openai_responses",
            base_url="https://chatgpt.com/backend-api/codex",
            auth_provider="openai-codex",
            default_model="subscription-default",
        ),
    )
    set_active_profile(persisted_cfg, "chatgpt-codex")
    session_cfg = clone_cfg(persisted_cfg)

    stdio_bridge._apply_config_overrides(
        session_cfg,
        {"base_url": "https://native.example.test/v1"},
        request_id="base-only",
    )

    profile = get_active_profile(session_cfg)
    assert session_cfg.model == "subscription-default"
    assert profile.default_model == "subscription-default"
    assert profile.base_url == "https://native.example.test/v1"
    assert profile.auth_provider is None
    assert get_active_profile(persisted_cfg).name == "chatgpt-codex"


def test_stdio_bridge_config_set_rejects_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    monkeypatch.setenv("ALYSIS_API_KEY", "config-secret-value")
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "config.set",
            {
                "workspace_trusted": True,
                "key": "model",
                "value": "config-secret-value",
            },
            request_id="config-secret",
        )
        + "\n"
    )
    bridge.process_line(
        _request(
            "config.set",
            {
                "workspace_trusted": True,
                "key": "base_url",
                "value": "https://user:password@example.test/v1",
            },
            request_id="config-userinfo",
        )
        + "\n"
    )

    text = out.getvalue()
    by_id = {payload["id"]: payload for payload in _json_lines(out)}
    assert by_id["config-secret"]["ok"] is False
    assert by_id["config-secret"]["error"]["code"] == "inline_secret_rejected"
    assert by_id["config-userinfo"]["ok"] is False
    assert by_id["config-userinfo"]["error"]["code"] == "invalid_base_url"
    assert "config-secret-value" not in text
    assert "password" not in text


def test_stdio_bridge_model_picker_atomically_configures_subscription_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    cfg = AppConfig(model="gpt-old")
    add_profile(
        cfg,
        ProfileSpec(
            name="chatgpt-codex",
            protocol="openai_responses",
            base_url="https://chatgpt.com/backend-api/codex",
            auth_provider="openai-codex",
            default_model="gpt-old",
            reasoning_effort="low",
        ),
        allow_auth_profile_update=True,
    )
    set_active_profile(cfg, "chatgpt-codex")
    cfg.extra_fields[SUBSCRIPTION_SELECTION_REQUIRED_KEY] = "openai-codex"
    save_config(cfg)

    class ConnectedCatalog:
        def account_status(self) -> ProviderAccountStatus:
            return ProviderAccountStatus(connected=True)

        def list_models(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
            assert refresh is True
            return (
                ProviderModel(
                    id="gpt-new",
                    label="GPT New",
                    reasoning_efforts=(
                        ProviderReasoningEffort("low", "Low"),
                        ProviderReasoningEffort("high", "High"),
                    ),
                    default_reasoning_effort="high",
                ),
            )

    monkeypatch.setattr(
        management_protocol,
        "create_provider_auth",
        lambda provider_id: ConnectedCatalog() if provider_id == "openai-codex" else None,
    )
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "config.set",
            {"workspace_trusted": True, "key": "model", "value": "gpt-new"},
            request_id="subscription-model",
        )
        + "\n"
    )

    payload = _json_lines(out)[0]
    assert payload["ok"] is True
    assert payload["result"]["reasoning_effort"] == "high"
    assert payload["result"]["subscription_selection_ready"] is True
    loaded = load_config()
    profile = get_active_profile(loaded)
    assert profile.default_model == "gpt-new"
    assert profile.reasoning_effort == "high"
    assert loaded.model == "gpt-new"
    assert loaded.llm_reasoning_effort == "high"
    assert SUBSCRIPTION_SELECTION_REQUIRED_KEY not in loaded.extra_fields
    assert loaded.extra_fields["onboarded"] is True


@pytest.mark.parametrize(
    ("selection_marker", "reasoning_effort", "expected_ready"),
    [
        ("openai-codex", "high", False),
        (None, None, False),
        (None, "high", True),
    ],
)
def test_stdio_bridge_profile_list_exposes_active_subscription_selection_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection_marker: str | None,
    reasoning_effort: str | None,
    expected_ready: bool,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    cfg = AppConfig(model="gpt-5.4", llm_reasoning_effort=reasoning_effort)
    add_profile(
        cfg,
        ProfileSpec(
            name="chatgpt-codex",
            protocol="openai_responses",
            base_url="https://chatgpt.com/backend-api/codex",
            auth_provider="openai-codex",
            default_model="gpt-5.4",
            reasoning_effort=reasoning_effort,
        ),
        allow_auth_profile_update=True,
    )
    set_active_profile(cfg, "chatgpt-codex")
    if selection_marker is not None:
        cfg.extra_fields[SUBSCRIPTION_SELECTION_REQUIRED_KEY] = selection_marker
    save_config(cfg)

    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    bridge.process_line(_request("profile.list", request_id="profile-list") + "\n")

    response = _json_lines(out)[0]
    assert response["ok"] is True
    profile = response["result"]["profiles"][0]
    assert profile["name"] == "chatgpt-codex"
    assert profile["active"] is True
    assert profile["auth_provider"] == "openai-codex"
    assert profile["reasoning_effort"] == reasoning_effort
    assert profile["subscription_selection_ready"] is expected_ready
    assert response["result"]["secret_values_included"] is False


def test_stdio_bridge_run_start_accepts_documented_create_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="default-model", stream=True),
    )
    created_kwargs: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.mode = kwargs["mode"]
            self.no_log = kwargs["no_log"]
            self.yes = kwargs["yes"]
            self.surface = kwargs["surface"]
            self.messages: list[dict[str, Any]] = []
            self.closed = False

        def run_turn(self, message: str, image_paths: list[str] | None = None) -> int:
            turns.append({"message": message, "image_paths": list(image_paths or [])})
            self.surface.emit_message_end("done")
            return 0

        def close(self) -> None:
            self.closed = True

    def fake_create_session(**kwargs: Any) -> FakeSession:
        created_kwargs.append(kwargs)
        return FakeSession(**kwargs)

    bridge = StdioBridge(stdout=out, create_session_fn=fake_create_session)
    bridge.process_line(
        _request(
            "run.start",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "run-model",
                "temperature": 0.3,
                "stream": False,
                "verify_cmd": "pytest -q",
                "subagents_enabled": True,
                "no_log": True,
                "yes": True,
                "max_steps": 7,
                "instruction": "List files",
            },
            request_id="run",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "run"][0]
    assert response["ok"] is True
    assert response["result"]["status"] == "started"
    _wait_for_line(out, lambda line: line.get("type") == "message_end")
    assert turns == [{"message": "List files", "image_paths": []}]
    kwargs = created_kwargs[0]
    assert kwargs["mode"] == "readonly"
    assert kwargs["cfg"].model == "run-model"
    assert kwargs["cfg"].stream is False
    assert kwargs["cfg"].temperature == 0.3
    assert kwargs["verify_cmd"] == ["pytest -q"]
    assert kwargs["subagents_enabled"] is True
    assert kwargs["no_log"] is True
    assert kwargs["yes"] is True
    assert kwargs["max_steps"] == 7


@pytest.mark.parametrize("confidence", [None, "estimated", "authoritative", "reported"])
def test_stdio_bridge_session_context_reports_estimated_tokens_not_fake_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    confidence: str | None,
) -> None:
    if confidence is not None:
        monkeypatch.setattr(
            stdio_bridge,
            "_call_context_left",
            lambda _session: SimpleNamespace(
                model_name="test-model",
                used_input_tokens=123,
                token_count_source="provider_response" if confidence != "estimated" else "mixed",
                token_count_confidence=confidence,
            ),
        )
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages = [{"role": "user", "content": "hello context"}]
            self.tool_list: list[dict[str, Any]] = []
            self.pinned_prefix_len = 0

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    context = _send_bridge_request(
        bridge,
        out,
        "session.context",
        {"session_id": create["result"]["session_id"]},
    )

    assert context["ok"] is True
    result = context["result"]
    assert result["approximate"] is (confidence not in {"authoritative", "reported"})
    assert result["token_usage_available"] is True
    assert result["used_input_tokens"] > 0
    assert result["context_categories"]
    if confidence is None:
        assert result["source"] == "tokenizer_estimate"
        assert result["token_breakdown"]["total_tokens"] == result["used_input_tokens"]
    else:
        assert result["used_input_tokens"] == 123
        assert result["count_confidence"] == confidence
        assert result["count_source"] == (
            "mixed" if confidence == "estimated" else "provider_response"
        )


def test_stdio_bridge_session_context_reports_unavailable_when_context_left_has_no_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    monkeypatch.setattr(
        stdio_bridge,
        "_call_context_left",
        lambda _session: SimpleNamespace(
            model_name="test-model",
            max_input_tokens=None,
            used_input_tokens=None,
            remaining_tokens=None,
            percent_left=None,
            effective_input_budget=None,
            effective_remaining_tokens=None,
            effective_percent_left=None,
            source="unknown",
        ),
    )

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages = [{"role": "user", "content": "hello context"}]
            self.tool_list: list[dict[str, Any]] = []
            self.pinned_prefix_len = 0

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    context = _send_bridge_request(
        bridge,
        out,
        "session.context",
        {"session_id": create["result"]["session_id"]},
    )

    assert context["ok"] is True
    assert context["result"]["source"] == "unavailable"
    assert context["result"]["approximate"] is False
    assert context["result"]["token_usage_available"] is False
    assert context["result"]["used_input_tokens"] is None
    assert context["result"]["token_breakdown"]["total_tokens"] > 0


def test_stdio_bridge_session_model_info_is_structured_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    cfg = AppConfig(
        model="test-model",
        base_url="https://user:secret-token@example.test/v1",
        stream=True,
    )
    cfg.extra_fields = {"active_profile": "test-profile"}
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: cfg)

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    info = _send_bridge_request(
        bridge,
        out,
        "session.modelInfo",
        {"session_id": create["result"]["session_id"]},
    )
    explicit = _send_bridge_request(
        bridge,
        out,
        "session.modelInfo",
        {"session_id": create["result"]["session_id"], "model": "override-model"},
        request_id="explicit-model",
    )

    payload_text = json.dumps(info)
    assert info["ok"] is True
    assert info["result"]["model"] == "test-model"
    assert info["result"]["provider"] == "test-profile"
    assert info["result"]["base_url_redacted"] is True
    assert "secret-token" not in payload_text
    assert info["result"]["secret_values_included"] is False
    assert explicit["result"]["model"] == "override-model"
    assert explicit["result"]["source"] == "config"


def test_stdio_bridge_session_subagent_status_and_trust_gated_toggle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", subagents_enabled=False),
    )

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []
            self.subagents_enabled = False
            self.subagent_registry = {"reviewer": object(), "tester": object()}

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    session_id = create["result"]["session_id"]

    status = _send_bridge_request(
        bridge,
        out,
        "session.subagents.status",
        {"session_id": session_id},
    )
    assert status["ok"] is True
    assert status["result"]["enabled"] is False
    assert status["result"]["available"] == ["reviewer", "tester"]
    assert status["result"]["explicit_execution_supported"] is False
    assert status["result"]["lifecycle_event"] == "subagent_state_changed"
    assert status["result"]["execution_lifecycle"] == "in_turn_parent_owned"
    assert status["result"]["cancellation"] == "parent_job"
    assert status["result"]["independently_resumable"] is False

    untrusted = _send_bridge_request(
        bridge,
        out,
        "session.subagents.setEnabled",
        {"session_id": session_id, "enabled": True},
        request_id="subagent-untrusted",
    )
    assert untrusted["ok"] is False
    assert untrusted["error"]["code"] == "workspace_trust_required"

    toggled = _send_bridge_request(
        bridge,
        out,
        "session.subagents.setEnabled",
        {"session_id": session_id, "enabled": True, "workspace_trusted": True},
        request_id="subagent-toggle",
    )
    assert toggled["ok"] is True
    assert toggled["result"]["enabled"] is True
    assert toggled["result"]["changed"] is True
    assert toggled["result"]["audit"]["secret_values_included"] is False
    next_status = _send_bridge_request(
        bridge,
        out,
        "session.status",
        {"session_id": session_id},
    )
    assert next_status["result"]["subagents_enabled"] is True


def test_stdio_bridge_session_trace_methods_are_bounded_redacted_and_confirm_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    trace_artifact = artifact_root / "trace.txt"
    trace_artifact.write_text(
        "Authorization: Bearer abcdefghijklmnop\nhttps://user:pass@example.test/path\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.surface = kwargs["surface"]
            self.messages: list[dict[str, Any]] = []

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    session_id = create["result"]["session_id"]
    bridge._sessions[session_id].surface.emit_info(  # noqa: SLF001 - verifies retained event safety.
        "Authorization: Bearer abcdefghijklmnop token=must-not-leak"
    )

    status = _send_bridge_request(bridge, out, "session.trace.status", {"session_id": session_id})
    compact = _send_bridge_request(
        bridge,
        out,
        "session.trace.setLevel",
        {"session_id": session_id, "level": "compact"},
    )
    full_without_confirm = _send_bridge_request(
        bridge,
        out,
        "session.trace.setLevel",
        {"session_id": session_id, "level": "full"},
        request_id="trace-full-no-confirm",
    )
    full = _send_bridge_request(
        bridge,
        out,
        "session.trace.setLevel",
        {"session_id": session_id, "level": "full", "confirm": True},
        request_id="trace-full",
    )
    events = _send_bridge_request(
        bridge,
        out,
        "session.trace.listEvents",
        {"session_id": session_id, "max_events": 10, "max_bytes": 2048},
    )
    artifact = _send_bridge_request(
        bridge,
        out,
        "session.trace.readArtifact",
        {"session_id": session_id, "artifact_id": "session:trace.txt", "max_bytes": 1024},
    )
    cleared = _send_bridge_request(
        bridge,
        out,
        "session.trace.clear",
        {"session_id": session_id},
    )

    rendered = out.getvalue()
    retained = json.dumps(list(bridge._event_buffers[session_id]))  # noqa: SLF001
    assert status["ok"] is True
    assert status["result"]["level"] == "compact"
    assert status["result"]["secret_values_included"] is False
    assert compact["result"]["level"] == "compact"
    assert full_without_confirm["ok"] is False
    assert full_without_confirm["error"]["code"] == "confirmation_required"
    assert full["result"]["level"] == "full"
    assert full["result"]["full_trace_confirmed"] is True
    assert events["result"]["redacted"] is True
    assert events["result"]["secret_values_included"] is False
    assert events["result"]["max_events"] == 10
    assert events["result"]["max_bytes"] == 2048
    assert artifact["result"]["content"].count("<redacted>") >= 2
    assert artifact["result"]["secret_values_included"] is False
    assert cleared["result"]["cleared"] is True
    assert "abcdefghijklmnop" not in rendered
    assert "must-not-leak" not in rendered
    assert "user:pass" not in rendered
    assert "abcdefghijklmnop" not in retained
    assert "must-not-leak" not in retained


def test_stdio_bridge_session_trace_rejects_invalid_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    invalid = _send_bridge_request(
        bridge,
        out,
        "session.trace.setLevel",
        {"session_id": create["result"]["session_id"], "level": "verbose"},
        request_id="trace-invalid",
    )

    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "invalid_trace_level"


def test_stdio_bridge_terminals_unavailable_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )

    listed = _send_bridge_request(
        bridge,
        out,
        "session.terminals.list",
        {"session_id": create["result"]["session_id"]},
    )
    shown = _send_bridge_request(
        bridge,
        out,
        "session.terminals.show",
        {"session_id": create["result"]["session_id"], "process_id": "proc-1"},
    )

    assert listed["ok"] is True
    assert listed["result"]["supported"] is False
    assert listed["result"]["available"] is False
    assert listed["result"]["terminals"] == []
    assert listed["result"]["arbitrary_shell_execution"] is False
    assert shown["result"]["supported"] is False
    assert shown["result"]["lines"] == []


def test_stdio_bridge_terminals_list_show_and_kill_are_bounded_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeTerminalManager:
        def __init__(self) -> None:
            self.killed: list[str] = []

        def list(self) -> tuple[Any, ...]:
            return (
                SimpleNamespace(
                    process_id="proc-1",
                    cmd="echo TOKEN=secret-value",
                    cwd=tmp_path,
                    status="running",
                    exit_code=None,
                    runtime_s=1.25,
                    started_at_wall=1_700_000_000.0,
                ),
            )

        def read(self, process_id: str, *, since: int = 0) -> Any:
            assert process_id == "proc-1"
            assert since == 0
            return SimpleNamespace(
                process_id=process_id,
                status="running",
                exit_code=None,
                failure_reason="Authorization: Bearer abcdefghijklmnop",
                lines=(
                    SimpleNamespace(
                        seq=1,
                        stream="stdout",
                        text="hello TOKEN=secret-value\n",
                        ts=1_700_000_001.0,
                    ),
                    SimpleNamespace(seq=2, stream="stderr", text="x" * 1000, ts=1_700_000_002.0),
                ),
                next_seq=2,
                dropped_lines=3,
                started_at_wall=1_700_000_000.0,
                runtime_s=2.5,
                total_bytes=2000,
            )

        def kill(self, process_id: str) -> Any:
            self.killed.append(process_id)
            return self.read(process_id, since=0)

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []
            self.terminal_manager = FakeTerminalManager()

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    session_id = create["result"]["session_id"]

    listed = _send_bridge_request(
        bridge,
        out,
        "session.terminals.list",
        {"session_id": session_id},
    )
    shown = _send_bridge_request(
        bridge,
        out,
        "session.terminals.show",
        {"session_id": session_id, "process_id": "proc-1", "max_lines": 1, "max_bytes": 64},
    )
    killed = _send_bridge_request(
        bridge,
        out,
        "session.terminals.kill",
        {
            "session_id": session_id,
            "process_id": "proc-1",
            "workspace_trusted": True,
            "confirm": True,
        },
    )

    rendered = out.getvalue()
    assert listed["result"]["supported"] is True
    assert listed["result"]["terminals"][0]["cmd"] == "echo TOKEN=<redacted>"
    assert shown["result"]["line_count"] == 1
    assert shown["result"]["truncated"] is True
    assert shown["result"]["dropped_lines"] == 3
    assert shown["result"]["failure_reason"] == "Authorization: <redacted>"
    assert killed["result"]["killed"] is True
    assert killed["result"]["secret_values_included"] is False
    assert "secret-value" not in rendered
    assert "abcdefghijklmnop" not in rendered


@pytest.mark.parametrize("method", ["session.terminals.kill", "session.terminals.clear"])
def test_stdio_bridge_terminal_mutations_require_workspace_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    untrusted = _send_bridge_request(
        bridge,
        out,
        method,
        {"session_id": create["result"]["session_id"], "process_id": "proc-1"},
        request_id=f"{method}-untrusted",
    )

    assert untrusted["ok"] is False
    assert untrusted["error"]["code"] == "workspace_trust_required"


def test_stdio_bridge_terminal_clear_returns_unsupported_without_manager_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeTerminalManager:
        def list(self) -> tuple[Any, ...]:
            return ()

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []
            self.terminal_manager = FakeTerminalManager()

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    result = _send_bridge_request(
        bridge,
        out,
        "session.terminals.clear",
        {
            "session_id": create["result"]["session_id"],
            "process_id": "proc-1",
            "workspace_trusted": True,
            "confirm": True,
        },
    )

    assert result["ok"] is True
    assert result["result"]["supported"] is False
    assert result["result"]["cleared"] is False


def test_stdio_bridge_session_compact_uses_live_conversation_compactor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeCompactor:
        state = SimpleNamespace(history_chunk_index=0, pins=[])

        def compact_now(self, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
            assert kwargs["focus"] == "bugs"
            self.state.history_chunk_index = 1
            return [{"role": "user", "content": "summary"}], True

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.client = SimpleNamespace(model="test-model")
            self.messages = [
                {"role": "user", "content": "first long message"},
                {"role": "assistant", "content": "second long message"},
            ]
            self.tool_list: list[dict[str, Any]] = []
            self.conversation_compactor = FakeCompactor()

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    compact = _send_bridge_request(
        bridge,
        out,
        "session.compact",
        {"session_id": create["result"]["session_id"], "focus": "bugs"},
    )

    assert compact["ok"] is True
    assert compact["result"]["supported"] is True
    assert compact["result"]["changed"] is True
    assert compact["result"]["tokens_before"] > compact["result"]["tokens_after"]
    assert compact["result"]["tokens_delta"] < 0
    assert compact["result"]["chunks_before"] == 0
    assert compact["result"]["chunks_after"] == 1


def test_stdio_bridge_session_compact_reports_unsupported_or_noop_honestly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class NoopCompactor:
        state = SimpleNamespace(history_chunk_index=0, pins=[])

        def compact_now(self, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
            return kwargs["messages"], True

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.client = SimpleNamespace(model="test-model")
            self.messages = [{"role": "user", "content": "unchanged"}]
            self.tool_list: list[dict[str, Any]] = []
            self.conversation_compactor = None

        def close(self) -> None:
            pass

    created_sessions: list[FakeSession] = []

    def fake_create_session(**kwargs: Any) -> FakeSession:
        session = FakeSession(**kwargs)
        created_sessions.append(session)
        return session

    bridge = StdioBridge(stdout=out, create_session_fn=fake_create_session)
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    unsupported = _send_bridge_request(
        bridge,
        out,
        "session.compact",
        {"session_id": create["result"]["session_id"]},
    )
    created_sessions[0].conversation_compactor = NoopCompactor()
    noop = _send_bridge_request(
        bridge,
        out,
        "session.compact",
        {"session_id": create["result"]["session_id"]},
    )

    assert unsupported["result"]["supported"] is False
    assert unsupported["result"]["changed"] is False
    assert unsupported["result"]["tokens_delta"] == 0
    assert unsupported["result"]["source"] == "tokenizer_estimate"
    assert unsupported["result"]["approximate"] is True
    assert noop["result"]["supported"] is True
    assert noop["result"]["changed"] is False
    assert noop["result"]["tokens_delta"] == 0


def test_stdio_bridge_session_compact_redacts_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FailingCompactor:
        state = SimpleNamespace(history_chunk_index=0, pins=[])

        def compact_now(self, **_kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
            raise RuntimeError("api_key=must-not-leak")

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.client = SimpleNamespace(model="test-model")
            self.messages = [{"role": "user", "content": "unchanged"}]
            self.tool_list: list[dict[str, Any]] = []
            self.conversation_compactor = FailingCompactor()

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    compact = _send_bridge_request(
        bridge,
        out,
        "session.compact",
        {"session_id": create["result"]["session_id"]},
    )

    assert compact["ok"] is False
    assert compact["error"]["code"] == "session_compaction_failed"
    assert "must-not-leak" not in out.getvalue()
    assert "api_key=<redacted>" in compact["error"]["message"]


@pytest.mark.parametrize(
    "git_available, checkpoint_state_exists", [(True, False), (False, False), (False, True)]
)
def test_stdio_bridge_session_resume_replays_bounded_redacted_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_available: bool,
    checkpoint_state_exists: bool,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    if not git_available:
        original_which = stdio_bridge.shutil.which
        monkeypatch.setattr(
            stdio_bridge.shutil,
            "which",
            lambda command: None if command == "git" else original_which(command),
        )
        monkeypatch.setattr(
            stdio_bridge, "checkpoint_storage_exists", lambda root: checkpoint_state_exists
        )
        if checkpoint_state_exists:

            def unavailable_checkpoints(*args: Any) -> None:
                raise stdio_bridge.ChangeLedgerError("Checkpoint storage could not start Git.")

            monkeypatch.setattr(StdioBridge, "_change_ledger_for_session", unavailable_checkpoints)
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    sessions_dir = tmp_path / "sessions"
    artifact_root.mkdir()
    sessions_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", session_log_dir=os.fspath(sessions_dir)),
    )

    retained_path = sessions_dir / "retained.jsonl"
    retained_events = [
        {
            "type": "user_message",
            "payload": {"content": "first secret_token=must-not-leak"},
        },
        {"type": "assistant_message", "payload": {"content": "assistant kept"}},
        {"type": "user_message", "payload": {"content": "last message"}},
    ]
    retained_path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in retained_events) + "\n",
        encoding="utf-8",
    )

    FakeStore = type(
        "FakeStore",
        (),
        {"session_artifact_root": artifact_root, "sessions_dir": sessions_dir},
    )

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []
            self.pinned_prefix_len = 0
            self.conversation_compactor = SimpleNamespace(
                state=SimpleNamespace(pinned_prefix_len=0)
            )

        def close(self) -> None:
            pass

    created_sessions: list[FakeSession] = []

    def fake_create_session(**kwargs: Any) -> FakeSession:
        session = FakeSession(**kwargs)
        created_sessions.append(session)
        return session

    bridge = StdioBridge(stdout=out, create_session_fn=fake_create_session)
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    resume = _send_bridge_request(
        bridge,
        out,
        "session.resume",
        {
            "session_id": create["result"]["session_id"],
            "target_session_id": "retained",
            "max_messages": 3,
        },
    )

    if checkpoint_state_exists and not git_available:
        assert resume["ok"] is False
        assert resume["error"]["code"] == "checkpoint_recovery_failed"
        assert created_sessions[0].messages == []
        return
    assert resume["ok"] is True
    assert resume["result"]["resumed"] is True
    assert resume["result"]["task_state_recovery"] == "not_restored"
    assert resume["result"]["task_id"] is None
    assert resume["result"]["history_count"] == 3
    assert resume["result"]["history_count_total"] == 3
    assert resume["result"]["bounded"] is False
    assert resume["result"]["resume_context_loaded"] is True
    if not git_available:
        assert resume["result"]["checkpoint_lineage_adopted"] is False
        assert "file checkpoints need Git" in out.getvalue()
    assert "must-not-leak" not in out.getvalue()
    assert all(
        "must-not-leak" not in json.dumps(message) for message in created_sessions[0].messages
    )


def test_stdio_bridge_session_resume_validates_ids_and_bounds_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    sessions_dir = tmp_path / "sessions"
    artifact_root.mkdir()
    sessions_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", session_log_dir=os.fspath(sessions_dir)),
    )

    retained_path = sessions_dir / "retained_many.jsonl"
    retained_events = [
        {
            "type": "user_message",
            "payload": {"content": f"message {index} Bearer token-value-{index:02d}-must-not-leak"},
        }
        for index in range(5)
    ]
    retained_path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in retained_events) + "\n",
        encoding="utf-8",
    )

    FakeStore = type(
        "FakeStore",
        (),
        {"session_artifact_root": artifact_root, "sessions_dir": sessions_dir},
    )

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []
            self.pinned_prefix_len = 0
            self.conversation_compactor = SimpleNamespace(
                state=SimpleNamespace(pinned_prefix_len=0)
            )

        def close(self) -> None:
            pass

    created_sessions: list[FakeSession] = []

    def fake_create_session(**kwargs: Any) -> FakeSession:
        session = FakeSession(**kwargs)
        created_sessions.append(session)
        return session

    bridge = StdioBridge(stdout=out, create_session_fn=fake_create_session)
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    invalid = _send_bridge_request(
        bridge,
        out,
        "session.resume",
        {
            "session_id": create["result"]["session_id"],
            "target_session_id": "../retained_many",
        },
        request_id="resume-invalid",
    )
    bounded = _send_bridge_request(
        bridge,
        out,
        "session.resume",
        {
            "session_id": create["result"]["session_id"],
            "target_session_id": "retained_many",
            "max_messages": 2,
        },
        request_id="resume-bounded",
    )

    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "invalid_session_id"
    assert bounded["ok"] is True
    assert bounded["result"]["history_count"] == 2
    assert bounded["result"]["history_count_total"] == 5
    assert bounded["result"]["bounded"] is True
    assert bounded["result"]["max_messages"] == 2
    rendered = json.dumps(created_sessions[0].messages)
    assert "message 0" not in rendered
    assert "message 3" in rendered
    assert "message 4" in rendered
    assert "must-not-leak" not in rendered
    assert "must-not-leak" not in out.getvalue()


def test_stdio_bridge_and_terminal_resume_share_context_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    sessions_dir = tmp_path / "sessions"
    artifact_root.mkdir()
    sessions_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", session_log_dir=os.fspath(sessions_dir)),
    )

    from alysis_code import cli as cli_mod

    target_id = "retained_parity"
    retained_events = [
        {
            "type": "session_start",
            "payload": {"mode": "review", "active_workdir_relpath": "."},
        },
        {"type": "user_message", "payload": {"content": "parity turn"}},
        {"type": "assistant_message", "payload": {"content": "parity reply"}},
    ]
    (sessions_dir / f"{target_id}.jsonl").write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in retained_events) + "\n",
        encoding="utf-8",
    )

    # IDE bridge resume.
    FakeStore = type(
        "FakeStore",
        (),
        {"session_artifact_root": artifact_root, "sessions_dir": sessions_dir},
    )

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []
            self.pinned_prefix_len = 0
            self.conversation_compactor = SimpleNamespace(
                state=SimpleNamespace(pinned_prefix_len=0)
            )

        def close(self) -> None:
            pass

    created_sessions: list[FakeSession] = []

    def fake_create_session(**kwargs: Any) -> FakeSession:
        session = FakeSession(**kwargs)
        created_sessions.append(session)
        return session

    bridge = StdioBridge(stdout=out, create_session_fn=fake_create_session)
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    resume = _send_bridge_request(
        bridge,
        out,
        "session.resume",
        {
            "session_id": create["result"]["session_id"],
            "target_session_id": target_id,
        },
    )
    assert resume["ok"] is True
    assert resume["result"]["bounded"] is False
    bridge_context = str(created_sessions[0].messages[0].get("content"))

    # Terminal resume against the same fixture.
    class _TerminalStore:
        def __init__(self, *, session_id: str) -> None:
            self.sessions_dir = sessions_dir
            self.session_id = session_id
            self.notes: list[tuple[str, dict[str, Any]]] = []

        def append(self, event_type: str, payload: dict[str, Any]) -> None:
            self.notes.append((event_type, payload))

    class _TerminalSession:
        pass

    current = _TerminalSession()
    current.cfg = AppConfig(model="test-model", max_steps=10)
    current.root = tmp_path
    current.mode = "review"
    current.yes = True
    current.max_steps = 5
    current.console = None
    current.surface = object()
    current.store = _TerminalStore(session_id="current-terminal")
    current.client = SimpleNamespace(api_key="override-key")
    current.usage_role = "main"
    current.tool_output_offloader = None
    current.conversation_compactor = None
    current.messages = []
    current.close = lambda: None

    def fake_terminal_create_session(**kwargs: Any) -> Any:
        session = _TerminalSession()
        session.cfg = kwargs["cfg"]
        session.root = kwargs["root"]
        session.mode = kwargs["mode"]
        session.yes = kwargs["yes"]
        session.max_steps = kwargs["max_steps"]
        session.console = kwargs["console"]
        session.surface = kwargs["surface"]
        session.store = _TerminalStore(session_id=kwargs["session_id_override"])
        session.client = SimpleNamespace(api_key="override-key")
        session.usage_role = kwargs["usage_role"]
        session.tool_output_offloader = None
        session.conversation_compactor = None
        session.messages = []
        session.pinned_prefix_len = 0
        return session

    monkeypatch.setattr(cli_mod, "create_session", fake_terminal_create_session)

    ok, _message, _history = cli_mod._resume_chat_session(
        session=current,
        target_session_id=target_id,
    )

    assert ok is True
    terminal_context = str(current.messages[0].get("content"))
    assert terminal_context.startswith("<resume_context>")
    assert terminal_context == bridge_context


def test_stdio_bridge_session_history_redacts_live_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages = [
                {"role": "user", "content": "debug secret_token=must-not-leak"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Authorization: Bearer nested-token-must-not-leak",
                        }
                    ],
                },
                {"role": "user", "content": "debug second match with api_key=also-hidden"},
            ]

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    history = _send_bridge_request(
        bridge,
        out,
        "session.history",
        {
            "session_id": create["result"]["session_id"],
            "pattern": "debug",
            "max_results": 1,
            "max_text_chars": 80,
        },
    )
    bearer_history = _send_bridge_request(
        bridge,
        out,
        "session.history",
        {"session_id": create["result"]["session_id"], "pattern": "bearer"},
    )
    capped_history = _send_bridge_request(
        bridge,
        out,
        "session.history",
        {
            "session_id": create["result"]["session_id"],
            "pattern": "second",
            "max_text_chars": 12,
        },
    )

    assert history["ok"] is True
    assert history["result"]["redacted"] is True
    assert history["result"]["secret_values_included"] is False
    assert history["result"]["max_results"] == 1
    assert history["result"]["max_text_chars"] == 80
    assert history["result"]["truncated"] is True
    assert history["result"]["scanned_count"] == 1
    assert history["result"]["matches"][0]["text"] == "debug secret_token=<redacted>"
    assert history["result"]["matches"][0]["text_truncated"] is False
    assert bearer_history["result"]["matches"][0]["text"] == (
        '[{"text": "Authorization: <redacted>", "type": "text"}]'
    )
    assert capped_history["result"]["matches"][0]["text"] == "debug second"
    assert capped_history["result"]["matches"][0]["text_truncated"] is True
    assert "must-not-leak" not in out.getvalue()
    assert "also-hidden" not in out.getvalue()


def test_stdio_bridge_session_image_basket_uses_safe_paths_and_clears_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    image = tmp_path / "ok.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    bad_type = tmp_path / "bad.txt"
    bad_type.write_text("not image", encoding="utf-8")
    symlink = tmp_path / "linked.png"
    if hasattr(os, "symlink"):
        _symlink_or_skip(symlink, outside, target_is_directory=False)
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    turns: list[list[str]] = []

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.messages: list[dict[str, Any]] = []
            self.surface = kwargs["surface"]

        def run_turn(self, message: str, image_paths: list[str] | None = None) -> int:
            _ = message
            turns.append(list(image_paths or []))
            self.surface.emit_message_end("done")
            return 0

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly"},
    )
    session_id = create["result"]["session_id"]

    for request_id, candidate, expected_code in (
        ("outside", outside, "image_path_outside_workspace"),
        ("missing", tmp_path / "missing.png", "image_not_found"),
        ("bad-type", bad_type, "unsupported_image_type"),
    ):
        response = _send_bridge_request(
            bridge,
            out,
            "session.images.add",
            {"session_id": session_id, "images": [os.fspath(candidate)]},
            request_id=request_id,
        )
        assert response["ok"] is False
        assert response["error"]["code"] == expected_code
    if symlink.exists() or symlink.is_symlink():
        response = _send_bridge_request(
            bridge,
            out,
            "session.images.add",
            {"session_id": session_id, "images": [os.fspath(symlink)]},
            request_id="symlink",
        )
        assert response["ok"] is False
        assert response["error"]["code"] == "image_symlink_rejected"

    added = _send_bridge_request(
        bridge,
        out,
        "session.images.add",
        {"session_id": session_id, "images": ["ok.png"]},
    )
    assert added["ok"] is True
    assert added["result"]["count"] == 1
    assert added["result"]["images"][0]["relpath"] == "ok.png"

    bridge.process_line(
        _request(
            "chat.send",
            {"session_id": session_id, "message": "describe"},
            request_id="chat",
        )
        + "\n"
    )
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and str(line.get("payload", {}).get("message", "")).startswith("job_completed ")
        ),
    )
    assert turns == [[os.fspath(image.resolve())]]

    _wait_for_job_workers(bridge)
    listed = _send_bridge_request(bridge, out, "session.images.list", {"session_id": session_id})
    assert listed["result"]["count"] == 0
    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    assert status["result"]["active_job"] is None
    assert status["result"]["last_job"]["status"] == "completed"


def test_stdio_bridge_live_settings_affect_next_chat_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="first-model"))
    observed: list[tuple[str, bool, str]] = []

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.mode = kwargs["mode"]
            self.surface = kwargs["surface"]
            self.messages: list[dict[str, Any]] = []

        def run_turn(self, message: str) -> int:
            _ = message
            observed.append((self.cfg.model, self.cfg.stream, self.mode))
            self.surface.emit_message_end("done")
            return 0

        def close(self) -> None:
            pass

    def fake_refresh(agent_session: Any, cfg: AppConfig) -> None:
        agent_session.cfg = cfg

    def fake_mode(agent_session: Any, mode: str) -> None:
        agent_session.mode = mode

    monkeypatch.setattr(stdio_bridge, "_refresh_agent_session_config", fake_refresh)
    monkeypatch.setattr(stdio_bridge, "_apply_agent_session_mode", fake_mode)
    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "review"},
    )
    session_id = create["result"]["session_id"]
    _send_bridge_request(
        bridge, out, "session.setModel", {"session_id": session_id, "model": "next-model"}
    )
    _send_bridge_request(
        bridge, out, "session.setStream", {"session_id": session_id, "stream": False}
    )
    _send_bridge_request(
        bridge, out, "session.setMode", {"session_id": session_id, "mode": "readonly"}
    )
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "go"}, request_id="chat") + "\n"
    )
    _wait_for_line(out, lambda line: line.get("type") == "message_end")

    assert observed == [("next-model", False, "readonly")]


def test_stdio_bridge_run_start_cleans_created_session_on_turn_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="default-model", stream=True),
    )
    closed: list[bool] = []

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.mode = kwargs["mode"]
            self.surface = kwargs["surface"]
            self.messages: list[dict[str, Any]] = []

        def close(self) -> None:
            closed.append(True)

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: FakeSession(**kwargs),
    )
    bridge.process_line(
        _request(
            "run.start",
            {"workspace": os.fspath(tmp_path), "mode": "readonly"},
            request_id="run-missing-message",
        )
        + "\n"
    )

    response = _json_lines(out)[0]
    assert response["ok"] is False
    assert response["error"]["code"] == "missing_field"
    assert closed == [True]
    assert bridge._sessions == {}


def test_stdio_bridge_run_start_rejects_existing_session_create_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _bridge, session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        lambda _surface, _message: 0,
    )
    bridge = _bridge

    bridge.process_line(
        _request(
            "run.start",
            {
                "session_id": session_id,
                "model": "ignored-model",
                "message": "hello",
            },
            request_id="run-existing",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "run-existing"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "unsupported_turn_option"


def test_run_start_retry_reuses_workspace_scoped_durable_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", subagents_enabled=False),
    )
    queue_path = tmp_path / "data" / "run-start.sqlite3"
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class FakeStore:
        session_artifact_root = tmp_path / "session-artifacts"

    class FakeSession:
        store = FakeStore()

        def __init__(self, surface: Any) -> None:
            self.surface = surface
            self.messages: list[dict[str, Any]] = []

        def run_turn(self, message: str) -> int:
            calls.append(message)
            entered.set()
            assert release.wait(timeout=5.0)
            return 0

        def close(self) -> None:
            return

    def make_bridge(output: io.StringIO) -> StdioBridge:
        return StdioBridge(
            stdout=output,
            create_session_fn=lambda **kwargs: FakeSession(kwargs["surface"]),
            prompt_queue=DurablePromptQueue(queue_path),
        )

    payload = {
        "workspace": os.fspath(tmp_path),
        "mode": "review",
        "instruction": "perform the one accepted task",
        "idempotency_key": "stable-webview-request",
    }
    first_out = io.StringIO()
    first_bridge = make_bridge(first_out)
    first = _send_bridge_request(first_bridge, first_out, "run.start", payload, request_id="first")
    assert first["ok"] is True
    assert first["result"]["session_id"].startswith("ide-start-")
    assert entered.wait(timeout=2.0)

    # Model an extension-host replacement which did not retain the first
    # acknowledgement. The second bridge must attach to the same durable row,
    # not execute the accepted prompt again.
    second_out = io.StringIO()
    second_bridge = make_bridge(second_out)
    second = _send_bridge_request(
        second_bridge, second_out, "run.start", payload, request_id="retry"
    )
    assert second["ok"] is True
    assert second["result"]["session_id"] == first["result"]["session_id"]
    assert second["result"]["job_id"] == first["result"]["job_id"]
    assert calls == ["perform the one accepted task"]

    release.set()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        first_done = first_bridge._jobs[first["result"]["job_id"]].status == "completed"
        second_done = second_bridge._jobs[second["result"]["job_id"]].status == "completed"
        if first_done and second_done:
            break
        time.sleep(0.02)
    assert first_bridge._jobs[first["result"]["job_id"]].status == "completed"
    assert second_bridge._jobs[second["result"]["job_id"]].status == "completed"
    assert calls == ["perform the one accepted task"]
    first_bridge.close()
    second_bridge.close()


def test_run_start_idempotency_key_rejects_changed_initial_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, _session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        lambda _surface, _message: 0,
    )
    base = {
        "workspace": os.fspath(tmp_path),
        "mode": "review",
        "idempotency_key": "same-initial-request",
    }
    first = _send_bridge_request(
        bridge,
        out,
        "run.start",
        {**base, "instruction": "first payload"},
        request_id="initial-one",
    )
    assert first["ok"] is True
    # Test the durable payload collision after the original worker retires;
    # while it is still settling the admission fence correctly returns busy.
    first_job = bridge._jobs[first["result"]["job_id"]]
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and f"job_completed {first_job.job_id}"
            in str(line.get("payload", {}).get("message", ""))
        ),
    )
    assert first_job.thread is not None
    first_job.thread.join(timeout=2.0)
    assert not first_job.thread.is_alive()
    bridge.process_line(
        _request(
            "run.start",
            {**base, "instruction": "different payload"},
            request_id="initial-conflict",
        )
        + "\n"
    )
    changed = _response_by_id(out, "initial-conflict")
    assert changed["ok"] is False
    assert changed["error"]["code"] == "prompt_queue_error"
    assert "different payload" not in repr(changed)


def test_stdio_bridge_live_session_setters_refresh_underlying_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="default-model", stream=True),
    )
    applied_modes: list[str] = []
    refreshed: list[tuple[str, bool]] = []
    created_sessions: list[Any] = []

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.mode = kwargs["mode"]
            self.surface = kwargs["surface"]
            self.messages: list[dict[str, Any]] = []

        def close(self) -> None:
            pass

    def fake_apply_mode(agent_session: Any, mode: str) -> None:
        agent_session.mode = mode
        applied_modes.append(mode)

    def fake_refresh_config(agent_session: Any, cfg: AppConfig) -> None:
        agent_session.cfg = cfg
        refreshed.append((cfg.model, cfg.stream))

    monkeypatch.setattr(stdio_bridge, "_apply_agent_session_mode", fake_apply_mode)
    monkeypatch.setattr(stdio_bridge, "_refresh_agent_session_config", fake_refresh_config)

    def fake_create_session(**kwargs: Any) -> FakeSession:
        session = FakeSession(**kwargs)
        created_sessions.append(session)
        return session

    bridge = StdioBridge(stdout=out, create_session_fn=fake_create_session)
    bridge.process_line(
        _request(
            "session.create",
            {"workspace": os.fspath(tmp_path), "mode": "review"},
            request_id="create",
        )
        + "\n"
    )
    session_id = _json_lines(out)[0]["result"]["session_id"]
    bridge.process_line(
        _request(
            "session.setMode",
            {"session_id": session_id, "mode": "readonly"},
            request_id="set-mode",
        )
        + "\n"
    )
    bridge.process_line(
        _request(
            "session.setModel",
            {"session_id": session_id, "model": "next-model"},
            request_id="set-model",
        )
        + "\n"
    )
    bridge.process_line(
        _request(
            "session.setStream",
            {"session_id": session_id, "stream": False},
            request_id="set-stream",
        )
        + "\n"
    )

    by_id = {payload["id"]: payload for payload in _json_lines(out) if "id" in payload}
    assert by_id["set-mode"]["result"]["mode"] == "readonly"
    assert applied_modes == ["readonly"]
    assert created_sessions[0].mode == "readonly"
    assert refreshed == [("next-model", True), ("next-model", False)]


def test_stdio_bridge_set_mode_rejects_an_active_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()

    def run_turn(_surface: Any, _message: str) -> int:
        started.set()
        assert release.wait(timeout=3.0)
        return 0

    out, bridge, session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        run_turn,
    )
    bridge.process_line(
        _request(
            "chat.send",
            {"session_id": session_id, "message": "work"},
            request_id="chat",
        )
        + "\n"
    )
    assert started.wait(timeout=1.0)

    bridge.process_line(
        _request(
            "session.setMode",
            {"session_id": session_id, "mode": "auto"},
            request_id="set-mode-busy",
        )
        + "\n"
    )

    response = _response_by_id(out, "set-mode-busy")
    assert response["ok"] is False
    assert response["error"]["code"] == "session_busy"
    assert bridge._sessions[session_id].mode == "review"

    release.set()
    bridge.close()


def test_stdio_bridge_management_config_profile_do_not_leak_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "profile.add",
            {
                "workspace_trusted": True,
                "name": "demo",
                "base_url": "https://provider.example/v1",
                "api_key_env": "DEMO_API_KEY",
                "extra_headers": {"X-Demo": "header-value"},
            },
            request_id="profile-add",
        )
        + "\n"
    )
    bridge.process_line(
        _request(
            "profile.add",
            {
                "workspace_trusted": True,
                "name": "demo",
                "base_url": "https://provider.example/v1",
                "api_key_env": "DEMO_API_KEY",
            },
            request_id="profile-add-safe",
        )
        + "\n"
    )
    bridge.process_line(_request("profile.list", request_id="profile-list") + "\n")
    bridge.process_line(_request("config.get", request_id="config-get") + "\n")

    text = out.getvalue()
    payloads = _json_lines(out)
    assert [payload["ok"] for payload in payloads] == [True, True, True, True]
    assert "header-value" not in text
    assert payloads[0]["result"]["changed"] is False
    assert payloads[0]["result"]["action"]["kind"] == "requires_secret_storage"
    assert payloads[0]["result"]["action"]["header_names"] == ["X-Demo"]
    assert payloads[1]["result"]["profile"]["key_env_var"] == "DEMO_API_KEY"
    assert payloads[1]["result"]["profile"]["api_key"]["present"] is False
    assert payloads[2]["result"]["profiles"][1]["extra_headers"]["values_redacted"] is True
    config_profiles = payloads[3]["result"]["config"]["extra_fields"]["profiles"]
    assert config_profiles["demo"]["extra_headers"]["values_redacted"] is True


def test_stdio_bridge_profile_preset_rejects_custom_base_url_userinfo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "profile.preset",
            {
                "workspace_trusted": True,
                "preset": "custom",
                "name": "custom",
                "base_url": "https://user:pass@example.test/v1",
                "yes": True,
            },
            request_id="profile-preset-userinfo",
        )
        + "\n"
    )

    text = out.getvalue()
    payload = _json_lines(out)[0]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_base_url"
    assert "user:pass" not in text


def test_stdio_bridge_update_check_defaults_to_cached_until_explicit_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    calls: list[str] = []

    monkeypatch.setattr(
        management_protocol,
        "status_from_cache",
        lambda **_kwargs: (
            calls.append("cache") or SimpleNamespace(to_json=lambda: {"state": "cached"})
        ),
    )
    monkeypatch.setattr(
        management_protocol,
        "check_for_updates",
        lambda **_kwargs: (
            calls.append("network") or SimpleNamespace(to_json=lambda: {"state": "fresh"})
        ),
    )

    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    default = _send_bridge_request(bridge, out, "update.check", {}, request_id="default")
    explicit = _send_bridge_request(
        bridge,
        out,
        "update.check",
        {"allow_network": True},
        request_id="network",
    )
    forced = _send_bridge_request(
        bridge,
        out,
        "update.check",
        {"force": True},
        request_id="force",
    )

    assert calls == ["cache", "network", "network"]
    assert default["result"]["status"]["state"] == "cached"
    assert default["result"]["network_used"] is False
    assert explicit["result"]["status"]["state"] == "fresh"
    assert explicit["result"]["network_used"] is True
    assert forced["result"]["network_used"] is True


def test_stdio_bridge_management_retained_session_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    sessions_dir = Path(os.environ["ALYSIS_DATA_DIR"]) / "sessions"
    sessions_dir.mkdir(parents=True)
    session_id = "20260602T000000Z_test1234"
    (sessions_dir / f"{session_id}.jsonl").write_text(
        json.dumps(
            {
                "type": "session_start",
                "session_id": session_id,
                "payload": {"stdout": "Authorization: Bearer abcdefghijklmnop"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "llm_usage",
                "session_id": session_id,
                "payload": {
                    "event_type": "llm_usage",
                    "timestamp": "2026-06-02T00:00:00Z",
                    "role": "main",
                    "requested_model": "test-model",
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "total_tokens": 7,
                    "usage_source": "api",
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "debug",
                "session_id": session_id,
                "payload": {"stdout": "Authorization: Bearer abcdefghijklmnop"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "session.show",
            {"session_id": session_id, "max_events": 2, "max_total_bytes": 10_000},
            request_id="show",
        )
        + "\n"
    )
    bridge.process_line(
        _request("session.usage", {"session_id": session_id}, request_id="usage") + "\n"
    )
    bridge.process_line(
        _request("session.score", {"session_id": session_id}, request_id="score") + "\n"
    )

    by_id = {payload["id"]: payload for payload in _json_lines(out)}
    show = by_id["show"]["result"]
    assert show["event_count"] == 3
    assert show["returned_event_count"] == 2
    assert show["truncated"] is True
    assert show["truncated_by_events"] is True
    assert show["secret_values_included"] is False
    assert show["redacted"] is True
    assert "abcdefghijklmnop" not in out.getvalue()
    assert by_id["usage"]["result"]["call_count"] == 1
    assert by_id["usage"]["result"]["totals"]["total_tokens"] == 7
    assert by_id["score"]["result"]["score"]["session_id"] == session_id


def test_stdio_bridge_doctor_bundle_redacts_provider_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    class FakeDiagnostics:
        def rows(self) -> tuple[tuple[str, str], ...]:
            return (
                ("profile", "default"),
                ("notes", "Authorization: Bearer abcdefghijklmnop"),
            )

    monkeypatch.setattr(
        management_protocol, "build_provider_diagnostics", lambda _cfg: FakeDiagnostics()
    )

    bridge.process_line(_request("doctor.bundle", request_id="bundle") + "\n")

    text = out.getvalue()
    payload = _json_lines(out)[0]
    assert payload["ok"] is True
    assert payload["result"]["bundle"]["redacted"] is True
    assert "abcdefghijklmnop" not in text


def test_stdio_bridge_rejects_invalid_client_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "session_id": "../bad",
            },
            request_id="bad-session",
        )
        + "\n"
    )

    payload = _json_lines(out)[0]
    assert payload["id"] == "bad-session"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_session_id"


def test_stdio_bridge_rejects_duplicate_session_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = io.StringIO()
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    class FakeStore:
        session_artifact_root = tmp_path / "session-artifacts"

    class FakeSession:
        store = FakeStore()

        def close(self) -> None:
            pass

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession())
    params = {
        "workspace": os.fspath(tmp_path),
        "mode": "readonly",
        "model": "test-model",
        "session_id": "fixed-session",
    }

    bridge.process_line(_request("session.create", params, request_id="first") + "\n")
    bridge.process_line(_request("session.create", params, request_id="second") + "\n")

    lines = _json_lines(out)
    assert lines[0]["ok"] is True
    assert lines[1]["ok"] is False
    assert lines[1]["error"]["code"] == "duplicate_session_id"


def test_stdio_bridge_approval_respond_allow_unblocks_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions: list[Any] = []

    def run_turn(surface: Any, message: str) -> int:
        decision = surface.request_approval(
            ApprovalRequest(
                kind="shell_run",
                reason="run shell",
                preview="echo hi",
                command="echo hi",
            )
        )
        decisions.append(decision)
        surface.emit_message_end(f"done {message}")
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, run_turn)
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "approve"}, request_id="chat")
        + "\n"
    )
    approval = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )
    approval_id = approval["payload"]["approval_id"]

    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": approval_id,
                "allow": True,
                "allow_for_session": False,
            },
            request_id="approve",
        )
        + "\n"
    )

    _wait_for_line(out, lambda line: line.get("type") == "message_end")
    response = [line for line in _json_lines(out) if line.get("id") == "approve"][0]
    assert response["ok"] is True
    assert response["result"]["status"] == "applied"
    assert response["result"]["allow"] is True
    assert response["result"]["allow_for_session"] is False
    assert decisions[0].allow is True


def test_stdio_bridge_approval_allow_for_session_exact_shell_scope_auto_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions: list[tuple[bool, bool]] = []

    def run_turn(surface: Any, message: str) -> int:
        _ = message
        request = ApprovalRequest(
            kind="shell_run",
            reason="run shell",
            preview="echo hi",
            command="echo hi",
        )
        first = surface.request_approval(request)
        decisions.append((first.allow, first.allow_for_session))
        second = surface.request_approval(request)
        decisions.append((second.allow, second.allow_for_session))
        surface.emit_message_end("done")
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, run_turn)
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "approve"}, request_id="chat")
        + "\n"
    )
    approval = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )
    assert approval["payload"]["approval_kind"] == "shell_run"
    assert approval["payload"]["scope"]["type"] == "exact_command_hash"
    assert approval["payload"]["allow_for_session_supported"] is True

    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": approval["payload"]["approval_id"],
                "allow": True,
                "allow_for_session": True,
            },
            request_id="approve",
        )
        + "\n"
    )

    _wait_for_line(out, lambda line: line.get("type") == "message_end")
    lines = _json_lines(out)
    response = [line for line in lines if line.get("id") == "approve"][0]
    assert response["result"]["allow_for_session_supported"] is True
    assert response["result"]["allow_for_session"] is True
    assert response["result"]["allow_for_session_scope"]["type"] == "exact_command_hash"
    approval_events = [
        line
        for line in lines
        if line.get("type") == "prompt_for_input"
        and line.get("payload", {}).get("kind") == "approval"
    ]
    assert len(approval_events) == 1
    assert any(
        line.get("type") == "info_emitted"
        and "approval_auto_allowed" in str(line.get("payload", {}).get("message", ""))
        for line in lines
    )
    assert decisions == [(True, True), (True, True)]


def _mcp_tool_approval_request(text: str, *, destructive: bool = False) -> ApprovalRequest:
    return ApprovalRequest(
        kind="mcp_tool_run:alpha/echo",
        reason="review mode requires confirmation for MCP tools",
        preview=f'Run MCP tool\nserver: alpha\ntool: echo\nargs:\n{{"text": "{text}"}}',
        command="mcp__alpha__echo",
        metadata={
            "mcp_server_id": "alpha",
            "mcp_tool_id": "echo",
            **({"destructive": True} if destructive else {}),
        },
    )


def test_stdio_bridge_mcp_tool_allow_for_session_covers_the_tool_for_any_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions: list[tuple[bool, bool]] = []

    def run_turn(surface: Any, message: str) -> int:
        _ = message
        for text in ("one", "two"):
            decision = surface.request_approval(_mcp_tool_approval_request(text))
            decisions.append((decision.allow, decision.allow_for_session))
        surface.emit_message_end("done")
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, run_turn)
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "approve"}, request_id="chat")
        + "\n"
    )
    approval = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )
    payload = approval["payload"]
    assert payload["approval_kind"] == "mcp_tool_run:alpha/echo"
    assert payload["allow_for_session_supported"] is True
    assert payload["action"]["tool_identity"] == {"mcp_server_id": "alpha", "mcp_tool_id": "echo"}
    assert payload["action"]["access"] == "execute"

    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": payload["approval_id"],
                "allow": True,
                "allow_for_session": True,
            },
            request_id="approve",
        )
        + "\n"
    )

    _wait_for_line(out, lambda line: line.get("type") == "message_end")
    approval_events = [
        line
        for line in _json_lines(out)
        if line.get("type") == "prompt_for_input"
        and line.get("payload", {}).get("kind") == "approval"
    ]
    assert len(approval_events) == 1
    assert decisions == [(True, True), (True, True)]


def test_stdio_bridge_destructive_mcp_tool_never_offers_allow_for_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run_turn(surface: Any, message: str) -> int:
        _ = message
        surface.request_approval(_mcp_tool_approval_request("one", destructive=True))
        surface.emit_message_end("done")
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, run_turn)
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "approve"}, request_id="chat")
        + "\n"
    )
    approval = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )
    assert approval["payload"]["allow_for_session_supported"] is False
    assert approval["payload"]["action"]["destructive"] is True
    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": approval["payload"]["approval_id"],
                "allow": False,
                "allow_for_session": False,
            },
            request_id="deny",
        )
        + "\n"
    )
    _wait_for_line(out, lambda line: line.get("type") == "message_end")


def test_stdio_bridge_approval_allow_for_session_unsafe_scope_is_downgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions: list[tuple[bool, bool]] = []

    def run_turn(surface: Any, message: str) -> int:
        _ = message
        decision = surface.request_approval(
            ApprovalRequest(
                kind="workspace_trust",
                reason="trust workspace",
                preview="trust this workspace",
            )
        )
        decisions.append((decision.allow, decision.allow_for_session))
        surface.emit_message_end("done")
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, run_turn)
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "unsafe"}, request_id="chat")
        + "\n"
    )
    approval = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )
    assert approval["payload"]["allow_for_session_supported"] is False

    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": approval["payload"]["approval_id"],
                "allow": True,
                "allow_for_session": True,
            },
            request_id="approve",
        )
        + "\n"
    )

    _wait_for_line(out, lambda line: line.get("type") == "message_end")
    response = [line for line in _json_lines(out) if line.get("id") == "approve"][0]
    result = response["result"]
    assert result["allow"] is True
    assert result["allow_for_session_supported"] is False
    assert result["allow_for_session"] is False
    assert result["allow_for_session_scope"] is None
    assert result["allow_for_session_warning"]
    assert decisions == [(True, False)]


def test_stdio_bridge_approval_deny_emits_denied_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions: list[bool] = []

    def run_turn(surface: Any, message: str) -> int:
        _ = message
        decision = surface.request_approval(
            ApprovalRequest(
                kind="shell_run",
                reason="run shell",
                preview="echo hi",
                command="echo hi",
            )
        )
        decisions.append(decision.allow)
        surface.emit_message_end("done")
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, run_turn)
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "deny"}, request_id="chat")
        + "\n"
    )
    approval_id = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )["payload"]["approval_id"]
    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": approval_id,
                "allow": False,
                "allow_for_session": False,
            },
            request_id="deny",
        )
        + "\n"
    )

    result_event = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval_result"
        ),
    )
    _wait_for_line(out, lambda line: line.get("type") == "message_end")
    assert result_event["payload"]["status"] == "denied"
    assert result_event["payload"]["allow"] is False
    assert decisions == [False]


def test_stdio_bridge_approval_duplicate_response_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run_turn(surface: Any, message: str) -> int:
        _ = message
        surface.request_approval(
            ApprovalRequest(
                kind="shell_run",
                reason="run shell",
                preview="echo hi",
                command="echo hi",
            )
        )
        surface.emit_message_end("done")
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, run_turn)
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "dup"}, request_id="chat")
        + "\n"
    )
    approval_id = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )["payload"]["approval_id"]
    params = {
        "session_id": session_id,
        "approval_id": approval_id,
        "allow": True,
        "allow_for_session": False,
    }
    bridge.process_line(_request("approval.respond", params, request_id="first") + "\n")
    bridge.process_line(_request("approval.respond", params, request_id="duplicate") + "\n")

    duplicate = [line for line in _json_lines(out) if line.get("id") == "duplicate"][0]
    assert duplicate["ok"] is False
    assert duplicate["error"]["code"] == "duplicate_response"


def test_stdio_bridge_approval_unknown_and_session_not_found_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run_turn(surface: Any, message: str) -> int:
        _ = surface, message
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, run_turn)
    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": "missing",
                "allow": True,
                "allow_for_session": False,
            },
            request_id="missing",
        )
        + "\n"
    )
    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": "missing-session",
                "approval_id": "missing",
                "allow": True,
                "allow_for_session": False,
            },
            request_id="missing-session",
        )
        + "\n"
    )

    lines = _json_lines(out)
    assert [line for line in lines if line.get("id") == "missing"][0]["error"][
        "code"
    ] == "unknown_approval"
    assert [line for line in lines if line.get("id") == "missing-session"][0]["error"][
        "code"
    ] == "session_not_found"


def test_stdio_bridge_approval_timeout_auto_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions: list[bool] = []

    def run_turn(surface: Any, message: str) -> int:
        _ = message
        decision = surface.request_approval(
            ApprovalRequest(kind="fs_write", reason="write", preview="write", files=["a.txt"])
        )
        decisions.append(decision.allow)
        surface.emit_message_end("done")
        return 0

    out, bridge, session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        run_turn,
        approval_timeout_seconds=0.3,
    )
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "timeout"}, request_id="chat")
        + "\n"
    )
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )
    result_event = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval_result"
        ),
        timeout=1.0,
    )
    _wait_for_line(out, lambda line: line.get("type") == "message_end", timeout=1.0)
    assert result_event["payload"]["status"] == "expired"
    assert result_event["payload"]["allow"] is False
    assert decisions == [False]


def test_stdio_bridge_approval_concurrent_requests_resolve_in_reverse_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions: dict[str, bool] = {}

    def run_turn(surface: Any, message: str) -> int:
        _ = message

        def ask(label: str, command: str) -> None:
            decision = surface.request_approval(
                ApprovalRequest(
                    kind="shell_run",
                    reason=label,
                    preview=command,
                    command=command,
                )
            )
            decisions[label] = decision.allow

        first = threading.Thread(target=ask, args=("first", "echo first"))
        second = threading.Thread(target=ask, args=("second", "echo second"))
        first.start()
        second.start()
        first.join(timeout=2.0)
        second.join(timeout=2.0)
        surface.emit_message_end("done")
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, run_turn)
    bridge.process_line(
        _request(
            "chat.send",
            {"session_id": session_id, "message": "concurrent"},
            request_id="chat",
        )
        + "\n"
    )
    prompts: dict[str, str] = {}
    for _ in range(2):
        prompt = _wait_for_line(
            out,
            lambda line: (
                line.get("type") == "prompt_for_input"
                and line.get("payload", {}).get("kind") == "approval"
                and line.get("payload", {}).get("reason") not in prompts
            ),
        )
        prompts[prompt["payload"]["reason"]] = prompt["payload"]["approval_id"]

    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": prompts["second"],
                "allow": False,
                "allow_for_session": False,
            },
            request_id="second",
        )
        + "\n"
    )
    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": prompts["first"],
                "allow": True,
                "allow_for_session": False,
            },
            request_id="first",
        )
        + "\n"
    )

    _wait_for_line(out, lambda line: line.get("type") == "message_end")
    assert decisions == {"first": True, "second": False}


def test_stdio_bridge_approval_respond_rejects_inline_secret() -> None:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": "session-ok",
                "approval_id": "approval",
                "allow": True,
                "allow_for_session": False,
                "api_key": "must-not-leak",
            },
            request_id="secret",
        )
        + "\n"
    )

    text = out.getvalue()
    response = _json_lines(out)[0]
    assert response["ok"] is False
    assert response["error"]["code"] == "inline_secret_rejected"
    assert "must-not-leak" not in text


def test_stdio_bridge_approval_events_redact_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "approval-secret-value")

    def run_turn(surface: Any, message: str) -> int:
        _ = message
        decision = surface.request_approval(
            ApprovalRequest(
                kind="fs_write",
                reason="write approval-secret-value",
                preview="write Bearer abcdefghijklmnop",
                files=["approval-secret-value.txt"],
                command="echo Authorization: Bearer abcdefghijklmnop",
                metadata={"authorization": "Bearer abcdefghijklmnop"},
            )
        )
        if decision.allow:
            surface.emit_message_end("done")
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, run_turn)
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "redact"}, request_id="chat")
        + "\n"
    )
    approval = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )
    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": approval["payload"]["approval_id"],
                "allow": True,
                "allow_for_session": False,
            },
            request_id="approve",
        )
        + "\n"
    )
    _wait_for_line(out, lambda line: line.get("type") == "message_end")

    rendered = json.dumps(_json_lines(out))
    assert "approval-secret-value" not in rendered
    assert "abcdefghijklmnop" not in rendered
    assert "<redacted>" in rendered


def test_stdio_bridge_approval_race_response_before_wait_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions: list[bool] = []
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, message: str) -> int:
            _ = message
            decision = self.surface.request_approval(
                ApprovalRequest(
                    kind="shell_run",
                    reason="run shell",
                    preview="echo hi",
                    command="echo hi",
                )
            )
            decisions.append(decision.allow)
            self.surface.emit_message_end("done")
            return 0

        def close(self) -> None:
            pass

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: FakeSession(kwargs["surface"]),
    )
    responded = False
    original_emit = bridge._record_and_write_event

    def intercept_emit(payload: dict[str, Any]) -> None:
        nonlocal responded
        original_emit(payload)
        if (
            not responded
            and payload.get("type") == "prompt_for_input"
            and payload.get("payload", {}).get("kind") == "approval"
        ):
            responded = True
            bridge.process_line(
                _request(
                    "approval.respond",
                    {
                        "session_id": payload["session_id"],
                        "approval_id": payload["payload"]["approval_id"],
                        "allow": True,
                        "allow_for_session": False,
                    },
                    request_id="race",
                )
                + "\n"
            )

    bridge._record_and_write_event = intercept_emit  # type: ignore[method-assign]
    bridge.process_line(
        _request(
            "session.create",
            {"workspace": os.fspath(tmp_path), "mode": "review", "model": "test-model"},
            request_id="create",
        )
        + "\n"
    )
    session_id = _json_lines(out)[0]["result"]["session_id"]
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "race"}, request_id="chat")
        + "\n"
    )

    _wait_for_line(out, lambda line: line.get("type") == "message_end")
    race_response = [line for line in _json_lines(out) if line.get("id") == "race"][0]
    assert race_response["ok"] is True
    assert decisions == [True]


def test_stdio_bridge_session_chat_events_and_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "note.txt").write_text("workspace file is not an artifact\n", encoding="utf-8")
    (artifact_root / "note.txt").write_bytes(b"hello artifact-secret\n")
    monkeypatch.setenv("ALYSIS_API_KEY", "artifact-secret")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, surface: Any) -> None:
            self.surface = surface
            self.closed = False

        def run_turn(self, message: str) -> int:
            self.surface.emit_message_delta(f"received {message}")
            self.surface.emit_message_end("done")
            return 0

        def close(self) -> None:
            self.closed = True

    def fake_create_session(**kwargs: Any) -> FakeSession:
        return FakeSession(kwargs["surface"])

    bridge = StdioBridge(stdout=out, create_session_fn=fake_create_session)
    bridge.process_line(
        _request(
            "session.create",
            {"workspace": os.fspath(tmp_path), "mode": "readonly", "model": "test-model"},
            request_id="create",
        )
        + "\n"
    )
    create_response = [line for line in _json_lines(out) if line.get("id") == "create"][0]
    session_id = create_response["result"]["session_id"]

    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "hi"}, request_id="chat") + "\n"
    )

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        lines = _json_lines(out)
        if any(line.get("type") == "message_end" for line in lines):
            break
        time.sleep(0.01)

    lines = _json_lines(out)
    chat_response = [line for line in lines if line.get("id") == "chat"][0]
    events = [line for line in lines if "type" in line]
    assert chat_response["result"]["status"] == "started"
    assert [event["type"] for event in events if event["type"].startswith("message_")] == [
        "message_delta",
        "message_end",
    ]
    assert events[0]["sequence"] == 1

    bridge.process_line(
        _request("artifact.list", {"session_id": session_id}, request_id="artifact-list") + "\n"
    )
    artifact_list_response = [
        line for line in _json_lines(out) if line.get("id") == "artifact-list"
    ][0]
    assert artifact_list_response["ok"] is True
    assert artifact_list_response["result"]["artifacts"] == [
        {
            "artifact_id": "session:note.txt",
            "root": "session",
            "path": "note.txt",
            "size_bytes": len("hello artifact-secret\n"),
        }
    ]

    bridge.process_line(
        _request(
            "artifact.read",
            {"session_id": session_id, "artifact_id": "session:note.txt"},
            request_id="artifact",
        )
        + "\n"
    )
    artifact_response = [line for line in _json_lines(out) if line.get("id") == "artifact"][0]
    assert artifact_response["ok"] is True
    assert artifact_response["result"]["content"] == "hello <redacted>\n"

    bridge.process_line(
        _request(
            "artifact.read",
            {"session_id": session_id, "artifact_id": "workspace:note.txt"},
            request_id="workspace-artifact",
        )
        + "\n"
    )
    workspace_artifact_response = [
        line for line in _json_lines(out) if line.get("id") == "workspace-artifact"
    ][0]
    assert workspace_artifact_response["ok"] is False
    assert workspace_artifact_response["error"]["code"] == "artifact_not_found"


def test_stdio_bridge_job_status_session_list_and_event_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, message: str) -> int:
            for idx in range(5):
                self.surface.emit_info(f"event-{idx}")
            self.surface.emit_message_end(message)
            return 0

        def close(self) -> None:
            pass

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: FakeSession(kwargs["surface"]),
        event_replay_max=3,
    )
    bridge.process_line(
        _request(
            "session.create",
            {"workspace": os.fspath(tmp_path), "mode": "readonly", "model": "test-model"},
            request_id="create",
        )
        + "\n"
    )
    session_id = _json_lines(out)[0]["result"]["session_id"]
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "done"}, request_id="chat")
        + "\n"
    )
    job_id = [line for line in _json_lines(out) if line.get("id") == "chat"][0]["result"]["job_id"]
    _wait_for_line(out, lambda line: line.get("type") == "message_end")

    # A reply precedes terminal cleanup. A completed but still reserved worker
    # remains the active job until its final ownership handoff finishes.
    worker = bridge._jobs[job_id].thread
    assert worker is not None
    worker.join(timeout=30.0)
    assert not worker.is_alive()

    bridge.process_line(_request("job.status", {"job_id": job_id}, request_id="job") + "\n")
    bridge.process_line(_request("session.list", request_id="sessions") + "\n")
    bridge.process_line(
        _request(
            "session.getEvents",
            {"session_id": session_id, "max_events": 2},
            request_id="events",
        )
        + "\n"
    )

    lines = _json_lines(out)
    job_status = [line for line in lines if line.get("id") == "job"][0]
    assert job_status["result"]["job_id"] == job_id
    assert job_status["result"]["session_id"] == session_id
    assert job_status["result"]["status"] == "completed"
    assert job_status["result"]["exit_code"] == 0
    assert job_status["result"]["started_at"]
    assert job_status["result"]["completed_at"]

    session_list = [line for line in lines if line.get("id") == "sessions"][0]
    assert session_list["result"]["sessions"][0]["session_id"] == session_id
    assert session_list["result"]["sessions"][0]["active_job"] is None
    assert session_list["result"]["sessions"][0]["last_job"]["job_id"] == job_id

    replay = [line for line in lines if line.get("id") == "events"][0]["result"]
    assert replay["truncated"] is True
    assert replay["lowest_retained_sequence"] == replay["events"][0]["sequence"] - 1
    assert len(replay["events"]) == 2
    assert replay["highest_retained_sequence"] >= replay["events"][-1]["sequence"]


def test_stdio_bridge_chat_send_does_not_block_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    started = threading.Event()
    release = threading.Event()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = tmp_path / "session-artifacts"

    class FakeSession:
        store = FakeStore()

        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, message: str) -> int:
            started.set()
            release.wait(timeout=2.0)
            self.surface.emit_message_end(f"done {message}")
            return 0

        def close(self) -> None:
            release.set()

    def fake_create_session(**kwargs: Any) -> FakeSession:
        return FakeSession(kwargs["surface"])

    bridge = StdioBridge(stdout=out, create_session_fn=fake_create_session)
    bridge.process_line(
        _request(
            "session.create",
            {"workspace": os.fspath(tmp_path), "mode": "readonly", "model": "test-model"},
            request_id="create",
        )
        + "\n"
    )
    session_id = _json_lines(out)[0]["result"]["session_id"]

    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "slow"}, request_id="chat")
        + "\n"
    )
    assert started.wait(timeout=1.0)
    chat_response = [line for line in _json_lines(out) if line.get("id") == "chat"][0]
    job_id = chat_response["result"]["job_id"]

    bridge.process_line(
        _request("session.cancel", {"session_id": session_id}, request_id="cancel") + "\n"
    )
    release.set()

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "warning_emitted"
            and "job_cancelled" in str(line.get("payload", {}).get("message", ""))
        ),
    )

    lines = _json_lines(out)
    cancel_response = [line for line in lines if line.get("id") == "cancel"][0]
    assert cancel_response["ok"] is True
    assert cancel_response["result"]["status"] == "cancellation_requested"
    assert cancel_response["result"]["state"] == "cancellation_requested"
    assert cancel_response["result"]["job_id"] == job_id
    assert cancel_response["result"]["job"]["cancellable"] is False
    assert any(
        line.get("type") == "warning_emitted"
        and "cancellation_requested" in str(line.get("payload", {}).get("message", ""))
        for line in lines
    )

    _wait_for_job_workers(bridge)
    bridge.process_line(_request("job.status", {"job_id": job_id}, request_id="job") + "\n")
    bridge.process_line(
        _request("session.status", {"session_id": session_id}, request_id="status") + "\n"
    )
    lines = _json_lines(out)
    job_status = [line for line in lines if line.get("id") == "job"][0]
    session_status = [line for line in lines if line.get("id") == "status"][0]
    assert job_status["result"]["status"] == "cancelled"
    assert job_status["result"]["state"] == "cancelled"
    assert job_status["result"]["exit_code"] == 130
    assert session_status["result"]["active_job"] is None
    assert session_status["result"]["last_job"]["job_id"] == job_id
    assert session_status["result"]["last_job"]["status"] == "cancelled"


def test_stdio_bridge_close_when_idle_releases_abandoned_session_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    browser_closed = threading.Event()
    executed_messages: list[str] = []
    browser_close_calls = 0

    class FakeBrowser:
        def close_all(self) -> None:
            nonlocal browser_close_calls
            browser_close_calls += 1
            browser_closed.set()

    def blocking_turn(_surface: Any, message: str) -> int:
        executed_messages.append(message)
        started.set()
        assert release.wait(timeout=2.0)
        return 0

    out, bridge, session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        blocking_turn,
        managed_browser_factory=lambda **_kwargs: FakeBrowser(),
    )
    chat = _send_bridge_request(
        bridge,
        out,
        "chat.send",
        {"session_id": session_id, "message": "slow"},
        request_id="chat-close-after-settle",
    )
    assert chat["ok"] is True
    assert started.wait(timeout=1.0)
    queued = _send_bridge_request(
        bridge,
        out,
        "chat.send",
        {"session_id": session_id, "message": "must-not-run"},
        request_id="queued-before-close",
    )
    assert queued["ok"] is True
    assert queued["result"]["status"] == "queued"

    bridge.process_line(
        _request(
            "session.cancel",
            {
                "session_id": session_id,
                "reason": "new_session_requested",
                "close_when_idle": True,
            },
            request_id="cancel-close-after-settle",
        )
        + "\n"
    )
    cancel = _response_by_id(out, "cancel-close-after-settle")
    assert cancel["ok"] is True
    assert cancel["result"]["status"] == "cancellation_requested"
    assert cancel["result"]["close_when_idle"] is True
    assert cancel["result"]["queued_prompts_cancelled"] == 1
    assert browser_closed.is_set() is False

    bridge.process_line(
        _request(
            "session.status",
            {"session_id": session_id},
            request_id="closing-session-status",
        )
        + "\n"
    )
    closing = _response_by_id(out, "closing-session-status")
    assert closing["ok"] is False
    assert closing["error"]["code"] == "session_closing"

    release.set()
    assert browser_closed.wait(timeout=2.0)
    deadline = time.monotonic() + 2.0
    while not bridge._sessions[session_id].closed and time.monotonic() < deadline:  # noqa: SLF001
        time.sleep(0.01)
    assert bridge._sessions[session_id].closed is True  # noqa: SLF001
    assert executed_messages == ["slow"]
    assert browser_close_calls == 1
    closed_status = _send_bridge_request(
        bridge,
        out,
        "session.status",
        {"session_id": session_id},
        request_id="closed-session-status",
    )
    assert closed_status["ok"] is False
    assert closed_status["error"]["code"] == "session_not_found"


def test_stdio_bridge_thread_start_failure_becomes_terminal_redacted_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = tmp_path / "session-artifacts"

    class FakeSession:
        store = FakeStore()

        def run_turn(self, _message: str) -> int:
            raise AssertionError("unstarted thread must not run")

        def close(self) -> None:
            return

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **_kwargs: FakeSession())
    created = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly", "model": "test-model"},
        request_id="create-thread-failure",
    )
    session_id = created["result"]["session_id"]

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("resource pressure Bearer abcdefgh1234567890")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    started = _send_bridge_request(
        bridge,
        out,
        "chat.send",
        {"session_id": session_id, "message": "hello"},
        request_id="chat-thread-failure",
    )
    job_id = started["result"]["job_id"]
    status = _send_bridge_request(
        bridge,
        out,
        "job.status",
        {"job_id": job_id},
        request_id="status-thread-failure",
    )

    assert started["ok"] is True
    assert status["result"]["status"] == "failed"
    assert status["result"]["exit_code"] == 1
    assert "job_start_failed" in str(status["result"]["error"])
    assert "abcdefgh1234567890" not in out.getvalue()
    assert len([line for line in _json_lines(out) if line.get("id") == "chat-thread-failure"]) == 1
    assert any(
        line.get("type") == "error_raised"
        and line.get("payload", {}).get("code") == "job_start_failed"
        for line in _json_lines(out)
    )


def test_stdio_bridge_close_cooperatively_stops_and_closes_active_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    started = threading.Event()
    closed: list[bool] = []
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeSession:
        store = SimpleNamespace(session_artifact_root=tmp_path / "session-artifacts")

        def run_turn(self, _message: str, *, cancellation_token: Any) -> int:
            started.set()
            while not cancellation_token.is_cancelled:
                time.sleep(0.001)
            cancellation_token.throw_if_cancelled("bridge_shutdown_interrupted")
            raise AssertionError("unreachable")

        def close(self) -> None:
            closed.append(True)

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **_kwargs: FakeSession(),
        shutdown_timeout_seconds=1.0,
    )
    created = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly", "model": "test-model"},
    )
    started_job = _send_bridge_request(
        bridge,
        out,
        "chat.send",
        {"session_id": created["result"]["session_id"], "message": "wait"},
    )
    assert started.wait(timeout=1.0)

    bridge.close()

    job = bridge._jobs[started_job["result"]["job_id"]]  # noqa: SLF001
    assert job.status == "cancelled"
    assert job.thread is not None and not job.thread.is_alive()
    assert closed == [True]
    assert bridge._sessions[created["result"]["session_id"]].closed is True  # noqa: SLF001


def test_stdio_bridge_shutdown_terminal_state_is_not_overwritten_by_late_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    started = threading.Event()
    release = threading.Event()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeSession:
        store = SimpleNamespace(session_artifact_root=tmp_path / "session-artifacts")

        def run_turn(self, _message: str) -> int:
            started.set()
            assert release.wait(timeout=2.0)
            return 0

        def close(self) -> None:
            return

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **_kwargs: FakeSession(),
        shutdown_timeout_seconds=0.0,
    )
    created = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly", "model": "test-model"},
    )
    started_job = _send_bridge_request(
        bridge,
        out,
        "chat.send",
        {"session_id": created["result"]["session_id"], "message": "wait"},
    )
    assert started.wait(timeout=1.0)
    job = bridge._jobs[started_job["result"]["job_id"]]  # noqa: SLF001

    try:
        with pytest.raises(stdio_bridge.BridgeSessionCleanupError, match="did not close cleanly"):
            bridge.close()
        assert job.status == "failed"
        assert bridge._sessions[created["result"]["session_id"]].closed is False
    finally:
        release.set()
    assert job.thread is not None
    job.thread.join(timeout=1.0)
    assert not job.thread.is_alive()
    assert job.status == "failed"
    assert job.error == "bridge_shutdown_interrupted"


def test_stdio_bridge_cancellation_side_effects_run_outside_state_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def blocking_turn(_surface: Any, _message: str) -> int:
        assert release.wait(timeout=2.0)
        return 0

    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, blocking_turn)
    started = _send_bridge_request(
        bridge,
        out,
        "chat.send",
        {"session_id": session_id, "message": "wait"},
    )
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and f"job_started {started['result']['job_id']}"
            in str(line.get("payload", {}).get("message", ""))
        ),
    )
    observed: list[bool] = []

    def assert_unlocked(_session: Any, _reason: str) -> None:
        observed.append(bool(bridge._state_lock._is_owned()))  # noqa: SLF001

    monkeypatch.setattr(bridge, "_cancel_pending_approvals", assert_unlocked)
    try:
        bridge.process_line(
            _request(
                "session.cancel",
                {"session_id": session_id},
                request_id="cancel-lock-order",
            )
            + "\n"
        )
    finally:
        release.set()
    response = _response_by_id(out, "cancel-lock-order")

    assert response["result"]["status"] == "cancellation_requested"
    assert observed == [False]
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "warning_emitted"
            and f"job_cancelled {started['result']['job_id']}"
            in str(line.get("payload", {}).get("message", ""))
        ),
    )


def test_stdio_bridge_cancel_closes_idle_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    closed = False
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = tmp_path / "session-artifacts"

    class FakeSession:
        store = FakeStore()

        def close(self) -> None:
            nonlocal closed
            closed = True

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession())
    bridge.process_line(
        _request(
            "session.create",
            {"workspace": os.fspath(tmp_path), "mode": "readonly", "model": "test-model"},
            request_id="create",
        )
        + "\n"
    )
    session_id = _json_lines(out)[0]["result"]["session_id"]

    bridge.process_line(
        _request("session.cancel", {"session_id": session_id}, request_id="cancel") + "\n"
    )

    cancel_response = [line for line in _json_lines(out) if line.get("id") == "cancel"][0]
    assert cancel_response["ok"] is True
    assert cancel_response["result"]["status"] == "closed"
    assert closed is True


def test_stdio_bridge_reusing_closed_session_id_resets_volatile_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeStore:
        session_artifact_root = tmp_path / "session-artifacts"

    class FakeSession:
        store = FakeStore()

        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, message: str) -> int:
            self.surface.emit_message_end(message)
            return 0

        def close(self) -> None:
            return

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: FakeSession(kwargs["surface"]),
    )
    create_params = {
        "workspace": os.fspath(tmp_path),
        "mode": "readonly",
        "model": "test-model",
        "session_id": "reused-session",
    }
    _send_bridge_request(bridge, out, "session.create", create_params, request_id="create-old")
    started = _send_bridge_request(
        bridge,
        out,
        "chat.send",
        {"session_id": "reused-session", "message": "old event"},
        request_id="chat-old",
    )
    old_job_id = started["result"]["job_id"]
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and f"job_completed {old_job_id}" in str(line.get("payload", {}).get("message", ""))
        ),
    )
    # job_completed is history, not proof that the original worker has retired;
    # until it has, closing the session is refused as session_busy. Wait for it.
    worker = bridge._jobs[old_job_id].thread
    assert worker is not None
    worker.join(timeout=30.0)
    assert not worker.is_alive()
    closed = _send_bridge_request(
        bridge,
        out,
        "session.cancel",
        {"session_id": "reused-session"},
        request_id="close-old",
    )
    assert closed["ok"] is True, closed

    recreated = _send_bridge_request(
        bridge,
        out,
        "session.create",
        create_params,
        request_id="create-new",
    )
    replay = _send_bridge_request(
        bridge,
        out,
        "session.getEvents",
        {"session_id": "reused-session"},
        request_id="replay-new",
    )
    bridge.process_line(_request("job.status", {"job_id": old_job_id}, request_id="old-job") + "\n")

    assert recreated["ok"] is True
    assert replay["result"]["events"] == []
    assert replay["result"]["dropped_event_count"] == 0
    assert _response_by_id(out, "old-job")["error"]["code"] == "job_not_found"


def test_stdio_bridge_bounds_closed_session_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeSession:
        store = SimpleNamespace(session_artifact_root=tmp_path / "session-artifacts")

        def close(self) -> None:
            return

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **_kwargs: FakeSession(),
        closed_session_history_max=1,
    )
    for session_id in ("closed-one", "closed-two"):
        _send_bridge_request(
            bridge,
            out,
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "session_id": session_id,
            },
            request_id=f"create-{session_id}",
        )
        _send_bridge_request(
            bridge,
            out,
            "session.cancel",
            {"session_id": session_id},
            request_id=f"close-{session_id}",
        )

    sessions = _send_bridge_request(bridge, out, "session.list", request_id="bounded-list")

    assert [row["session_id"] for row in sessions["result"]["sessions"]] == ["closed-two"]


def test_stdio_bridge_bounds_terminal_job_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeSession:
        store = SimpleNamespace(session_artifact_root=tmp_path / "session-artifacts")

        def run_turn(self, _message: str) -> int:
            return 0

        def close(self) -> None:
            return

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **_kwargs: FakeSession(),
        job_history_max=2,
    )
    created = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "readonly", "model": "test-model"},
        request_id="create-bounded-jobs",
    )
    session_id = created["result"]["session_id"]
    job_ids: list[str] = []
    for index in range(3):
        started = _send_bridge_request(
            bridge,
            out,
            "chat.send",
            {"session_id": session_id, "message": f"turn {index}"},
            request_id=f"bounded-job-{index}",
        )
        job_id = started["result"]["job_id"]
        job_ids.append(job_id)
        _wait_for_line(
            out,
            lambda line, expected=job_id: (
                line.get("type") == "info_emitted"
                and f"job_completed {expected}" in str(line.get("payload", {}).get("message", ""))
            ),
        )

    bridge.process_line(
        _request("job.status", {"job_id": job_ids[0]}, request_id="pruned-job") + "\n"
    )
    retained = _send_bridge_request(
        bridge,
        out,
        "job.status",
        {"job_id": job_ids[-1]},
        request_id="retained-job",
    )

    assert _response_by_id(out, "pruned-job")["error"]["code"] == "job_not_found"
    assert retained["result"]["status"] == "completed"


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = True) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"filesystem does not allow symlinks for this test: {exc}")


def _response_by_id(out: io.StringIO, request_id: str) -> dict[str, Any]:
    return [line for line in _json_lines(out) if line.get("id") == request_id][0]


def test_stdio_bridge_trace_clear_preserves_session_event_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, lambda *_: 0)
    surface = bridge._sessions[session_id].surface  # noqa: SLF001 - drives real session events.
    surface.emit_info("The session has retained events.")

    events_before = _send_bridge_request(
        bridge,
        out,
        "session.getEvents",
        {"session_id": session_id, "max_events": 100},
        request_id="events-before-clear",
    )
    assert events_before["ok"] is True
    assert events_before["result"]["events"], "session activity should retain replay events"
    highest_before = events_before["result"]["highest_retained_sequence"]

    cleared = _send_bridge_request(
        bridge,
        out,
        "session.trace.clear",
        {"session_id": session_id},
        request_id="trace-clear",
    )
    assert cleared["ok"] is True
    assert cleared["result"]["cleared"] is True
    assert cleared["result"]["events_after"] == 0

    trace_after = _send_bridge_request(
        bridge,
        out,
        "session.trace.listEvents",
        {"session_id": session_id},
        request_id="trace-after-clear",
    )
    assert trace_after["ok"] is True
    assert trace_after["result"]["events"] == []
    assert trace_after["result"]["count"] == 0

    status_after = _send_bridge_request(
        bridge,
        out,
        "session.trace.status",
        {"session_id": session_id},
        request_id="trace-status-after-clear",
    )
    assert status_after["ok"] is True
    assert status_after["result"]["retained_events"] == 0

    replay = _send_bridge_request(
        bridge,
        out,
        "session.getEvents",
        {"session_id": session_id, "max_events": 100},
        request_id="events-after-clear",
    )
    assert replay["ok"] is True
    assert replay["result"]["events"], (
        "session.trace.clear must not destroy the session.getEvents reconnect replay"
    )
    assert replay["result"]["highest_retained_sequence"] == highest_before

    surface.emit_info("A new event after the trace was cleared.")
    trace_new = _send_bridge_request(
        bridge,
        out,
        "session.trace.listEvents",
        {"session_id": session_id},
        request_id="trace-new-events",
    )
    assert trace_new["ok"] is True
    assert trace_new["result"]["count"] >= 1, (
        "events emitted after session.trace.clear must be visible in the trace view"
    )


def test_ide_bridge_cli_health_subprocess() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    proc = subprocess.run(
        [sys.executable, "-m", "alysis_code.cli", "ide-bridge", "health"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["protocol_version"] == "1"
    assert payload["capabilities"]["features"]["terminal_output_scraping"] is False
    assert (
        payload["capabilities"]["features"]["management"]["mcp"]["auth_login"]["supported"] is True
    )
    assert (
        payload["capabilities"]["features"]["management"]["mcp"]["auth_login"][
            "advertised_lifecycle_methods"
        ]
        is True
    )
    assert payload["capabilities"]["features"]["management"]["mcp"]["auth_login"]["methods"] == [
        "mcp.auth.login.start",
        "mcp.auth.login.status",
        "mcp.auth.login.cancel",
    ]
    assert payload["capabilities"]["features"]["management"]["hooks"]["watch"]["supported"] is False
    assert (
        payload["capabilities"]["features"]["management"]["hooks"]["watch"][
            "advertised_lifecycle_methods"
        ]
        is False
    )
    assert (
        "hooks.watch.start"
        in payload["capabilities"]["features"]["management"]["hooks"]["watch"]["proposed_methods"]
    )
    assert payload["capabilities"]["features"]["cancellation"]["active_jobs"] is True
    assert (
        payload["capabilities"]["features"]["cancellation"]["behavior"]
        == "cooperative_checkpoint_cancellation"
    )
    assert payload["capabilities"]["features"]["cancellation"]["interrupt_kind"] == "cooperative"
    assert payload["capabilities"]["features"]["cancellation"]["hard_interrupt"] is False
    assert (
        payload["capabilities"]["features"]["cancellation"]["stale_active_jobs_reconciled"] is True
    )
    assert payload["capabilities"]["features"]["approvals"] == {
        "round_trip": True,
        "default_deny": True,
        "session_scoped_allow": True,
        "supported_kinds_for_session_allow": [
            "shell_run",
            "shell_background",
            "custom_tool_run",
            "mcp_tool_run",
            "fs_write",
            "fs_edit",
            "fs_move",
            "fs_copy",
            "fs_delete",
            "fs_mkdir",
            "git_apply_patch",
            "verify_run",
        ],
        "timeout_seconds_default": 300,
    }
    assert payload["capabilities"]["features"]["event_replay"]["bounded"] is True
    assert not [
        method
        for method in payload["capabilities"]["methods"]
        if method.startswith(("forge.", "diff."))
    ]
    assert "forge" not in payload["capabilities"]["features"]


def test_ide_bridge_stdio_subprocess() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    proc = subprocess.run(
        [sys.executable, "-m", "alysis_code.cli", "ide-bridge", "--stdio"],
        cwd=REPO_ROOT,
        env=env,
        input=_request("getCapabilities", request_id="caps") + "\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout.splitlines()[0])
    assert payload["id"] == "caps"
    assert "session.create" in payload["result"]["methods"]


def test_stdio_bridge_structured_tasks_and_questions_are_durable_and_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    state_path = tmp_path.parent / f"{tmp_path.name}-structured-state.sqlite3"
    out, bridge, session_id = _create_review_session(
        tmp_path,
        monkeypatch,
        lambda _surface, _message: 0,
        structured_state_factory=lambda **kwargs: DurableStructuredState(**kwargs, path=state_path),
    )

    bridge.process_line(
        _request(
            "session.tasks.replace",
            {
                "session_id": session_id,
                "workspace_trusted": True,
                "expected_revision": 0,
                "tasks": [
                    {"task_id": "inspect", "title": "Inspect", "status": "completed"},
                    {"task_id": "build", "title": "Build", "status": "in_progress"},
                ],
            },
            request_id="tasks-replace",
        )
        + "\n"
    )
    replaced = _response_by_id(out, "tasks-replace")
    assert replaced["ok"] is True, replaced
    assert replaced["result"]["revision"] == 1
    assert replaced["result"]["updated"] is True

    bridge.process_line(
        _request(
            "session.tasks.replace",
            {
                "session_id": session_id,
                "workspace_trusted": True,
                "expected_revision": 0,
                "tasks": [],
            },
            request_id="tasks-stale",
        )
        + "\n"
    )
    stale = _response_by_id(out, "tasks-stale")
    assert stale["result"] == {
        "session_id": session_id,
        "updated": False,
        "conflict": True,
        "current_revision": 1,
    }

    question_params = {
        "session_id": session_id,
        "workspace_trusted": True,
        "idempotency_key": "delivery-choice",
        "expires_in_seconds": 60,
        "questions": [
            {
                "question_id": "strategy",
                "prompt": "Choose a strategy",
                "options": [
                    {
                        "option_id": "safe",
                        "label": "Safe",
                        "description": "Prefer verification",
                    },
                    {
                        "option_id": "fast",
                        "label": "Fast",
                        "description": "Prefer throughput",
                    },
                ],
            }
        ],
    }
    bridge.process_line(
        _request("session.questions.create", question_params, request_id="question-create") + "\n"
    )
    created = _response_by_id(out, "question-create")
    assert created["ok"] is True
    assert created["result"]["created"] is True
    question_set_id = created["result"]["question_set_id"]

    bridge.process_line(
        _request("session.questions.create", question_params, request_id="question-retry") + "\n"
    )
    assert _response_by_id(out, "question-retry")["result"]["created"] is False

    bridge.process_line(
        _request(
            "session.questions.answer",
            {
                "session_id": session_id,
                "workspace_trusted": True,
                "question_set_id": question_set_id,
                "answers": {"strategy": "safe"},
            },
            request_id="question-answer",
        )
        + "\n"
    )
    answered = _response_by_id(out, "question-answer")
    assert answered["ok"] is True
    assert answered["result"]["status"] == "answered"
    assert answered["result"]["answers"] == [{"question_id": "strategy", "option_id": "safe"}]
    serialized = json.dumps(_json_lines(out))
    assert "lease_token" not in serialized


def test_stdio_bridge_config_get_names_the_key_route_scope_like_config_show(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config.get carries `config show`'s credential_id (as route_scope) and the saved-keys
    file, so the desktop app needs no CLI process to read the account; never the key."""
    from alysis_code.config import credentials_path, load_config, resolve_api_key
    from alysis_code.llm.metadata import credential_scope_fingerprint

    _isolate_alysis_state(tmp_path, monkeypatch)
    key = "route-scope-fixture-value-0123456789"
    monkeypatch.setenv("ROUTE_SCOPE_FIXTURE_KEY", key)
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    bridge.process_line(_request("config.get", request_id="before") + "\n")
    bridge.process_line(
        _request(
            "profile.add",
            {
                "workspace_trusted": True,
                "name": "demo",
                "base_url": "https://provider.example/v1",
                "api_key_env": "ROUTE_SCOPE_FIXTURE_KEY",
            },
            request_id="add",
        )
        + "\n"
    )
    bridge.process_line(
        _request("profile.use", {"workspace_trusted": True, "name": "demo"}, request_id="use")
        + "\n"
    )
    bridge.process_line(_request("config.get", request_id="after") + "\n")

    payloads = _json_lines(out)
    assert [payload["ok"] for payload in payloads] == [True, True, True, True]
    before, after = payloads[0]["result"], payloads[3]["result"]
    if before["api_key"]["present"] is False:
        assert before["route_scope"] == ""
    # The same fingerprint `config show` prints as credential_id for these settings.
    expected = credential_scope_fingerprint(resolve_api_key(load_config()).key)
    assert expected
    assert after["api_key"]["present"] is True
    assert after["route_scope"] == expected
    assert after["saved_keys_path"] == str(credentials_path())
    assert key not in out.getvalue()


def test_stdio_bridge_profile_select_model_is_the_cli_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """profile.selectModel makes `alysis profile select-model`'s one locked write, with no
    new process; a refused selection writes nothing."""
    from alysis_code.config import config_path, load_config
    from alysis_code.profiles import get_profile

    _isolate_alysis_state(tmp_path, monkeypatch)
    monkeypatch.setenv("SELECT_MODEL_FIXTURE_KEY", "select-model-fixture-value-0123456789")
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    for name in ("alpha", "beta"):
        bridge.process_line(
            _request(
                "profile.add",
                {
                    "workspace_trusted": True,
                    "name": name,
                    "base_url": "https://provider.example/v1",
                    "api_key_env": "SELECT_MODEL_FIXTURE_KEY",
                },
                request_id=f"add-{name}",
            )
            + "\n"
        )
    bridge.process_line(
        _request(
            "profile.selectModel",
            {"workspace_trusted": True, "name": "beta", "model": "beta-model-2"},
            request_id="select",
        )
        + "\n"
    )
    saved = config_path().read_text(encoding="utf-8")
    bridge.process_line(
        _request(
            "profile.selectModel",
            {"workspace_trusted": True, "name": "missing", "model": "other"},
            request_id="unknown",
        )
        + "\n"
    )

    payloads = _json_lines(out)
    assert [payload["ok"] for payload in payloads] == [True, True, True, False]
    selected = payloads[2]["result"]
    assert selected["profile_name"] == "beta"
    assert selected["model"] == "beta-model-2"
    assert selected["changed"] is True
    assert selected["secret_values_included"] is False
    cfg = load_config()
    assert cfg.extra_fields["active_profile"] == "beta"
    assert get_profile(cfg, "beta").default_model == "beta-model-2"
    assert cfg.model == "beta-model-2"
    assert cfg.execution.backend == "native"
    assert payloads[3]["error"]["code"] == "config_error"
    assert config_path().read_text(encoding="utf-8") == saved, "a refused selection writes nothing"
    assert "select-model-fixture-value" not in out.getvalue()
