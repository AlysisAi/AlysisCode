from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.approval_scope import exact_command_scope, exact_file_set_scope
from alysis_code.config import AppConfig, clone_cfg, get_api_key, load_config, save_config
from alysis_code.extensions.models import InstalledExtensionState
from alysis_code.ide import forge_protocol, management_protocol, stdio_bridge
from alysis_code.ide.forge_request_ledger import (
    DurableForgeRequestLedger,
    ForgeRequestLedgerConfig,
)
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
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult

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


def _wait_for_line(out: io.StringIO, predicate: Any, *, timeout: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in _json_lines(out):
            if predicate(line):
                return line
        time.sleep(0.01)
    raise AssertionError("timed out waiting for protocol line")


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


def test_stdio_bridge_session_close_keeps_cleanup_failure_retryable(
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
        "message": "Managed browser cleanup is incomplete; retry session close.",
    }
    assert "private detail" not in repr(failed)

    retried = _send_bridge_request(
        bridge,
        out,
        "session.cancel",
        {"session_id": session_id},
        request_id="session-close-retry",
    )
    assert retried["result"]["status"] == "closed"
    assert browser.close_calls == 2


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


def test_stdio_bridge_session_context_reports_estimated_tokens_not_fake_zero(
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
    assert context["result"]["source"] == "tokenizer_estimate"
    assert context["result"]["approximate"] is True
    assert context["result"]["token_usage_available"] is True
    assert context["result"]["used_input_tokens"] > 0
    assert (
        context["result"]["token_breakdown"]["total_tokens"]
        == context["result"]["used_input_tokens"]
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
    assert status["result"]["background_worker_surface"] == "forge.swarm"

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
            if browser_close_calls == 1:
                raise RuntimeError("transient browser cleanup failure")
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
    assert browser_close_calls == 2
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

    bridge.close()
    assert job.status == "failed"

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
    _send_bridge_request(
        bridge,
        out,
        "session.cancel",
        {"session_id": "reused-session"},
        request_id="close-old",
    )

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


def test_stdio_bridge_forge_plan_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {"workspace": os.fspath(tmp_path), "mode": "readonly", "model": "test-model"},
            request_id="plan",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "plan"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "missing_field"


def _fake_planner_success(**_: Any) -> SimpleNamespace:
    return SimpleNamespace(
        assistant_message="Structured Forge plan created.",
        questions=[],
        error=None,
        plan_update={
            "project_goal": "Productionize the IDE Forge planner.",
            "summary": "Create durable, scoped Forge planning behavior for IDE clients.",
            "tasks_add": [
                {
                    "title": "Wire IDE Forge Plan to planner output",
                    "description": "Replace the shallow IDE Forge task scaffold with planner-produced structured tasks.",
                    "acceptance_criteria": [
                        "Forge Plan returns structured tasks with concrete scope.",
                        "Forge Plan persists artifacts that can be reopened.",
                    ],
                    "estimated_files": ["demo.py"],
                    "write_scope": ["demo.py"],
                    "dependencies": [],
                }
            ],
        },
        planner_router_event=None,
    )


def _fake_planner_two_task_dependency(**_: Any) -> SimpleNamespace:
    return SimpleNamespace(
        assistant_message="Structured Forge plan with dependent tasks created.",
        questions=[],
        error=None,
        plan_update={
            "project_goal": "Execute dependent review tasks.",
            "summary": "Run a prerequisite task before its dependent task.",
            "tasks_add": [
                {
                    "title": "Prepare dependency",
                    "description": "Change the prerequisite file.",
                    "acceptance_criteria": ["The prerequisite task has a reviewed change."],
                    "estimated_files": ["demo.py"],
                    "write_scope": ["demo.py"],
                    "dependencies": [],
                },
                {
                    "title": "Use dependency",
                    "description": "Change the dependent file after the prerequisite passes.",
                    "acceptance_criteria": [
                        "The dependent task only runs after the prerequisite passes."
                    ],
                    "estimated_files": ["dependent.py"],
                    "write_scope": ["dependent.py"],
                    "dependencies": ["T01"],
                },
            ],
        },
        planner_router_event=None,
    )


def _fake_planner_incomplete(**_: Any) -> SimpleNamespace:
    return SimpleNamespace(
        assistant_message="Incomplete plan.",
        questions=[],
        error=None,
        plan_update={
            "tasks_add": [
                {
                    "title": "Implement unclear scope",
                    "description": "Change the requested behavior but without enough execution details.",
                    "acceptance_criteria": [],
                    "estimated_files": ["missing.py"],
                    "write_scope": ["missing.py"],
                    "dependencies": [],
                }
            ],
        },
        planner_router_event=None,
    )


def _sandbox_diagnostic(*, ready: bool, status: str = "ready") -> SimpleNamespace:
    return SimpleNamespace(
        ready=ready,
        status=status,
        configured_mode="strict",
        configured_backend="auto",
        selected_backend="test",
        docker_image="test",
        server_image="test",
        checks=(),
        next_steps=() if ready else ("Run `alysis doctor sandbox`.",),
    )


def _fake_forge_execute_agent(**kwargs: Any) -> int:
    root = Path(kwargs["root"])
    surface = kwargs["surface"]
    files = list(kwargs.get("allow_write_globs") or [])
    decision = surface.request_approval(
        ApprovalRequest(
            kind="fs_write",
            reason="test file write",
            preview="write demo.py",
            files=files,
            allow_for_session_scope=exact_file_set_scope(files, operation="fs_write"),
        )
    )
    if not decision.allow:
        return 1
    (root / "demo.py").write_text("print('executed')\n", encoding="utf-8")
    return 0


def _fake_shell_approval_agent(**kwargs: Any) -> int:
    surface = kwargs["surface"]
    command = "pytest -q"
    decision = surface.request_approval(
        ApprovalRequest(
            kind="shell_run",
            reason="test shell command",
            preview=command,
            command=command,
            allow_for_session_scope=exact_command_scope(command, kind="shell_run"),
        )
    )
    return 0 if decision.allow else 1


def _fake_verify_success(
    *,
    root: Path,
    commands: list[str],
    artifact_path: Path,
    cfg: AppConfig,
) -> VerifyRunResult:
    _ = root, cfg
    artifact_path.write_text("verification passed\n", encoding="utf-8")
    return VerifyRunResult(
        commands=commands,
        command_results=[
            VerifyCommandResult(command=command, exit_code=0, output="ok", real_execution=True)
            for command in commands
        ],
        artifact_path=artifact_path,
    )


def _fake_verify_failure(
    *,
    root: Path,
    commands: list[str],
    artifact_path: Path,
    cfg: AppConfig,
) -> VerifyRunResult:
    _ = root, cfg
    artifact_path.write_text("verification failed\n", encoding="utf-8")
    return VerifyRunResult(
        commands=commands,
        command_results=[
            VerifyCommandResult(command=command, exit_code=1, output="failed", real_execution=True)
            for command in commands
        ],
        artifact_path=artifact_path,
    )


def _configure_successful_ide_forge_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "ALYSIS_DATA_DIR", os.fspath(tmp_path.parent / f"{tmp_path.name}-alysis-data")
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)


def _create_successful_ide_forge_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    instruction: str = "Prepare a persisted Forge plan.",
    forge_swarm_runner: Any | None = None,
    approval_timeout_seconds: float = 300.0,
) -> tuple[io.StringIO, StdioBridge, dict[str, Any]]:
    _configure_successful_ide_forge_plan(tmp_path, monkeypatch)
    out = io.StringIO()
    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path),
        forge_swarm_runner=forge_swarm_runner,
        approval_timeout_seconds=approval_timeout_seconds,
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": instruction,
            },
            request_id="plan",
        )
        + "\n"
    )
    response = [line for line in _json_lines(out) if line.get("id") == "plan"][0]
    assert response["ok"] is True
    return out, bridge, response["result"]


def _write_external_forge_plan(runtime_root: Path, plan_id: str, marker: str) -> None:
    plan_dir = runtime_root / "runs" / plan_id / "plan"
    plan_dir.mkdir(parents=True)
    plan = {
        "schema_version": 2,
        "run_id": plan_id,
        "project_goal": marker,
        "summary": marker,
        "tasks": [],
        "assets": [],
        "requirements": [],
        "created_at": "2026-05-22T00:00:00+00:00",
        "updated_at": "2026-05-22T00:00:00+00:00",
    }
    (plan_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (plan_dir / "PLAN.md").write_text(marker, encoding="utf-8")


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = True) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"filesystem does not allow symlinks for this test: {exc}")


def _assert_error_does_not_leak_external_path(
    response: dict[str, Any],
    *,
    external: Path,
    marker: str,
) -> None:
    payload = json.dumps(response)
    assert os.fspath(external) not in payload
    assert marker not in payload


def _assert_protocol_error(
    out: io.StringIO,
    *,
    request_id: str,
    code: str,
    message_contains: str,
) -> None:
    response = [line for line in _json_lines(out) if line.get("id") == request_id][0]
    assert response["ok"] is False
    assert response["error"]["code"] == code
    assert message_contains in response["error"]["message"]


def _response_by_id(out: io.StringIO, request_id: str) -> dict[str, Any]:
    return [line for line in _json_lines(out) if line.get("id") == request_id][0]


def test_stdio_bridge_forge_show_review_attach_and_assets_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare safe Forge assets parity.",
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]
    source = tmp_path / "spec.md"
    source.write_text("Design requirements for the Forge asset surface.\n", encoding="utf-8")

    bridge.process_line(
        _request(
            "forge.show",
            {"session_id": session_id, "plan_id": plan_id},
            request_id="show",
        )
        + "\n"
    )
    show_response = _response_by_id(out, "show")
    assert show_response["ok"] is True
    assert show_response["result"]["plan_id"] == plan_id
    assert show_response["result"]["assets"] == []

    bridge.process_line(
        _request(
            "forge.assets.add",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "source_path": "spec.md",
                "title": "Spec",
            },
            request_id="add-untrusted",
        )
        + "\n"
    )
    add_untrusted = _response_by_id(out, "add-untrusted")
    assert add_untrusted["ok"] is False
    assert add_untrusted["error"]["code"] == "workspace_trust_required"

    bridge.process_line(
        _request(
            "forge.assets.add",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "source_path": "spec.md",
                "title": "Spec",
                "description": "Structured backend test asset.",
                "pinned": True,
            },
            request_id="add",
        )
        + "\n"
    )
    add_response = _response_by_id(out, "add")
    assert add_response["ok"] is True
    asset_id = add_response["result"]["asset"]["record"]["id"]
    assert add_response["result"]["asset"]["record"]["title"] == "Spec"
    assert add_response["result"]["status"] == "added"

    bridge.process_line(
        _request(
            "forge.assets.list",
            {"session_id": session_id, "plan_id": plan_id},
            request_id="assets-list",
        )
        + "\n"
    )
    list_response = _response_by_id(out, "assets-list")
    assert list_response["ok"] is True
    assert list_response["result"]["count"] == 1

    bridge.process_line(
        _request(
            "forge.assets.show",
            {"session_id": session_id, "plan_id": plan_id, "asset_id": asset_id},
            request_id="assets-show",
        )
        + "\n"
    )
    detail_response = _response_by_id(out, "assets-show")
    assert detail_response["ok"] is True
    assert detail_response["result"]["asset"]["record"]["id"] == asset_id

    bridge.process_line(
        _request(
            "forge.assets.edit",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "asset_id": asset_id,
                "pinned": False,
            },
            request_id="assets-edit",
        )
        + "\n"
    )
    edit_response = _response_by_id(out, "assets-edit")
    assert edit_response["ok"] is True
    assert edit_response["result"]["asset"]["record"]["pinned"] is False

    bridge.process_line(
        _request(
            "forge.assets.refresh",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "asset_id": asset_id,
            },
            request_id="assets-refresh",
        )
        + "\n"
    )
    refresh_response = _response_by_id(out, "assets-refresh")
    assert refresh_response["ok"] is True
    assert refresh_response["result"]["async_background"] is False

    bridge.process_line(
        _request(
            "forge.assets.checkPlan",
            {"session_id": session_id, "plan_id": plan_id},
            request_id="assets-check",
        )
        + "\n"
    )
    check_response = _response_by_id(out, "assets-check")
    assert check_response["ok"] is True
    assert check_response["result"]["ok"] is True

    bridge.process_line(
        _request(
            "forge.assets.cancelPending",
            {"session_id": session_id, "plan_id": plan_id},
            request_id="assets-cancel",
        )
        + "\n"
    )
    cancel_response = _response_by_id(out, "assets-cancel")
    assert cancel_response["ok"] is True
    assert cancel_response["result"]["status"] in {"idle", "cancelled"}

    attach_source = tmp_path / "attach.md"
    attach_source.write_text("Attachment alias input.\n", encoding="utf-8")
    bridge.process_line(
        _request(
            "forge.attach",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "source": "attach.md",
            },
            request_id="attach",
        )
        + "\n"
    )
    attach_response = _response_by_id(out, "attach")
    assert attach_response["ok"] is True
    assert attach_response["result"]["status"] == "added"

    def fake_review_task(**kwargs: Any) -> SimpleNamespace:
        paths = kwargs["paths"]
        task = kwargs["task"]
        assert kwargs["api_key_override"] is None
        paths.execution_reviews_dir.mkdir(parents=True, exist_ok=True)
        json_path = paths.execution_reviews_dir / "T01.json"
        markdown_path = paths.execution_reviews_dir / "T01.md"
        json_path.write_text(
            json.dumps({"approved": False, "summary": "Needs review"}),
            encoding="utf-8",
        )
        markdown_path.write_text("# Review\nNeeds review\n", encoding="utf-8")
        return SimpleNamespace(
            task_id=str(task["id"]),
            approved=False,
            confidence="medium",
            summary="Needs review",
            blocking_issues_count=1,
            non_blocking_issues_count=0,
            json_path=json_path,
            markdown_path=markdown_path,
        )

    monkeypatch.setattr(forge_protocol, "review_task", fake_review_task)
    bridge.process_line(
        _request(
            "forge.review",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "task_id": "T01",
            },
            request_id="review",
        )
        + "\n"
    )
    review_response = _response_by_id(out, "review")
    assert review_response["ok"] is True
    assert review_response["result"]["requires_human_approval"] is True
    assert review_response["result"]["action"]["kind"] == "review_needed"

    bridge.process_line(
        _request(
            "forge.assets.delete",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "asset_id": asset_id,
            },
            request_id="delete-unconfirmed",
        )
        + "\n"
    )
    delete_unconfirmed = _response_by_id(out, "delete-unconfirmed")
    assert delete_unconfirmed["ok"] is False
    assert delete_unconfirmed["error"]["code"] == "confirmation_required"

    bridge.process_line(
        _request(
            "forge.assets.delete",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "asset_id": asset_id,
                "yes": True,
            },
            request_id="delete",
        )
        + "\n"
    )
    delete_response = _response_by_id(out, "delete")
    assert delete_response["ok"] is True
    assert delete_response["result"]["status"] == "deleted"

    plan_json = tmp_path / ".alysis" / "runs" / plan_id / "plan" / "plan.json"
    plan_payload = json.loads(plan_json.read_text(encoding="utf-8"))
    plan_payload["schema_version"] = 2
    plan_json.write_text(json.dumps(plan_payload), encoding="utf-8")
    bridge.process_line(
        _request(
            "forge.assets.pruneLegacy",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "yes": True,
            },
            request_id="prune",
        )
        + "\n"
    )
    prune_response = _response_by_id(out, "prune")
    assert prune_response["ok"] is True
    assert prune_response["result"]["deleted"] == []


def test_stdio_bridge_forge_plan_edit_methods_persist_and_validate_revisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare typed Forge plan edits.",
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]

    def send(
        method: str,
        params: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        bridge.process_line(_request(method, params, request_id=request_id) + "\n")
        return _response_by_id(out, request_id)

    state = send(
        "forge.plan.getState",
        {"session_id": session_id, "plan_id": plan_id},
        "plan-state",
    )
    assert state["ok"] is True
    assert state["result"]["ide_revision"] == 0
    assert state["result"]["assistant"]["source"] == "default"
    assert state["result"]["validation"]["ok"] is True
    assert state["result"]["tasks"][0]["task_id"] == "T01"

    untrusted = send(
        "forge.plan.setGoal",
        {"session_id": session_id, "plan_id": plan_id, "goal": "New goal"},
        "goal-untrusted",
    )
    assert untrusted["ok"] is False
    assert untrusted["error"]["code"] == "workspace_trust_required"

    assistant = send(
        "forge.plan.setAssistant",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "instruction": "Focus on reviewed, scoped edits.",
            "expected_revision": 0,
        },
        "assistant-set",
    )
    assert assistant["ok"] is True
    assert assistant["result"]["changed"] is True
    assert assistant["result"]["ide_revision"] == 1
    assert assistant["result"]["assistant"]["instruction"] == "Focus on reviewed, scoped edits."
    assert assistant["result"]["audit"]["secret_values_included"] is False

    stale = send(
        "forge.plan.setGoal",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "goal": "Stale update",
            "expected_revision": 0,
        },
        "goal-stale",
    )
    assert stale["ok"] is False
    assert stale["error"]["code"] == "stale_plan_revision"

    goal = send(
        "forge.plan.setGoal",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "goal": "Updated IDE Forge goal",
            "expected_revision": 1,
        },
        "goal-set",
    )
    assert goal["ok"] is True
    assert goal["result"]["goal"] == "Updated IDE Forge goal"
    assert goal["result"]["ide_revision"] == 2
    assert goal["result"]["validation"]["stale_reason"] == "goal_changed"

    task = send(
        "forge.plan.updateTask",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "task_id": "T01",
            "title": "Updated task title",
            "body": "Keep changes in the plan store only.",
            "status": "blocked",
            "expected_revision": 2,
        },
        "task-update",
    )
    assert task["ok"] is True
    assert task["result"]["task"]["task_id"] == "T01"
    assert task["result"]["task"]["title"] == "Updated task title"
    assert task["result"]["task"]["status"] == "blocked"
    assert task["result"]["ide_revision"] == 3

    show_task = send(
        "forge.plan.updateTask",
        {"session_id": session_id, "plan_id": plan_id, "task_id": "T01"},
        "task-show",
    )
    assert show_task["ok"] is True
    assert show_task["result"]["changed"] is False
    assert show_task["result"]["task"]["title"] == "Updated task title"

    invalid_task = send(
        "forge.plan.updateTask",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "task_id": "../T01",
            "status": "done",
        },
        "task-invalid",
    )
    assert invalid_task["ok"] is False
    assert invalid_task["error"]["code"] == "invalid_task_id"

    invalid_status = send(
        "forge.plan.updateTask",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "task_id": "T01",
            "status": "auto/fullaccess",
        },
        "task-invalid-status",
    )
    assert invalid_status["ok"] is False
    assert invalid_status["error"]["code"] == "invalid_task_status"

    regen_untrusted = send(
        "forge.plan.regenerate",
        {"session_id": session_id, "plan_id": plan_id},
        "regen-untrusted",
    )
    assert regen_untrusted["ok"] is False
    assert regen_untrusted["error"]["code"] == "workspace_trust_required"

    regen_stale = send(
        "forge.plan.regenerate",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "expected_revision": 2,
            "instruction": "Refresh the plan for IDE parity.",
        },
        "regen-stale",
    )
    assert regen_stale["ok"] is False
    assert regen_stale["error"]["code"] == "stale_plan_revision"

    regenerated = send(
        "forge.plan.regenerate",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "expected_revision": 3,
            "instruction": "Refresh the plan for IDE parity.",
            "focus": "demo.py",
        },
        "regen-ok",
    )
    assert regenerated["ok"] is True
    assert regenerated["result"]["old_revision"] == 3
    assert regenerated["result"]["new_revision"] == 4
    assert regenerated["result"]["ide_revision"] == 4
    assert regenerated["result"]["changed"] is True
    assert regenerated["result"]["redacted"] is True
    assert regenerated["result"]["secret_values_included"] is False
    assert regenerated["result"]["audit"]["operation"] == "forge.plan.regenerate"
    assert regenerated["result"]["validation"]["stale_reason"] == "regenerated"

    persisted_plan = tmp_path / ".alysis" / "runs" / plan_id / "plan" / "plan.json"
    persisted = json.loads(persisted_plan.read_text(encoding="utf-8"))
    assert persisted["project_goal"] == "Productionize the IDE Forge planner."
    assert persisted["ide_revision"] == 4
    assert persisted["tasks"][0]["title"] == "Updated task title"
    assert persisted["ide_stale_reason"] == "regenerated"


def test_forge_plan_regenerate_protocol_does_not_shell_or_swarm() -> None:
    source = (REPO_ROOT / "src" / "alysis_code" / "ide" / "forge_protocol.py").read_text(
        encoding="utf-8"
    )

    assert "run_swarm" not in source
    assert "subprocess" not in source
    assert "terminal" not in source.casefold()


def _blocking_planner_fixture() -> tuple[threading.Event, threading.Event, Any]:
    planner_started = threading.Event()
    planner_release = threading.Event()

    def blocking_planner(**kwargs: Any) -> SimpleNamespace:
        planner_started.set()
        cancellation_token = kwargs.get("cancellation_token")
        deadline = time.monotonic() + 5.0
        while not planner_release.wait(timeout=0.01):
            if cancellation_token is not None and getattr(
                cancellation_token, "is_cancelled", False
            ):
                cancellation_token.throw_if_cancelled("planner_cancelled")
            assert time.monotonic() < deadline
        return _fake_planner_success(**kwargs)

    return planner_started, planner_release, blocking_planner


def _persisted_plan(tmp_path: Path, plan_id: str) -> dict[str, Any]:
    plan_path = tmp_path / ".alysis" / "runs" / plan_id / "plan" / "plan.json"
    return json.loads(plan_path.read_text(encoding="utf-8"))


def test_stdio_bridge_sync_forge_plan_regenerate_releases_state_lock_during_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare a plan for lock-free regeneration.",
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]
    planner_started, planner_release, blocking_planner = _blocking_planner_fixture()
    monkeypatch.setattr(forge_protocol, "run_planner_turn", blocking_planner)

    regen_thread = threading.Thread(
        target=bridge.process_line,
        args=(
            _request(
                "forge.plan.regenerate",
                {
                    "session_id": session_id,
                    "plan_id": plan_id,
                    "workspace_trusted": True,
                    "instruction": "Refresh the plan without freezing the bridge.",
                },
                request_id="regen-sync",
            )
            + "\n",
        ),
        daemon=True,
    )
    regen_thread.start()
    try:
        assert planner_started.wait(timeout=3.0)
        probe_done = threading.Event()

        def probe() -> None:
            bridge.process_line(_request("session.list", request_id="probe-during-regen") + "\n")
            probe_done.set()

        probe_thread = threading.Thread(target=probe, daemon=True)
        probe_thread.start()
        assert probe_done.wait(timeout=2.0), (
            "session.list blocked while the sync forge.plan.regenerate planner was "
            "running; _state_lock must not be held across the planner call"
        )
    finally:
        planner_release.set()
        regen_thread.join(timeout=5.0)

    assert not regen_thread.is_alive()
    probe_response = _response_by_id(out, "probe-during-regen")
    assert probe_response["ok"] is True
    regen_response = _response_by_id(out, "regen-sync")
    assert regen_response["ok"] is True
    assert regen_response["result"]["audit"]["operation"] == "forge.plan.regenerate"


def test_stdio_bridge_forge_plan_regenerate_start_dispatches_and_cancels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare a plan for async regeneration.",
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]
    revision_before = int(_persisted_plan(tmp_path, plan_id).get("ide_revision", 0) or 0)
    planner_started, planner_release, blocking_planner = _blocking_planner_fixture()
    monkeypatch.setattr(forge_protocol, "run_planner_turn", blocking_planner)

    try:
        start_response = _send_bridge_request(
            bridge,
            out,
            "forge.plan.regenerate.start",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "instruction": "Refresh the plan in the background.",
            },
            request_id="regen-start",
        )
        assert start_response["ok"] is True
        assert start_response["result"]["status"] == "started"
        assert start_response["result"]["plan_id"] == plan_id
        job_id = start_response["result"]["job_id"]
        assert planner_started.wait(timeout=3.0)

        bridge.process_line(
            _request("job.status", {"job_id": job_id}, request_id="regen-running") + "\n"
        )
        running = _response_by_id(out, "regen-running")
        assert running["ok"] is True
        assert running["result"]["kind"] == "forge_plan_regenerate"
        assert running["result"]["status"] == "running"
        assert running["result"]["plan_id"] == plan_id

        bridge.process_line(_request("session.list", request_id="list-during-regen") + "\n")
        assert _response_by_id(out, "list-during-regen")["ok"] is True

        bridge.process_line(
            _request(
                "forge.plan.regenerate.result",
                {"job_id": job_id},
                request_id="regen-progress",
            )
            + "\n"
        )
        progress = _response_by_id(out, "regen-progress")
        assert progress["ok"] is True
        assert progress["result"]["complete"] is False
        assert progress["result"]["status"] == "running"
        assert progress["result"]["cancellable"] is True

        bridge.process_line(
            _request(
                "forge.cancel",
                {"session_id": session_id, "plan_id": plan_id},
                request_id="regen-cancel",
            )
            + "\n"
        )
        cancel_response = _response_by_id(out, "regen-cancel")
        assert cancel_response["ok"] is True
        assert cancel_response["result"]["status"] == "cancellation_requested"
        assert cancel_response["result"]["job_id"] == job_id

        _wait_for_line(
            out,
            lambda line: (
                line.get("type") == "warning_emitted"
                and "forge_plan_regenerate_cancelled"
                in str(line.get("payload", {}).get("message", ""))
            ),
            timeout=3.0,
        )

        bridge.process_line(
            _request("job.status", {"job_id": job_id}, request_id="regen-done") + "\n"
        )
        done = _response_by_id(out, "regen-done")
        assert done["ok"] is True
        assert done["result"]["status"] == "cancelled"
        assert done["result"]["exit_code"] == 130

        bridge.process_line(
            _request(
                "forge.plan.regenerate.result",
                {"job_id": job_id},
                request_id="regen-result",
            )
            + "\n"
        )
        result_response = _response_by_id(out, "regen-result")
        assert result_response["ok"] is True
        assert result_response["result"]["status"] == "cancelled"
        assert result_response["result"]["cancelled"] is True

        revision_after = int(_persisted_plan(tmp_path, plan_id).get("ide_revision", 0) or 0)
        assert revision_after == revision_before, (
            "a cancelled regeneration must not commit plan changes"
        )
    finally:
        planner_release.set()


def test_stdio_bridge_sync_forge_plan_regenerate_commit_conflict_is_stale_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare a plan for conflicting edits.",
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]

    def conflicting_planner(**kwargs: Any) -> SimpleNamespace:
        edit_done = threading.Event()

        def edit() -> None:
            bridge.process_line(
                _request(
                    "forge.plan.setGoal",
                    {
                        "session_id": session_id,
                        "plan_id": plan_id,
                        "workspace_trusted": True,
                        "goal": "Concurrent goal edit during sync regeneration",
                    },
                    request_id="conflict-goal-sync",
                )
                + "\n"
            )
            edit_done.set()

        editor = threading.Thread(target=edit, daemon=True)
        editor.start()
        assert edit_done.wait(timeout=3.0), (
            "the concurrent plan edit deadlocked; the regenerate handler is "
            "holding _state_lock across the planner call"
        )
        return _fake_planner_success(**kwargs)

    monkeypatch.setattr(forge_protocol, "run_planner_turn", conflicting_planner)

    bridge.process_line(
        _request(
            "forge.plan.regenerate",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "instruction": "Refresh the plan while a concurrent edit lands.",
            },
            request_id="regen-conflict-sync",
        )
        + "\n"
    )
    regen_response = _response_by_id(out, "regen-conflict-sync")
    assert regen_response["ok"] is False
    assert regen_response["error"]["code"] == "stale_plan_revision"

    goal_response = _response_by_id(out, "conflict-goal-sync")
    assert goal_response["ok"] is True

    persisted = _persisted_plan(tmp_path, plan_id)
    assert persisted["project_goal"] == "Concurrent goal edit during sync regeneration"
    assert int(persisted.get("ide_revision", 0) or 0) == 1


def test_stdio_bridge_async_forge_plan_regenerate_commit_conflict_is_stale_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare a plan for async conflicting edits.",
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]

    def conflicting_planner(**kwargs: Any) -> SimpleNamespace:
        edit_done = threading.Event()

        def edit() -> None:
            bridge.process_line(
                _request(
                    "forge.plan.setGoal",
                    {
                        "session_id": session_id,
                        "plan_id": plan_id,
                        "workspace_trusted": True,
                        "goal": "Concurrent goal edit during async regeneration",
                    },
                    request_id="conflict-goal-async",
                )
                + "\n"
            )
            edit_done.set()

        editor = threading.Thread(target=edit, daemon=True)
        editor.start()
        assert edit_done.wait(timeout=3.0)
        return _fake_planner_success(**kwargs)

    monkeypatch.setattr(forge_protocol, "run_planner_turn", conflicting_planner)

    start_response = _send_bridge_request(
        bridge,
        out,
        "forge.plan.regenerate.start",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "instruction": "Refresh the plan while a concurrent edit lands.",
        },
        request_id="regen-conflict-start",
    )
    assert start_response["ok"] is True
    job_id = start_response["result"]["job_id"]

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "error_raised"
            and line.get("payload", {}).get("code") == "stale_plan_revision"
        ),
        timeout=3.0,
    )

    bridge.process_line(
        _request("job.status", {"job_id": job_id}, request_id="conflict-job") + "\n"
    )
    job_response = _response_by_id(out, "conflict-job")
    assert job_response["ok"] is True
    assert job_response["result"]["status"] == "failed"

    bridge.process_line(
        _request(
            "forge.plan.regenerate.result",
            {"job_id": job_id},
            request_id="conflict-result",
        )
        + "\n"
    )
    result_response = _response_by_id(out, "conflict-result")
    assert result_response["ok"] is False
    assert result_response["error"]["code"] == "stale_plan_revision"

    persisted = _persisted_plan(tmp_path, plan_id)
    assert persisted["project_goal"] == "Concurrent goal edit during async regeneration"


def test_stdio_bridge_async_forge_plan_regenerate_completes_with_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare a plan for async regeneration completion.",
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]

    def renaming_planner(**kwargs: Any) -> SimpleNamespace:
        result = _fake_planner_success(**kwargs)
        result.plan_update = dict(result.plan_update)
        result.plan_update["project_goal"] = "Async regenerated goal"
        return result

    monkeypatch.setattr(forge_protocol, "run_planner_turn", renaming_planner)

    start_response = _send_bridge_request(
        bridge,
        out,
        "forge.plan.regenerate.start",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "instruction": "Refresh the plan goal in the background.",
        },
        request_id="regen-complete-start",
    )
    assert start_response["ok"] is True
    job_id = start_response["result"]["job_id"]

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and "forge_plan_regenerated" in str(line.get("payload", {}).get("message", ""))
        ),
        timeout=3.0,
    )

    bridge.process_line(
        _request(
            "forge.plan.regenerate.result",
            {"job_id": job_id},
            request_id="regen-complete-result",
        )
        + "\n"
    )
    result_response = _response_by_id(out, "regen-complete-result")
    assert result_response["ok"] is True
    assert result_response["result"]["changed"] is True
    assert result_response["result"]["old_revision"] == 0
    assert result_response["result"]["new_revision"] == 1
    assert result_response["result"]["audit"]["operation"] == "forge.plan.regenerate"
    assert result_response["result"]["redacted"] is True
    assert result_response["result"]["secret_values_included"] is False

    persisted = _persisted_plan(tmp_path, plan_id)
    assert persisted["project_goal"] == "Async regenerated goal"
    assert int(persisted["ide_revision"]) == 1


def test_stdio_bridge_trace_clear_preserves_session_event_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare a plan so the session has retained events.",
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]

    events_before = _send_bridge_request(
        bridge,
        out,
        "session.getEvents",
        {"session_id": session_id, "max_events": 100},
        request_id="events-before-clear",
    )
    assert events_before["ok"] is True
    assert events_before["result"]["events"], "plan creation should retain replay events"
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

    bridge.process_line(
        _request(
            "forge.plan.setGoal",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "goal": "Replay survives trace clear",
            },
            request_id="goal-after-clear",
        )
        + "\n"
    )
    goal_response = _response_by_id(out, "goal-after-clear")
    assert goal_response["ok"] is True

    bridge.process_line(
        _request(
            "session.trace.listEvents",
            {"session_id": session_id},
            request_id="trace-new-events",
        )
        + "\n"
    )
    trace_new = _response_by_id(out, "trace-new-events")
    assert trace_new["ok"] is True
    assert trace_new["result"]["count"] >= 1, (
        "events emitted after session.trace.clear must be visible in the trace view"
    )


def test_stdio_bridge_forge_assets_reject_invalid_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare Forge asset path safety.",
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]
    outside = tmp_path.parent / f"{tmp_path.name}_outside.md"
    outside.write_text("outside\n", encoding="utf-8")

    def add_with(source: str, request_id: str) -> dict[str, Any]:
        bridge.process_line(
            _request(
                "forge.assets.add",
                {
                    "session_id": session_id,
                    "plan_id": plan_id,
                    "workspace_trusted": True,
                    "source_path": source,
                    "title": "Bad asset",
                },
                request_id=request_id,
            )
            + "\n"
        )
        return _response_by_id(out, request_id)

    outside_response = add_with(os.fspath(outside), "outside")
    assert outside_response["ok"] is False
    assert outside_response["error"]["code"] == "asset_path_outside_workspace"

    missing_response = add_with("missing.md", "missing")
    assert missing_response["ok"] is False
    assert missing_response["error"]["code"] == "asset_not_found"

    unsupported = tmp_path / "unsupported.bin"
    unsupported.write_bytes(b"\x00\x01binary")
    unsupported_response = add_with("unsupported.bin", "unsupported")
    assert unsupported_response["ok"] is False
    assert unsupported_response["error"]["code"] == "asset_error"
    assert "Unsupported asset file type" in unsupported_response["error"]["message"]

    huge = tmp_path / "huge.md"
    with huge.open("wb") as handle:
        handle.seek(forge_protocol.MAX_FORGE_ASSET_BYTES)
        handle.write(b"x")
    huge_response = add_with("huge.md", "huge")
    assert huge_response["ok"] is False
    assert huge_response["error"]["code"] == "asset_too_large"

    symlink = tmp_path / "outside-link.md"
    _symlink_or_skip(symlink, outside, target_is_directory=False)
    symlink_response = add_with("outside-link.md", "symlink")
    assert symlink_response["ok"] is False
    assert symlink_response["error"]["code"] == "asset_symlink_rejected"


def test_stdio_bridge_forge_plan_creates_session_schema_events_and_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=False, status="missing"),
    )
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )

    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Add a safe IDE Forge protocol foundation.",
            },
            request_id="plan",
        )
        + "\n"
    )

    lines = _json_lines(out)
    response = [line for line in lines if line.get("id") == "plan"][0]
    assert response["ok"] is True
    result = response["result"]
    assert result["created_session"] is True
    assert result["plan_id"]
    assert result["session_id"]
    assert result["job_id"] is None
    assert result["status"] == "planned"
    assert result["source"] == "active_memory"
    assert result["incomplete"] is False
    assert result["tasks"][0]["task_id"] == "T01"
    assert result["tasks"][0]["title"] == "Wire IDE Forge Plan to planner output"
    assert result["tasks"][0]["file_scope"] == {
        "estimated_files": ["demo.py"],
        "write_scope": ["demo.py"],
    }
    assert result["tasks"][0]["acceptance_criteria"]
    assert result["tasks"][0]["verification_commands"] == ["pytest -q"]
    assert any(line.get("type") == "status_update" for line in lines)
    assert any(line.get("type") == "plan_node_updated" for line in lines)
    assert any(line.get("type") == "info_emitted" for line in lines)

    bridge.process_line(
        _request(
            "artifact.read",
            {
                "session_id": result["session_id"],
                "artifact_id": result["plan_artifact_id"],
            },
            request_id="artifact",
        )
        + "\n"
    )
    artifact_response = [line for line in _json_lines(out) if line.get("id") == "artifact"][0]
    assert artifact_response["ok"] is True
    assert '"tasks"' in artifact_response["result"]["content"]


def test_stdio_bridge_forge_plan_start_runs_background_job_and_replays_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    planner_started = threading.Event()
    planner_release = threading.Event()

    def blocking_planner(**kwargs: Any) -> SimpleNamespace:
        planner_started.set()
        cancellation_token = kwargs.get("cancellation_token")
        deadline = time.monotonic() + 3.0
        while not planner_release.wait(timeout=0.01):
            if cancellation_token is not None and getattr(
                cancellation_token, "is_cancelled", False
            ):
                cancellation_token.throw_if_cancelled("planner_cancelled")
            assert time.monotonic() < deadline
        return _fake_planner_success(**kwargs)

    monkeypatch.setattr(forge_protocol, "run_planner_turn", blocking_planner)
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )

    bridge.process_line(
        _request(
            "forge.plan.start",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Prepare async Forge Plan.",
            },
            request_id="start",
        )
        + "\n"
    )
    start_response = [line for line in _json_lines(out) if line.get("id") == "start"][0]
    assert start_response["ok"] is True
    assert start_response["result"]["status"] == "started"
    session_id = start_response["result"]["session_id"]
    job_id = start_response["result"]["job_id"]
    assert planner_started.wait(timeout=3.0)

    bridge.process_line(_request("job.status", {"job_id": job_id}, request_id="running") + "\n")
    running_response = [line for line in _json_lines(out) if line.get("id") == "running"][0]
    assert running_response["ok"] is True
    assert running_response["result"]["kind"] == "forge_plan"
    assert running_response["result"]["status"] == "running"
    assert running_response["result"]["plan_id"] is None

    bridge.process_line(
        _request(
            "session.cancel",
            {"session_id": session_id},
            request_id="cancel-running-plan",
        )
        + "\n"
    )
    cancel_response = [
        line for line in _json_lines(out) if line.get("id") == "cancel-running-plan"
    ][0]
    assert cancel_response["ok"] is True
    assert cancel_response["result"]["status"] == "cancellation_requested"
    assert cancel_response["result"]["state"] == "cancellation_requested"
    assert cancel_response["result"]["job_id"] == job_id

    cancelled = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "warning_emitted"
            and "forge_plan_cancelled" in str(line.get("payload", {}).get("message", ""))
        ),
    )
    assert "cancelled_by_user" in str(cancelled.get("payload", {}).get("message", ""))

    bridge.process_line(_request("job.status", {"job_id": job_id}, request_id="done") + "\n")
    done_response = [line for line in _json_lines(out) if line.get("id") == "done"][0]
    assert done_response["ok"] is True
    assert done_response["result"]["status"] == "cancelled"
    assert done_response["result"]["state"] == "cancelled"
    assert done_response["result"]["plan_id"] is None
    assert done_response["result"]["exit_code"] == 130

    bridge.process_line(
        _request("forge.plan.result", {"job_id": job_id}, request_id="result") + "\n"
    )
    result_response = [line for line in _json_lines(out) if line.get("id") == "result"][0]
    assert result_response["ok"] is True
    assert result_response["result"]["job_id"] == job_id
    assert result_response["result"]["status"] == "cancelled"
    assert result_response["result"]["cancelled"] is True

    bridge.process_line(
        _request(
            "forge.status",
            {"session_id": session_id, "job_id": job_id},
            request_id="status-by-job",
        )
        + "\n"
    )
    status_response = [line for line in _json_lines(out) if line.get("id") == "status-by-job"][0]
    assert status_response["ok"] is False
    assert status_response["error"]["code"] == "forge_plan_failed"

    bridge.process_line(
        _request(
            "session.getEvents",
            {"session_id": session_id, "max_events": 50},
            request_id="events",
        )
        + "\n"
    )
    replay_response = [line for line in _json_lines(out) if line.get("id") == "events"][0]
    assert replay_response["ok"] is True
    replayed_types = {event["type"] for event in replay_response["result"]["events"]}
    assert {"status_update", "warning_emitted"} <= replayed_types


def test_stdio_bridge_forge_plan_start_reports_redacted_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    monkeypatch.setattr(
        forge_protocol,
        "run_planner_turn",
        lambda **_: SimpleNamespace(
            assistant_message="",
            questions=[],
            plan_update=None,
            error="provider rejected sk-asyncfailuresecret123",
            planner_router_event=None,
        ),
    )
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )

    bridge.process_line(
        _request(
            "forge.plan.start",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Fail safely.",
            },
            request_id="start",
        )
        + "\n"
    )
    start_response = [line for line in _json_lines(out) if line.get("id") == "start"][0]
    job_id = start_response["result"]["job_id"]
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "error_raised"
            and line.get("payload", {}).get("code") == "forge_plan_failed"
        ),
    )

    bridge.process_line(_request("job.status", {"job_id": job_id}, request_id="job") + "\n")
    job_response = [line for line in _json_lines(out) if line.get("id") == "job"][0]
    assert job_response["ok"] is True
    assert job_response["result"]["status"] == "failed"

    bridge.process_line(_request("session.list", request_id="sessions-after-failed-plan") + "\n")
    session_response = _response_by_id(out, "sessions-after-failed-plan")
    session_summary = session_response["result"]["sessions"][0]
    assert session_summary["active_job"] is None
    assert session_summary["last_job"]["job_id"] == job_id
    assert session_summary["last_job"]["status"] == "failed"

    bridge.process_line(
        _request("forge.plan.result", {"job_id": job_id}, request_id="result") + "\n"
    )
    result_response = [line for line in _json_lines(out) if line.get("id") == "result"][0]
    assert result_response["ok"] is False
    assert result_response["error"]["code"] == "forge_plan_failed"
    assert "sk-asyncfailuresecret123" not in json.dumps(_json_lines(out))


def test_stdio_bridge_forge_plan_idempotency_survives_bridge_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (workspace / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    calls = 0

    def planner(**kwargs: Any) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return _fake_planner_success(**kwargs)

    monkeypatch.setattr(forge_protocol, "run_planner_turn", planner)
    ledger_path = tmp_path / "private" / "forge.sqlite3"
    request_params = {
        "workspace": os.fspath(workspace),
        "mode": "readonly",
        "model": "test-model",
        "instruction": "Prepare a restart-safe Forge Plan.",
        "idempotency_key": "forge-restart-request-1",
    }

    out1 = io.StringIO()
    bridge1 = StdioBridge(
        stdout=out1,
        create_session_fn=lambda **kwargs: _FakeBridgeSession(workspace),
        forge_request_ledger=DurableForgeRequestLedger(ledger_path),
    )
    first = _send_bridge_request(bridge1, out1, "forge.plan.start", request_params)
    assert first["ok"] is True
    job_id = first["result"]["job_id"]
    completed = _wait_for_line(
        out1,
        lambda line: (
            line.get("type") == "info_emitted"
            and "forge_plan_completed" in str(line.get("payload", {}).get("message", ""))
        ),
    )
    assert completed
    first_result = _send_bridge_request(bridge1, out1, "forge.plan.result", {"job_id": job_id})[
        "result"
    ]
    bridge1.close()

    out2 = io.StringIO()
    bridge2 = StdioBridge(
        stdout=out2,
        create_session_fn=lambda **kwargs: _FakeBridgeSession(workspace),
        forge_request_ledger=DurableForgeRequestLedger(ledger_path),
    )
    status_before_retry = _send_bridge_request(bridge2, out2, "job.status", {"job_id": job_id})
    assert status_before_retry["result"]["status"] == "completed"
    recovered_before_retry = _send_bridge_request(
        bridge2, out2, "forge.plan.result", {"job_id": job_id}
    )
    assert recovered_before_retry["ok"] is True
    assert recovered_before_retry["result"]["created_session"] is True
    attached_status = _send_bridge_request(bridge2, out2, "job.status", {"job_id": job_id})
    assert attached_status["result"]["session_id"] == recovered_before_retry["result"]["session_id"]
    retried = _send_bridge_request(bridge2, out2, "forge.plan.start", request_params)
    assert retried["ok"] is True
    assert retried["result"]["job_id"] == job_id
    assert retried["result"]["status"] == "completed"
    assert retried["result"]["duplicate"] is True
    status = _send_bridge_request(bridge2, out2, "job.status", {"job_id": job_id})
    assert status["result"]["status"] == "completed"
    recovered = _send_bridge_request(bridge2, out2, "forge.plan.result", {"job_id": job_id})
    assert recovered["ok"] is True
    assert recovered["result"]["job_id"] == job_id
    assert recovered["result"]["plan_id"] == first_result["plan_id"]
    assert recovered["result"]["source"] == "durable_idempotent_recovery"
    assert calls == 1

    conflict = _send_bridge_request(
        bridge2,
        out2,
        "forge.plan.start",
        {**request_params, "instruction": "A conflicting instruction."},
    )
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "idempotency_conflict"
    assert "restart-safe" not in json.dumps(_json_lines(out2))
    bridge2.close()


def test_stdio_bridge_forge_plan_session_busy_rejects_durable_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    out, bridge, session_id = _create_review_session(tmp_path, monkeypatch, lambda *_: 0)
    ledger = DurableForgeRequestLedger(tmp_path / "private" / "forge.sqlite3")
    bridge._forge_request_ledger = ledger
    session = bridge._sessions[session_id]
    session.active_job = stdio_bridge.BridgeJob(
        job_id="job-existing",
        session_id=session_id,
        created_at="2026-01-01T00:00:00Z",
        status="running",
    )
    bridge._jobs["job-existing"] = session.active_job
    planner_called = False

    def planner(**_: Any) -> SimpleNamespace:
        nonlocal planner_called
        planner_called = True
        raise AssertionError("busy session must not dispatch Forge planning")

    monkeypatch.setattr(forge_protocol, "run_planner_turn", planner)
    params = {
        "session_id": session_id,
        "instruction": "Do not dispatch while busy.",
        "idempotency_key": "forge-busy-request-1",
    }
    response = _send_bridge_request(bridge, out, "forge.plan.start", params)
    assert response["ok"] is False
    assert response["error"]["code"] == "session_busy"
    assert planner_called is False

    duplicate = ledger.accept(
        workspace_root=tmp_path,
        session_id=session_id,
        idempotency_key="forge-busy-request-1",
        payload={"instruction": "Do not dispatch while busy."},
    )
    assert duplicate.created is False
    assert duplicate.record.state.value == "failed"
    assert duplicate.record.error_code == "session_busy"
    assert duplicate.dispatch_lease is None
    bridge.close()


def test_stdio_bridge_forge_plan_expired_running_request_is_not_reexecuted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_alysis_state(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    now = [1_000.0]
    ledger = DurableForgeRequestLedger(
        tmp_path / "private" / "forge.sqlite3",
        config=ForgeRequestLedgerConfig(lease_seconds=2.0),
        clock=lambda: now[0],
    )
    params = {
        "workspace": os.fspath(workspace),
        "mode": "readonly",
        "instruction": "Prepare exactly once.",
        "idempotency_key": "forge-uncertain-request-1",
    }
    acceptance = ledger.accept(
        workspace_root=workspace,
        session_id="session-original",
        idempotency_key=params["idempotency_key"],
        payload={"instruction": params["instruction"], "mode": params["mode"]},
    )
    assert acceptance.dispatch_lease is not None
    ledger.begin(acceptance.dispatch_lease)
    now[0] += 3.0

    planner_called = False

    def planner(**_: Any) -> SimpleNamespace:
        nonlocal planner_called
        planner_called = True
        raise AssertionError("indeterminate work must not be executed again")

    monkeypatch.setattr(forge_protocol, "run_planner_turn", planner)
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    out = io.StringIO()
    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: _FakeBridgeSession(workspace),
        forge_request_ledger=ledger,
    )
    status = _send_bridge_request(bridge, out, "job.status", {"job_id": acceptance.record.job_id})
    assert status["ok"] is True
    assert status["result"]["status"] == "failed"
    assert status["result"]["error_code"] == "worker_lease_expired"

    duplicate = _send_bridge_request(bridge, out, "forge.plan.start", params)
    assert duplicate["ok"] is True
    assert duplicate["result"]["job_id"] == acceptance.record.job_id
    assert duplicate["result"]["status"] == "failed"
    assert duplicate["result"]["duplicate"] is True
    assert planner_called is False
    result = _send_bridge_request(
        bridge, out, "forge.plan.result", {"job_id": acceptance.record.job_id}
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "forge_plan_indeterminate"
    assert planner_called is False
    bridge.close()


def test_stdio_bridge_forge_plan_fails_closed_when_planner_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    monkeypatch.setattr(
        forge_protocol,
        "run_planner_turn",
        lambda **_: SimpleNamespace(
            assistant_message="",
            questions=[],
            plan_update=None,
            error="Planner assistant is unavailable because no API key is configured.",
            planner_router_event=None,
        ),
    )
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )

    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Create a Forge plan.",
            },
            request_id="plan",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "plan"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "forge_plan_failed"
    assert "no API key" in response["error"]["message"]


def test_stdio_bridge_forge_plan_rejects_requirements_only_planner_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    monkeypatch.setattr(
        forge_protocol,
        "run_planner_turn",
        lambda **_: SimpleNamespace(
            assistant_message="Captured a requirement only.",
            questions=[],
            error=None,
            plan_update={"requirements_add": ["Do the requested work without task detail."]},
            planner_router_event=None,
        ),
    )
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )

    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Create a taskless plan.",
            },
            request_id="plan",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "plan"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "forge_plan_incomplete"


def test_stdio_bridge_forge_plan_emits_incomplete_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_incomplete)
    monkeypatch.setattr(forge_protocol, "resolve_verify_commands", lambda **_: [])
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )

    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Investigate a risky unclear change.",
            },
            request_id="plan",
        )
        + "\n"
    )

    lines = _json_lines(out)
    response = [line for line in lines if line.get("id") == "plan"][0]
    assert response["ok"] is True
    result = response["result"]
    assert result["incomplete"] is True
    assert any("missing acceptance criteria" in warning for warning in result["warnings"])
    assert any("missing verification commands" in warning for warning in result["warnings"])
    assert any(line.get("type") == "warning_emitted" for line in lines)


def test_stdio_bridge_forge_status_and_execute_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    monkeypatch.setenv("ALYSIS_API_KEY", "preview-secret-value")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=False, status="missing"),
    )
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Prepare execution foundations.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]

    bridge.process_line(
        _request(
            "forge.status",
            {"session_id": plan_result["session_id"], "plan_id": plan_result["plan_id"]},
            request_id="status",
        )
        + "\n"
    )
    status_response = [line for line in _json_lines(out) if line.get("id") == "status"][0]
    assert status_response["ok"] is True
    assert status_response["result"]["plan_id"] == plan_result["plan_id"]

    before_preview_files = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    bridge.process_line(
        _request(
            "forge.executePreview",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "review",
                "workspace_trusted": True,
                "sandbox_profile": "default",
            },
            request_id="preview",
        )
        + "\n"
    )
    after_preview_files = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after_preview_files == before_preview_files
    preview_response = [line for line in _json_lines(out) if line.get("id") == "preview"][0]
    assert preview_response["ok"] is True
    preview = preview_response["result"]
    assert preview["selected_task_ids"] == ["T01"]
    assert preview["execution_mode_requested"] == "review"
    assert preview["workspace_trust_required"] is True
    assert preview["workspace_trusted"] is True
    assert preview["preview_ready"] is False
    assert preview["real_execution_supported"] is True
    assert preview["active_cancellation_supported"] is True
    assert preview["cancellation"] == {
        "supported": True,
        "kind": "cooperative_checkpoint",
        "hard_interrupt": False,
    }
    assert preview["approval_scopes_safe"] is True
    assert preview["sandbox_profile"]["supported"] is True
    assert preview["sandbox_profile"]["available"] is False
    assert "doctor sandbox" in "\n".join(preview["missing_prerequisites"])
    assert preview["estimated_file_scopes"][0]["write_scope"] == ["demo.py"]
    assert preview["verification_commands"][0]["commands"] == ["pytest -q"]
    approval_kinds = {item["kind"] for item in preview["required_approvals"]}
    assert {"fs_write", "verify_run"} <= approval_kinds
    runtime_approval_kinds = {item["kind"] for item in preview["runtime_approval_requirements"]}
    assert {"shell_run", "custom_tool_run", "mcp_tool_run"} <= runtime_approval_kinds
    shell_requirement = [
        item for item in preview["runtime_approval_requirements"] if item["kind"] == "shell_run"
    ][0]
    assert shell_requirement["scope_requirement"]["type"] == "exact_command_hash"
    custom_requirement = [
        item
        for item in preview["runtime_approval_requirements"]
        if item["kind"] == "custom_tool_run"
    ][0]
    assert custom_requirement["allow_for_session_supported"] is False
    assert any(
        item["allow_for_session_scope"]["type"] == "exact_file_set"
        for item in preview["required_approvals"]
    )
    assert any(
        item["allow_for_session_scope"]["type"] == "exact_verify_command_set"
        for item in preview["required_approvals"]
    )
    assert "preview-secret-value" not in json.dumps(preview_response)

    bridge.process_line(
        _request(
            "forge.execute",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "review",
                "dry_run": True,
                "workspace_trusted": True,
            },
            request_id="execute-dry-run",
        )
        + "\n"
    )
    dry_run_response = [line for line in _json_lines(out) if line.get("id") == "execute-dry-run"][0]
    assert dry_run_response["ok"] is True
    assert dry_run_response["result"]["status"] == "preview"
    assert dry_run_response["result"]["preview_ready"] is False

    bridge.process_line(
        _request(
            "forge.execute",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "review",
            },
            request_id="execute",
        )
        + "\n"
    )
    execute_response = [line for line in _json_lines(out) if line.get("id") == "execute"][0]
    assert execute_response["ok"] is False
    assert execute_response["error"]["code"] == "forge_execute_prerequisites_failed"
    assert "doctor sandbox" in execute_response["error"]["message"]

    bridge.process_line(
        _request(
            "forge.execute",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "mode": "readonly",
            },
            request_id="execute-readonly",
        )
        + "\n"
    )
    readonly_response = [line for line in _json_lines(out) if line.get("id") == "execute-readonly"][
        0
    ]
    assert readonly_response["ok"] is False
    assert readonly_response["error"]["code"] == "invalid_mode"

    bridge.process_line(
        _request(
            "forge.cancel",
            {"session_id": plan_result["session_id"], "plan_id": plan_result["plan_id"]},
            request_id="cancel-forge",
        )
        + "\n"
    )
    cancel_response = [line for line in _json_lines(out) if line.get("id") == "cancel-forge"][0]
    assert cancel_response["ok"] is True
    assert cancel_response["result"]["status"] == "no_active_job"
    assert cancel_response["result"]["state"] == "idle"


def test_stdio_bridge_forge_execute_v1_boundary_rejects_unsupported_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=True),
    )
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan v1 boundary execution.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]
    base_params = {
        "session_id": plan_result["session_id"],
        "plan_id": plan_result["plan_id"],
        "task_ids": ["T01"],
        "workspace_trusted": True,
        "sandbox_profile": "strict",
    }

    bridge.process_line(
        _request("forge.execute", {**base_params, "mode": "auto"}, request_id="execute-auto") + "\n"
    )
    _assert_protocol_error(
        out,
        request_id="execute-auto",
        code="forge_execute_unsupported",
        message_contains="review mode only",
    )

    bridge.process_line(
        _request(
            "forge.execute",
            {**base_params, "mode": "fullaccess"},
            request_id="execute-fullaccess",
        )
        + "\n"
    )
    _assert_protocol_error(
        out,
        request_id="execute-fullaccess",
        code="invalid_mode",
        message_contains="mode must be review or auto",
    )

    missing_task_params = dict(base_params)
    missing_task_params.pop("task_ids")
    bridge.process_line(
        _request(
            "forge.execute",
            {**missing_task_params, "mode": "review"},
            request_id="execute-missing-tasks",
        )
        + "\n"
    )
    _assert_protocol_error(
        out,
        request_id="execute-missing-tasks",
        code="missing_field",
        message_contains="explicit task_ids",
    )

    missing_trust_params = dict(base_params)
    missing_trust_params.pop("workspace_trusted")
    bridge.process_line(
        _request(
            "forge.execute",
            {**missing_trust_params, "mode": "review"},
            request_id="execute-missing-trust",
        )
        + "\n"
    )
    _assert_protocol_error(
        out,
        request_id="execute-missing-trust",
        code="forge_execute_prerequisites_failed",
        message_contains="Workspace Trust status is required",
    )

    bridge.process_line(
        _request(
            "forge.execute",
            {**base_params, "mode": "review", "workspace_trusted": False},
            request_id="execute-untrusted",
        )
        + "\n"
    )
    _assert_protocol_error(
        out,
        request_id="execute-untrusted",
        code="forge_execute_prerequisites_failed",
        message_contains="Workspace Trust is required",
    )

    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=False, status="missing"),
    )
    bridge.process_line(
        _request(
            "forge.execute",
            {**base_params, "mode": "review"},
            request_id="execute-missing-sandbox",
        )
        + "\n"
    )
    _assert_protocol_error(
        out,
        request_id="execute-missing-sandbox",
        code="forge_execute_prerequisites_failed",
        message_contains="doctor sandbox",
    )


def test_stdio_bridge_forge_execute_requires_complete_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "missing.py").write_text("print('missing')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_incomplete)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=True),
    )
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan incomplete execution.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]

    bridge.process_line(
        _request(
            "forge.execute",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "review",
                "workspace_trusted": True,
                "sandbox_profile": "strict",
            },
            request_id="execute-incomplete",
        )
        + "\n"
    )
    _assert_protocol_error(
        out,
        request_id="execute-incomplete",
        code="forge_execute_prerequisites_failed",
        message_contains="acceptance criteria are required",
    )


def test_stdio_bridge_forge_execute_review_job_uses_scoped_approvals_and_diffs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=True),
    )
    monkeypatch.setattr(forge_protocol, "run_task_verification", _fake_verify_success)
    runner_calls: list[dict[str, Any]] = []

    def recording_forge_execute_agent(**kwargs: Any) -> int:
        runner_calls.append(dict(kwargs))
        return _fake_forge_execute_agent(**kwargs)

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path),
        forge_execute_agent_runner=recording_forge_execute_agent,
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan review execution.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]

    bridge.process_line(
        _request(
            "forge.execute",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "review",
                "workspace_trusted": True,
                "sandbox_profile": "strict",
                "max_steps": 3,
                "no_log": True,
            },
            request_id="execute",
        )
        + "\n"
    )
    execute_response = [line for line in _json_lines(out) if line.get("id") == "execute"][0]
    assert execute_response["ok"] is True
    job_id = execute_response["result"]["job_id"]

    handled: set[str] = set()
    for expected_kind, expected_scope in [
        ("fs_write", "exact_file_set"),
        ("verify_run", "exact_verify_command_set"),
    ]:
        prompt = _wait_for_line(
            out,
            lambda line: (
                line.get("type") == "prompt_for_input"
                and line.get("payload", {}).get("kind") == "approval"
                and line.get("payload", {}).get("approval_id") not in handled
            ),
        )
        payload = prompt["payload"]
        handled.add(payload["approval_id"])
        assert payload["metadata"].get("kind", expected_kind) in {expected_kind, None}
        assert payload["allow_for_session_supported"] is True
        assert payload["allow_for_session_scope"]["type"] == expected_scope
        bridge.process_line(
            _request(
                "approval.respond",
                {
                    "session_id": plan_result["session_id"],
                    "approval_id": payload["approval_id"],
                    "allow": True,
                    "allow_for_session": True,
                },
                request_id=f"approve-{expected_kind}",
            )
            + "\n"
        )

    review_event = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "review_gate_decision"
            and line.get("payload", {}).get("decision") == "accepted"
        ),
    )
    terminal_event = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and line.get("job_id") == job_id
            and str(line.get("payload", {}).get("message", "")).startswith(
                "forge_execute_completed "
            )
        ),
    )
    assert terminal_event["sequence"] > review_event["sequence"]

    bridge.process_line(_request("job.status", {"job_id": job_id}, request_id="job") + "\n")
    job_response = [line for line in _json_lines(out) if line.get("id") == "job"][0]
    assert job_response["ok"] is True
    assert job_response["result"]["kind"] == "forge_execute"
    assert job_response["result"]["status"] == "completed"
    assert job_response["result"]["exit_code"] == 0
    assert job_response["result"]["completed_at"]
    assert runner_calls
    assert runner_calls[0]["max_steps"] == 3
    assert runner_calls[0]["no_log"] is True
    assert runner_calls[0]["subagents_enabled"] is False

    bridge.process_line(_request("session.list", request_id="sessions-after-execute") + "\n")
    session_response = [
        line for line in _json_lines(out) if line.get("id") == "sessions-after-execute"
    ][0]
    session_summary = session_response["result"]["sessions"][0]
    assert session_summary["active_job"] is None
    assert session_summary["last_job"]["job_id"] == job_id
    assert session_summary["last_job"]["status"] == "completed"
    assert session_summary["last_job"]["exit_code"] == 0

    bridge.process_line(
        _request(
            "session.getEvents",
            {"session_id": plan_result["session_id"], "max_events": 100},
            request_id="events-after-execute",
        )
        + "\n"
    )
    replay_response = [
        line for line in _json_lines(out) if line.get("id") == "events-after-execute"
    ][0]
    replayed_terminal_events = [
        event
        for event in replay_response["result"]["events"]
        if event["type"] == "info_emitted"
        and event.get("job_id") == job_id
        and str(event.get("payload", {}).get("message", "")).startswith("forge_execute_completed ")
    ]
    assert replayed_terminal_events
    assert replayed_terminal_events[-1]["sequence"] == terminal_event["sequence"]

    bridge.process_line(
        _request(
            "chat.send",
            {"session_id": plan_result["session_id"], "message": "follow-up"},
            request_id="chat-after-execute",
        )
        + "\n"
    )
    chat_response = [line for line in _json_lines(out) if line.get("id") == "chat-after-execute"][0]
    assert chat_response["ok"] is True
    chat_job_id = chat_response["result"]["job_id"]
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and line.get("job_id") == chat_job_id
            and str(line.get("payload", {}).get("message", "")).startswith("job_completed ")
        ),
    )

    bridge.process_line(
        _request(
            "forge.status",
            {"session_id": plan_result["session_id"], "plan_id": plan_result["plan_id"]},
            request_id="status-after-execute",
        )
        + "\n"
    )
    status_response = [
        line for line in _json_lines(out) if line.get("id") == "status-after-execute"
    ][0]
    assert status_response["ok"] is True
    assert status_response["result"]["tasks"][0]["status"] == "done"

    bridge.process_line(
        _request(
            "diff.list",
            {"session_id": plan_result["session_id"], "plan_id": plan_result["plan_id"]},
            request_id="diffs",
        )
        + "\n"
    )
    diff_response = [line for line in _json_lines(out) if line.get("id") == "diffs"][0]
    assert diff_response["ok"] is True
    assert diff_response["result"]["diffs"]

    bridge.process_line(
        _request(
            "artifact.list",
            {"session_id": plan_result["session_id"]},
            request_id="artifacts-after-execute",
        )
        + "\n"
    )
    artifact_response = [
        line for line in _json_lines(out) if line.get("id") == "artifacts-after-execute"
    ][0]
    artifact_ids = {item["artifact_id"] for item in artifact_response["result"]["artifacts"]}
    forge_root = forge_protocol.forge_artifact_root_name(plan_result["plan_id"])
    assert any(
        artifact_id.startswith(f"{forge_root}:execution/reports/") for artifact_id in artifact_ids
    )
    assert any(
        artifact_id.startswith(f"{forge_root}:execution/patches/") for artifact_id in artifact_ids
    )
    assert any(
        artifact_id.startswith(f"{forge_root}:execution/context/") for artifact_id in artifact_ids
    )

    assert "executed" in (tmp_path / "demo.py").read_text(encoding="utf-8")
    assert "test-model" in json.dumps(_json_lines(out))
    assert "ALYSIS_API_KEY" not in json.dumps(_json_lines(out))


def test_stdio_bridge_forge_execute_active_job_can_be_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=True),
    )
    monkeypatch.setattr(forge_protocol, "run_task_verification", _fake_verify_success)
    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path),
        forge_execute_agent_runner=_fake_forge_execute_agent,
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan cancellable review execution.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]

    bridge.process_line(
        _request(
            "forge.execute",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "review",
                "workspace_trusted": True,
                "sandbox_profile": "strict",
            },
            request_id="execute",
        )
        + "\n"
    )
    execute_response = [line for line in _json_lines(out) if line.get("id") == "execute"][0]
    assert execute_response["ok"] is True
    job_id = execute_response["result"]["job_id"]

    prompt = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )
    approval_id = prompt["payload"]["approval_id"]

    bridge.process_line(
        _request(
            "session.cancel",
            {"session_id": plan_result["session_id"], "reason": "stop bearer sk-cancel-secret"},
            request_id="cancel-running-execute",
        )
        + "\n"
    )
    cancel_response = [
        line for line in _json_lines(out) if line.get("id") == "cancel-running-execute"
    ][0]
    assert cancel_response["ok"] is True
    assert cancel_response["result"]["status"] == "cancellation_requested"
    assert cancel_response["result"]["job_id"] == job_id
    assert "sk-cancel-secret" not in json.dumps(cancel_response)

    approval_result = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval_result"
            and line.get("payload", {}).get("approval_id") == approval_id
        ),
    )
    assert str(approval_result["payload"]["status"]).startswith("cancelled:")
    assert "sk-cancel-secret" not in json.dumps(approval_result)
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "warning_emitted"
            and "forge_execute_cancelled" in str(line.get("payload", {}).get("message", ""))
        ),
    )

    bridge.process_line(_request("job.status", {"job_id": job_id}, request_id="job") + "\n")
    bridge.process_line(_request("session.list", request_id="sessions-after-cancel") + "\n")
    lines = _json_lines(out)
    job_response = [line for line in lines if line.get("id") == "job"][0]
    assert job_response["ok"] is True
    assert job_response["result"]["status"] == "cancelled"
    assert job_response["result"]["state"] == "cancelled"
    assert job_response["result"]["exit_code"] == 130
    assert "sk-cancel-secret" not in json.dumps(job_response)
    session_summary = [line for line in lines if line.get("id") == "sessions-after-cancel"][0][
        "result"
    ]["sessions"][0]
    assert session_summary["active_job"] is None
    assert session_summary["last_job"]["job_id"] == job_id
    assert session_summary["last_job"]["status"] == "cancelled"
    assert "executed" not in (tmp_path / "demo.py").read_text(encoding="utf-8")


def test_stdio_bridge_forge_execute_verification_failure_blocks_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=True),
    )
    monkeypatch.setattr(forge_protocol, "run_task_verification", _fake_verify_failure)
    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path),
        forge_execute_agent_runner=_fake_forge_execute_agent,
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan review execution failure.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]
    bridge.process_line(
        _request(
            "forge.execute",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "review",
                "workspace_trusted": True,
                "sandbox_profile": "strict",
            },
            request_id="execute",
        )
        + "\n"
    )
    job_id = [line for line in _json_lines(out) if line.get("id") == "execute"][0]["result"][
        "job_id"
    ]
    handled: set[str] = set()
    for _ in range(2):
        prompt = _wait_for_line(
            out,
            lambda line: (
                line.get("type") == "prompt_for_input"
                and line.get("payload", {}).get("kind") == "approval"
                and line.get("payload", {}).get("approval_id") not in handled
            ),
        )
        approval_id = prompt["payload"]["approval_id"]
        handled.add(approval_id)
        bridge.process_line(
            _request(
                "approval.respond",
                {
                    "session_id": plan_result["session_id"],
                    "approval_id": approval_id,
                    "allow": True,
                    "allow_for_session": False,
                },
                request_id=f"approve-{len(handled)}",
            )
            + "\n"
        )

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "verify_gate_result"
            and line.get("payload", {}).get("success") is False
        ),
    )
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "review_gate_decision"
            and line.get("payload", {}).get("decision") == "blocked"
        ),
    )
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and line.get("job_id") == job_id
            and str(line.get("payload", {}).get("message", "")).startswith(
                "forge_execute_completed "
            )
        ),
    )
    bridge.process_line(_request("job.status", {"job_id": job_id}, request_id="job") + "\n")
    job_response = [line for line in _json_lines(out) if line.get("id") == "job"][0]
    assert job_response["ok"] is True
    # Data-outcome job status: the run finished, so the job is completed and
    # the failure lives in the exit code + result payload.
    assert job_response["result"]["status"] == "completed"
    assert job_response["result"]["exit_code"] == 1
    assert job_response["result"]["completed_at"]

    bridge.process_line(_request("session.list", request_id="sessions-after-failed-execute") + "\n")
    session_response = _response_by_id(out, "sessions-after-failed-execute")
    session_summary = session_response["result"]["sessions"][0]
    assert session_summary["active_job"] is None
    assert session_summary["last_job"]["job_id"] == job_id
    assert session_summary["last_job"]["status"] == "completed"
    assert session_summary["last_job"]["exit_code"] == 1

    bridge.process_line(
        _request(
            "forge.status",
            {"session_id": plan_result["session_id"], "plan_id": plan_result["plan_id"]},
            request_id="status-after-execute",
        )
        + "\n"
    )
    status_response = [
        line for line in _json_lines(out) if line.get("id") == "status-after-execute"
    ][0]
    assert status_response["ok"] is True
    assert status_response["result"]["tasks"][0]["status"] == "verify_failed"


def test_stdio_bridge_forge_execute_blocks_dependent_task_after_failed_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    (tmp_path / "dependent.py").write_text("print('dependent')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_two_task_dependency)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=True),
    )
    monkeypatch.setattr(forge_protocol, "run_task_verification", _fake_verify_failure)
    calls: list[str] = []

    def agent(**kwargs: Any) -> int:
        calls.append(str(kwargs.get("session_id_override") or ""))
        return _fake_forge_execute_agent(**kwargs)

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path),
        forge_execute_agent_runner=agent,
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan dependent review execution.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]
    assert [task["task_id"] for task in plan_result["tasks"]] == ["T01", "T02"]

    bridge.process_line(
        _request(
            "forge.execute",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01", "T02"],
                "mode": "review",
                "workspace_trusted": True,
                "sandbox_profile": "strict",
            },
            request_id="execute",
        )
        + "\n"
    )
    job_id = [line for line in _json_lines(out) if line.get("id") == "execute"][0]["result"][
        "job_id"
    ]
    handled: set[str] = set()
    for _ in range(2):
        prompt = _wait_for_line(
            out,
            lambda line: (
                line.get("type") == "prompt_for_input"
                and line.get("payload", {}).get("kind") == "approval"
                and line.get("payload", {}).get("approval_id") not in handled
            ),
        )
        approval_id = prompt["payload"]["approval_id"]
        handled.add(approval_id)
        bridge.process_line(
            _request(
                "approval.respond",
                {
                    "session_id": plan_result["session_id"],
                    "approval_id": approval_id,
                    "allow": True,
                    "allow_for_session": False,
                },
                request_id=f"approve-{len(handled)}",
            )
            + "\n"
        )

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "review_gate_decision"
            and line.get("payload", {}).get("worker_id") == "T02"
            and line.get("payload", {}).get("decision") == "blocked"
        ),
    )
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and line.get("job_id") == job_id
            and str(line.get("payload", {}).get("message", "")).startswith(
                "forge_execute_completed "
            )
        ),
    )
    assert calls == ["T01"]

    bridge.process_line(_request("job.status", {"job_id": job_id}, request_id="job") + "\n")
    job_response = [line for line in _json_lines(out) if line.get("id") == "job"][0]
    assert job_response["ok"] is True
    # Data-outcome job status: the run finished; failure is in the exit code
    # and result payload, not the job status.
    assert job_response["result"]["status"] == "completed"
    assert job_response["result"]["exit_code"] == 1
    assert job_response["result"]["completed_at"]

    bridge.process_line(
        _request(
            "forge.status",
            {"session_id": plan_result["session_id"], "plan_id": plan_result["plan_id"]},
            request_id="status-after-execute",
        )
        + "\n"
    )
    status_response = [
        line for line in _json_lines(out) if line.get("id") == "status-after-execute"
    ][0]
    assert status_response["ok"] is True
    statuses = [task["status"] for task in status_response["result"]["tasks"]]
    assert statuses == ["verify_failed", "blocked"]


def test_stdio_bridge_forge_execute_verification_denial_emits_verify_gate_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=True),
    )
    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path),
        forge_execute_agent_runner=_fake_forge_execute_agent,
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan review execution denial.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]
    bridge.process_line(
        _request(
            "forge.execute",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "review",
                "workspace_trusted": True,
                "sandbox_profile": "strict",
            },
            request_id="execute",
        )
        + "\n"
    )

    write_prompt = _wait_for_line(
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
                "session_id": plan_result["session_id"],
                "approval_id": write_prompt["payload"]["approval_id"],
                "allow": True,
                "allow_for_session": False,
            },
            request_id="approve-write",
        )
        + "\n"
    )
    verify_prompt = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
            and line.get("payload", {}).get("approval_id") != write_prompt["payload"]["approval_id"]
        ),
    )
    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": plan_result["session_id"],
                "approval_id": verify_prompt["payload"]["approval_id"],
                "allow": False,
                "allow_for_session": False,
            },
            request_id="deny-verify",
        )
        + "\n"
    )

    verify_event = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "verify_gate_result"
            and line.get("payload", {}).get("success") is False
        ),
    )
    assert "approval denied" in verify_event["payload"]["summary"]


def test_stdio_bridge_forge_execute_shell_approval_uses_exact_command_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=True),
    )
    monkeypatch.setattr(forge_protocol, "run_task_verification", _fake_verify_success)
    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path),
        forge_execute_agent_runner=_fake_shell_approval_agent,
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan shell approval execution.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]
    bridge.process_line(
        _request(
            "forge.execute",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "review",
                "workspace_trusted": True,
                "sandbox_profile": "strict",
            },
            request_id="execute",
        )
        + "\n"
    )

    shell_prompt = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
            and line.get("payload", {}).get("command") == "pytest -q"
        ),
    )
    assert shell_prompt["payload"]["allow_for_session_supported"] is True
    assert shell_prompt["payload"]["allow_for_session_scope"]["type"] == "exact_command_hash"
    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": plan_result["session_id"],
                "approval_id": shell_prompt["payload"]["approval_id"],
                "allow": True,
                "allow_for_session": True,
            },
            request_id="approve-shell",
        )
        + "\n"
    )
    verify_prompt = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
            and line.get("payload", {}).get("approval_id") != shell_prompt["payload"]["approval_id"]
        ),
    )
    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": plan_result["session_id"],
                "approval_id": verify_prompt["payload"]["approval_id"],
                "allow": True,
                "allow_for_session": False,
            },
            request_id="approve-verify",
        )
        + "\n"
    )
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "review_gate_decision"
            and line.get("payload", {}).get("decision") == "accepted"
        ),
    )


def test_stdio_bridge_forge_execute_preview_reports_missing_prerequisites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_incomplete)
    monkeypatch.setattr(forge_protocol, "resolve_verify_commands", lambda **_: [])
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan an incomplete execution.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]

    bridge.process_line(
        _request(
            "forge.executePreview",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "mode": "review",
                "workspace_trusted": False,
                "sandbox_profile": "missing-profile",
            },
            request_id="preview",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "preview"][0]
    assert response["ok"] is True
    result = response["result"]
    assert result["preview_ready"] is False
    assert result["real_execution_supported"] is True
    missing = "\n".join(result["missing_prerequisites"])
    assert "Workspace Trust is required" in missing
    assert "acceptance criteria" in missing
    assert "verification commands" in missing
    assert "Unsupported sandbox profile" in missing
    assert any(line.get("type") == "warning_emitted" for line in _json_lines(out))


def test_stdio_bridge_forge_execute_preview_rejects_invalid_task_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=False, status="missing"),
    )
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan one task.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]

    bridge.process_line(
        _request(
            "forge.executePreview",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["missing-task"],
                "mode": "readonly",
            },
            request_id="preview",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "preview"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "forge_task_not_found"


def test_stdio_bridge_forge_execute_preview_readonly_does_not_require_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    monkeypatch.setattr(
        forge_protocol,
        "diagnose_sandbox",
        lambda *_, **__: _sandbox_diagnostic(ready=False, status="missing"),
    )
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan readonly preview.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]

    bridge.process_line(
        _request(
            "forge.executePreview",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "readonly",
                "workspace_trusted": False,
            },
            request_id="preview",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "preview"][0]
    assert response["ok"] is True
    result = response["result"]
    assert result["workspace_trust_required"] is False
    assert result["missing_prerequisites"] == []
    assert result["required_approvals"] == []
    assert result["preview_ready"] is True
    assert result["sandbox_profile"]["supported"] is True
    assert result["sandbox_profile"]["available"] is False


def test_stdio_bridge_forge_execute_policy_params_are_validated_for_preview_and_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Plan Forge Execute policy validation.",
    )
    base = {
        "session_id": plan_result["session_id"],
        "plan_id": plan_result["plan_id"],
        "task_ids": ["T01"],
        "mode": "readonly",
        "workspace_trusted": False,
    }

    bridge.process_line(
        _request(
            "forge.executePreview",
            {**base, "max_steps": 5, "no_log": True},
            request_id="preview-policy",
        )
        + "\n"
    )
    preview = _response_by_id(out, "preview-policy")
    assert preview["ok"] is True
    assert preview["result"]["max_steps"] == 5
    assert preview["result"]["no_log"] is True
    assert preview["result"]["subagents_supported"] is False
    assert preview["result"]["subagents_enabled"] is False
    assert preview["result"]["subagents_policy"] == "disabled_for_ide_forge_execute_v1"

    bridge.process_line(
        _request(
            "forge.executePreview",
            {**base, "max_steps": 0},
            request_id="preview-invalid-max-steps",
        )
        + "\n"
    )
    invalid_max_steps = _response_by_id(out, "preview-invalid-max-steps")
    assert invalid_max_steps["ok"] is False
    assert invalid_max_steps["error"]["code"] == "invalid_field"

    bridge.process_line(
        _request(
            "forge.executePreview",
            {**base, "no_log": "yes"},
            request_id="preview-invalid-no-log",
        )
        + "\n"
    )
    invalid_no_log = _response_by_id(out, "preview-invalid-no-log")
    assert invalid_no_log["ok"] is False
    assert invalid_no_log["error"]["code"] == "invalid_field"

    bridge.process_line(
        _request(
            "forge.executePreview",
            {**base, "subagents_enabled": False},
            request_id="preview-subagents",
        )
        + "\n"
    )
    unsupported_preview_subagents = _response_by_id(out, "preview-subagents")
    assert unsupported_preview_subagents["ok"] is False
    assert unsupported_preview_subagents["error"]["code"] == "forge_execute_subagents_unsupported"

    bridge.process_line(
        _request(
            "forge.execute",
            {**base, "mode": "review", "workspace_trusted": True, "subagents_enabled": True},
            request_id="execute-subagents",
        )
        + "\n"
    )
    unsupported_execute_subagents = _response_by_id(out, "execute-subagents")
    assert unsupported_execute_subagents["ok"] is False
    assert unsupported_execute_subagents["error"]["code"] == "forge_execute_subagents_unsupported"


def test_stdio_bridge_forge_execute_preview_loads_plan_without_legacy_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan readonly preview migration safety.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]

    original_load_plan = forge_protocol.load_plan
    migrate_flags: list[bool] = []

    def recording_load_plan(paths: Any, *, migrate_legacy: bool = True) -> dict[str, Any]:
        migrate_flags.append(migrate_legacy)
        if migrate_legacy:
            (paths.run_dir / "preview-mutated.txt").write_text("unexpected\n", encoding="utf-8")
        return original_load_plan(paths, migrate_legacy=False)

    monkeypatch.setattr(forge_protocol, "load_plan", recording_load_plan)
    bridge.process_line(
        _request(
            "forge.executePreview",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "readonly",
            },
            request_id="preview",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "preview"][0]
    assert response["ok"] is True
    assert migrate_flags == [False]
    run_dir = tmp_path / ".alysis" / "runs" / plan_result["plan_id"]
    assert not (run_dir / "preview-mutated.txt").exists()


def test_stdio_bridge_forge_execute_preview_does_not_default_to_blocked_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Plan blocked task preview safety.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]
    plan_path = tmp_path / ".alysis" / "runs" / plan_result["plan_id"] / "plan" / "plan.json"
    plan_doc = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_doc["tasks"][0]["status"] = "blocked"
    plan_path.write_text(json.dumps(plan_doc, indent=2) + "\n", encoding="utf-8")

    bridge.process_line(
        _request(
            "forge.executePreview",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "mode": "readonly",
            },
            request_id="default-preview",
        )
        + "\n"
    )
    default_response = [line for line in _json_lines(out) if line.get("id") == "default-preview"][0]
    assert default_response["ok"] is True
    assert default_response["result"]["selected_task_ids"] == []
    assert "No executable Forge tasks" in "\n".join(
        default_response["result"]["missing_prerequisites"]
    )

    bridge.process_line(
        _request(
            "forge.executePreview",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "task_ids": ["T01"],
                "mode": "readonly",
            },
            request_id="explicit-preview",
        )
        + "\n"
    )
    explicit_response = [line for line in _json_lines(out) if line.get("id") == "explicit-preview"][
        0
    ]
    assert explicit_response["ok"] is True
    assert explicit_response["result"]["selected_task_ids"] == ["T01"]
    missing = "\n".join(explicit_response["result"]["missing_prerequisites"])
    assert "task status 'blocked' is not executable" in missing
    assert explicit_response["result"]["required_approvals"] == []


def test_stdio_bridge_diff_list_empty_plan_reports_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Prepare a no-diff plan.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]

    bridge.process_line(
        _request(
            "diff.list",
            {"session_id": plan_result["session_id"], "plan_id": plan_result["plan_id"]},
            request_id="diffs",
        )
        + "\n"
    )
    response = [line for line in _json_lines(out) if line.get("id") == "diffs"][0]
    assert response["ok"] is True
    assert response["result"]["diffs"] == []
    assert "No Forge diff artifacts" in response["result"]["empty_reason"]


def test_stdio_bridge_forge_list_and_open_survive_session_close_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Prepare durable plan registry.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]
    asset_source = tmp_path / "durable-spec.md"
    asset_source.write_text("Durable Forge asset content.\n", encoding="utf-8")
    bridge.process_line(
        _request(
            "forge.assets.add",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "workspace_trusted": True,
                "source_path": "durable-spec.md",
                "title": "Durable spec",
            },
            request_id="asset-add",
        )
        + "\n"
    )
    asset_add_response = _response_by_id(out, "asset-add")
    assert asset_add_response["ok"] is True
    asset_id = asset_add_response["result"]["asset"]["record"]["id"]
    patch_dir = tmp_path / ".alysis" / "runs" / plan_result["plan_id"] / "execution" / "patches"
    patch_dir.mkdir(parents=True)
    (patch_dir / "T01.diff").write_text(
        "\n".join(
            [
                "diff --git a/demo.py b/demo.py",
                "--- a/demo.py",
                "+++ b/demo.py",
                "@@ -1 +1 @@",
                "-old",
                "+new",
                "",
            ]
        ),
        encoding="utf-8",
    )

    bridge.process_line(
        _request("session.cancel", {"session_id": plan_result["session_id"]}, request_id="close")
        + "\n"
    )
    bridge.process_line(
        _request(
            "forge.list",
            {"workspace": os.fspath(tmp_path)},
            request_id="list",
        )
        + "\n"
    )
    list_response = [line for line in _json_lines(out) if line.get("id") == "list"][0]
    assert list_response["ok"] is True
    assert [item["plan_id"] for item in list_response["result"]["plans"]] == [
        plan_result["plan_id"]
    ]

    restarted_out = io.StringIO()
    restarted = StdioBridge(stdout=restarted_out)
    restarted.process_line(
        _request(
            "forge.open",
            {"workspace": os.fspath(tmp_path), "plan_id": plan_result["plan_id"]},
            request_id="open",
        )
        + "\n"
    )

    open_response = [line for line in _json_lines(restarted_out) if line.get("id") == "open"][0]
    assert open_response["ok"] is True
    opened = open_response["result"]
    assert opened["plan_id"] == plan_result["plan_id"]
    assert opened["created_session"] is True
    assert opened["source"] == "loaded_persisted"
    assert opened["tasks"][0]["acceptance_criteria"]
    assert opened["tasks"][0]["verification_commands"] == ["pytest -q"]

    restarted.process_line(
        _request(
            "forge.status",
            {"session_id": opened["session_id"], "plan_id": opened["plan_id"]},
            request_id="status",
        )
        + "\n"
    )
    status_response = _response_by_id(restarted_out, "status")
    assert status_response["ok"] is True
    assert status_response["result"]["plan_id"] == opened["plan_id"]
    assert status_response["result"]["diff_count"] == 1

    restarted.process_line(
        _request(
            "forge.show",
            {"session_id": opened["session_id"], "plan_id": opened["plan_id"]},
            request_id="show",
        )
        + "\n"
    )
    show_response = _response_by_id(restarted_out, "show")
    assert show_response["ok"] is True
    assert show_response["result"]["assets"][0]["record"]["id"] == asset_id

    restarted.process_line(
        _request(
            "forge.executePreview",
            {
                "session_id": opened["session_id"],
                "plan_id": opened["plan_id"],
                "task_ids": ["T01"],
                "mode": "readonly",
                "workspace_trusted": False,
            },
            request_id="preview",
        )
        + "\n"
    )
    preview_response = _response_by_id(restarted_out, "preview")
    assert preview_response["ok"] is True
    assert preview_response["result"]["selected_task_ids"] == ["T01"]

    restarted.process_line(
        _request(
            "artifact.list",
            {"session_id": opened["session_id"]},
            request_id="artifacts",
        )
        + "\n"
    )
    artifact_list_response = _response_by_id(restarted_out, "artifacts")
    assert artifact_list_response["ok"] is True
    artifact_ids = {
        artifact["artifact_id"] for artifact in artifact_list_response["result"]["artifacts"]
    }
    assert opened["plan_artifact_id"] in artifact_ids

    restarted.process_line(
        _request(
            "artifact.read",
            {
                "session_id": opened["session_id"],
                "artifact_id": opened["plan_artifact_id"],
            },
            request_id="artifact",
        )
        + "\n"
    )
    artifact_response = [
        line for line in _json_lines(restarted_out) if line.get("id") == "artifact"
    ][0]
    assert artifact_response["ok"] is True
    assert "Wire IDE Forge Plan" in artifact_response["result"]["content"]

    restarted.process_line(
        _request(
            "forge.assets.list",
            {"session_id": opened["session_id"], "plan_id": opened["plan_id"]},
            request_id="assets",
        )
        + "\n"
    )
    assets_response = _response_by_id(restarted_out, "assets")
    assert assets_response["ok"] is True
    assert assets_response["result"]["assets"][0]["record"]["id"] == asset_id

    restarted.process_line(
        _request(
            "forge.assets.show",
            {
                "session_id": opened["session_id"],
                "plan_id": opened["plan_id"],
                "asset_id": asset_id,
            },
            request_id="asset-show",
        )
        + "\n"
    )
    asset_show_response = _response_by_id(restarted_out, "asset-show")
    assert asset_show_response["ok"] is True
    assert asset_show_response["result"]["asset"]["record"]["title"] == "Durable spec"

    restarted.process_line(
        _request(
            "diff.list",
            {"session_id": opened["session_id"], "plan_id": opened["plan_id"]},
            request_id="diffs",
        )
        + "\n"
    )
    diff_list_response = [line for line in _json_lines(restarted_out) if line.get("id") == "diffs"][
        0
    ]
    assert diff_list_response["ok"] is True
    diff_summary = diff_list_response["result"]["diffs"][0]
    assert diff_summary["file_path"] == "demo.py"

    restarted.process_line(
        _request(
            "diff.get",
            {
                "session_id": opened["session_id"],
                "plan_id": opened["plan_id"],
                "diff_id": diff_summary["diff_id"],
            },
            request_id="diff",
        )
        + "\n"
    )
    diff_response = [line for line in _json_lines(restarted_out) if line.get("id") == "diff"][0]
    assert diff_response["ok"] is True
    assert "demo.py" in diff_response["result"]["unified_diff"]


def test_stdio_bridge_forge_list_rejects_alysis_symlink_escape(tmp_path: Path) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    marker = "external registry marker"
    external = tmp_path.parent / f"{tmp_path.name}_external_registry"
    _write_external_forge_plan(external, "run_abc", marker)
    _symlink_or_skip(tmp_path / ".alysis", external)
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request("forge.list", {"workspace": os.fspath(tmp_path)}, request_id="list") + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "list"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "forge_registry_rejected"
    _assert_error_does_not_leak_external_path(response, external=external, marker=marker)


def test_stdio_bridge_forge_open_rejects_alysis_symlink_escape(tmp_path: Path) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    marker = "external open marker"
    external = tmp_path.parent / f"{tmp_path.name}_external_open_registry"
    _write_external_forge_plan(external, "run_abc", marker)
    _symlink_or_skip(tmp_path / ".alysis", external)
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "forge.open",
            {"workspace": os.fspath(tmp_path), "plan_id": "run_abc"},
            request_id="open",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "open"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "forge_registry_rejected"
    _assert_error_does_not_leak_external_path(response, external=external, marker=marker)


def test_stdio_bridge_forge_open_rejects_plan_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _out, _bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare plan symlink safety.",
    )
    marker = "external plan marker"
    external_plan = tmp_path.parent / f"{tmp_path.name}_external_plan"
    external_plan.mkdir()
    external_doc = {
        "schema_version": 2,
        "run_id": plan_result["plan_id"],
        "project_goal": marker,
        "summary": marker,
        "tasks": [],
        "assets": [],
        "requirements": [],
    }
    (external_plan / "plan.json").write_text(json.dumps(external_doc), encoding="utf-8")
    (external_plan / "PLAN.md").write_text(marker, encoding="utf-8")
    plan_dir = tmp_path / ".alysis" / "runs" / plan_result["plan_id"] / "plan"
    shutil.rmtree(plan_dir)
    _symlink_or_skip(plan_dir, external_plan)

    restarted_out = io.StringIO()
    restarted = StdioBridge(stdout=restarted_out)
    restarted.process_line(
        _request(
            "forge.open",
            {"workspace": os.fspath(tmp_path), "plan_id": plan_result["plan_id"]},
            request_id="open",
        )
        + "\n"
    )

    response = [line for line in _json_lines(restarted_out) if line.get("id") == "open"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "forge_registry_rejected"
    _assert_error_does_not_leak_external_path(response, external=external_plan, marker=marker)


def test_stdio_bridge_forge_open_rejects_execution_symlink_before_artifact_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _out, _bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare execution symlink safety.",
    )
    marker = "external execution marker"
    external_execution = tmp_path.parent / f"{tmp_path.name}_external_execution"
    (external_execution / "patches").mkdir(parents=True)
    (external_execution / "patches" / "T01.diff").write_text(marker, encoding="utf-8")
    execution_dir = tmp_path / ".alysis" / "runs" / plan_result["plan_id"] / "execution"
    if execution_dir.exists():
        shutil.rmtree(execution_dir)
    _symlink_or_skip(execution_dir, external_execution)

    restarted_out = io.StringIO()
    restarted = StdioBridge(stdout=restarted_out)
    restarted.process_line(
        _request(
            "forge.open",
            {"workspace": os.fspath(tmp_path), "plan_id": plan_result["plan_id"]},
            request_id="open",
        )
        + "\n"
    )

    response = [line for line in _json_lines(restarted_out) if line.get("id") == "open"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "forge_registry_rejected"
    _assert_error_does_not_leak_external_path(
        response,
        external=external_execution,
        marker=marker,
    )


def test_stdio_bridge_diff_list_rejects_patches_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare patches symlink safety.",
    )
    marker = "external patches marker"
    external_patches = tmp_path.parent / f"{tmp_path.name}_external_patches"
    external_patches.mkdir()
    (external_patches / "T01.diff").write_text(marker, encoding="utf-8")
    execution_dir = tmp_path / ".alysis" / "runs" / plan_result["plan_id"] / "execution"
    execution_dir.mkdir(exist_ok=True)
    patches_dir = execution_dir / "patches"
    if patches_dir.exists():
        shutil.rmtree(patches_dir)
    _symlink_or_skip(patches_dir, external_patches)

    bridge.process_line(
        _request(
            "diff.list",
            {"session_id": plan_result["session_id"], "plan_id": plan_result["plan_id"]},
            request_id="diffs",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "diffs"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "forge_registry_rejected"
    _assert_error_does_not_leak_external_path(response, external=external_patches, marker=marker)


def test_stdio_bridge_diff_get_rejects_symlinked_diff_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Prepare diff file symlink safety.",
    )
    patches_dir = tmp_path / ".alysis" / "runs" / plan_result["plan_id"] / "execution" / "patches"
    patches_dir.mkdir(parents=True)
    diff_file = patches_dir / "T01.diff"
    diff_file.write_text(
        "\n".join(
            [
                "diff --git a/demo.py b/demo.py",
                "--- a/demo.py",
                "+++ b/demo.py",
                "@@ -1 +1 @@",
                "-old",
                "+new",
                "",
            ]
        ),
        encoding="utf-8",
    )
    bridge.process_line(
        _request(
            "diff.list",
            {"session_id": plan_result["session_id"], "plan_id": plan_result["plan_id"]},
            request_id="diffs",
        )
        + "\n"
    )
    diff_response = [line for line in _json_lines(out) if line.get("id") == "diffs"][0]
    assert diff_response["ok"] is True
    diff_id = diff_response["result"]["diffs"][0]["diff_id"]

    marker = "external diff marker"
    external_diff = tmp_path.parent / f"{tmp_path.name}_external.diff"
    external_diff.write_text(marker, encoding="utf-8")
    diff_file.unlink()
    _symlink_or_skip(diff_file, external_diff, target_is_directory=False)
    bridge.process_line(
        _request(
            "diff.get",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "diff_id": diff_id,
            },
            request_id="diff",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "diff"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "diff_not_found"
    _assert_error_does_not_leak_external_path(response, external=external_diff, marker=marker)


def test_stdio_bridge_forge_open_rejects_arbitrary_plan_id(tmp_path: Path) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "forge.open",
            {"workspace": os.fspath(tmp_path), "plan_id": "../outside"},
            request_id="open",
        )
        + "\n"
    )

    response = [line for line in _json_lines(out) if line.get("id") == "open"][0]
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_plan_id"


def test_stdio_bridge_diff_protocol_uses_opaque_ids_limits_and_redacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    monkeypatch.setenv("ALYSIS_API_KEY", "diff-secret-value")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    outside = tmp_path.parent / "must-not-read.diff"
    outside.write_text("diff-secret-value", encoding="utf-8")
    (tmp_path / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )
    monkeypatch.setattr(forge_protocol, "run_planner_turn", _fake_planner_success)
    bridge = StdioBridge(
        stdout=out, create_session_fn=lambda **kwargs: _FakeBridgeSession(tmp_path)
    )
    bridge.process_line(
        _request(
            "forge.plan",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "instruction": "Review diff protocol.",
            },
            request_id="plan",
        )
        + "\n"
    )
    plan_result = [line for line in _json_lines(out) if line.get("id") == "plan"][0]["result"]
    patch_dir = tmp_path / ".alysis" / "runs" / plan_result["plan_id"] / "execution" / "patches"
    patch_dir.mkdir(parents=True)
    (patch_dir / "T01.diff").write_text(
        "\n".join(
            [
                "diff --git a/README.md b/README.md",
                "--- a/README.md",
                "+++ b/README.md",
                "@@ -1 +1 @@",
                "-old",
                "+new diff-secret-value",
                "",
            ]
        ),
        encoding="utf-8",
    )

    bridge.process_line(
        _request(
            "diff.list",
            {"session_id": plan_result["session_id"], "plan_id": plan_result["plan_id"]},
            request_id="diffs",
        )
        + "\n"
    )
    diff_list_response = [line for line in _json_lines(out) if line.get("id") == "diffs"][0]
    assert diff_list_response["ok"] is True
    diff_summary = diff_list_response["result"]["diffs"][0]
    assert diff_summary["diff_id"].startswith("diff_")
    assert diff_summary["file_path"] == "README.md"
    assert diff_summary["size_bytes"] > 0

    bridge.process_line(
        _request(
            "diff.list",
            {"session_id": plan_result["session_id"], "plan_id": "missing-plan"},
            request_id="diffs-missing-plan",
        )
        + "\n"
    )
    diff_list_missing_plan_response = [
        line for line in _json_lines(out) if line.get("id") == "diffs-missing-plan"
    ][0]
    assert diff_list_missing_plan_response["ok"] is False
    assert diff_list_missing_plan_response["error"]["code"] == "forge_plan_not_found"

    bridge.process_line(
        _request(
            "diff.list",
            {"session_id": "missing-session", "plan_id": plan_result["plan_id"]},
            request_id="diffs-missing-session",
        )
        + "\n"
    )
    diff_list_missing_session_response = [
        line for line in _json_lines(out) if line.get("id") == "diffs-missing-session"
    ][0]
    assert diff_list_missing_session_response["ok"] is False
    assert diff_list_missing_session_response["error"]["code"] == "session_not_found"

    bridge.process_line(
        _request(
            "diff.get",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "diff_id": diff_summary["diff_id"],
                "max_bytes": 32,
            },
            request_id="diff",
        )
        + "\n"
    )
    diff_response = [line for line in _json_lines(out) if line.get("id") == "diff"][0]
    assert diff_response["ok"] is True
    assert diff_response["result"]["truncated"] is True
    assert "diff-secret-value" not in json.dumps(diff_response)

    bridge.process_line(
        _request("diff.get", {"diff_id": os.fspath(outside)}, request_id="outside") + "\n"
    )
    outside_response = [line for line in _json_lines(out) if line.get("id") == "outside"][0]
    assert outside_response["ok"] is False
    assert outside_response["error"]["code"] == "missing_field"
    assert "diff-secret-value" not in json.dumps(outside_response)

    bridge.process_line(
        _request(
            "diff.get",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "diff_id": os.fspath(outside),
            },
            request_id="outside-scoped",
        )
        + "\n"
    )
    outside_scoped_response = [
        line for line in _json_lines(out) if line.get("id") == "outside-scoped"
    ][0]
    assert outside_scoped_response["ok"] is False
    assert outside_scoped_response["error"]["code"] == "diff_not_found"
    assert "diff-secret-value" not in json.dumps(outside_scoped_response)

    bridge.process_line(
        _request(
            "diff.get",
            {
                "session_id": plan_result["session_id"],
                "plan_id": "missing-plan",
                "diff_id": diff_summary["diff_id"],
            },
            request_id="missing-plan",
        )
        + "\n"
    )
    missing_plan_response = [line for line in _json_lines(out) if line.get("id") == "missing-plan"][
        0
    ]
    assert missing_plan_response["ok"] is False
    assert missing_plan_response["error"]["code"] == "forge_plan_not_found"

    bridge.process_line(
        _request(
            "diff.get",
            {
                "session_id": "missing-session",
                "plan_id": plan_result["plan_id"],
                "diff_id": diff_summary["diff_id"],
            },
            request_id="missing-session",
        )
        + "\n"
    )
    missing_session_response = [
        line for line in _json_lines(out) if line.get("id") == "missing-session"
    ][0]
    assert missing_session_response["ok"] is False
    assert missing_session_response["error"]["code"] == "session_not_found"
    assert "diff-secret-value" not in json.dumps(missing_session_response)


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
    assert "forge.plan" in payload["capabilities"]["methods"]
    assert "forge.plan.start" in payload["capabilities"]["methods"]
    assert "forge.plan.result" in payload["capabilities"]["methods"]
    assert "forge.list" in payload["capabilities"]["methods"]
    assert "forge.open" in payload["capabilities"]["methods"]
    assert "forge.resume" in payload["capabilities"]["methods"]
    assert "forge.status" in payload["capabilities"]["methods"]
    assert "forge.show" in payload["capabilities"]["methods"]
    assert "forge.review" in payload["capabilities"]["methods"]
    assert "forge.attach" in payload["capabilities"]["methods"]
    assert "forge.assets.add" in payload["capabilities"]["methods"]
    assert "forge.assets.pruneLegacy" in payload["capabilities"]["methods"]
    assert "forge.executePreview" in payload["capabilities"]["methods"]
    assert "forge.execute" in payload["capabilities"]["methods"]
    assert "forge.swarm" not in payload["capabilities"]["methods"]
    assert "diff.list" in payload["capabilities"]["methods"]
    assert "diff.get" in payload["capabilities"]["methods"]
    assert payload["capabilities"]["features"]["forge"]["plan"]["supported"] is True
    assert payload["capabilities"]["features"]["forge"]["plan"]["async"] is True
    assert (
        payload["capabilities"]["features"]["forge"]["plan"]["start_method"] == "forge.plan.start"
    )
    assert payload["capabilities"]["features"]["forge"]["plan"]["shallow_fallback"] is False
    assert payload["capabilities"]["features"]["forge"]["list"]["durable"] is True
    assert payload["capabilities"]["features"]["forge"]["open"]["durable"] is True
    assert payload["capabilities"]["features"]["forge"]["plan"]["terminal_output_scraping"] is False
    assert payload["capabilities"]["features"]["forge"]["execute_preview"]["supported"] is True
    assert payload["capabilities"]["features"]["forge"]["execute_preview"]["mutates"] is False
    assert payload["capabilities"]["features"]["forge"]["execute"]["supported"] is True
    assert payload["capabilities"]["features"]["forge"]["execute"]["supported_modes"] == ["review"]
    assert payload["capabilities"]["features"]["forge"]["execute"]["dry_run_supported"] is True
    assert (
        payload["capabilities"]["features"]["forge"]["execute"]["unsafe_modes"]["supported"]
        is False
    )
    assert payload["capabilities"]["features"]["forge"]["execute"]["unsafe_modes"][
        "experimental_flags"
    ] == {"auto": False, "fullaccess": False, "forge.swarm": False}
    assert payload["capabilities"]["features"]["forge"]["cancel"]["supported"] is True
    assert payload["capabilities"]["features"]["forge"]["cancel"]["callable"] is True
    assert (
        payload["capabilities"]["features"]["forge"]["cancel"]["behavior"]
        == "cooperative_checkpoint_cancellation"
    )
    assert (
        payload["capabilities"]["features"]["forge"]["cancel"][
            "must_not_mark_cancelled_without_interrupt"
        ]
        is False
    )
    assert payload["capabilities"]["features"]["forge"]["swarm"]["supported"] is True
    assert (
        payload["capabilities"]["features"]["forge"]["swarm"]["cancellation"]
        == "cooperative_checkpoint_cancellation"
    )
    assert payload["capabilities"]["features"]["forge"]["swarm"]["workspace_trust_required"] is True
    assert (
        payload["capabilities"]["features"]["forge"]["assets"]["methods"]["forge.assets.add"][
            "trust_required"
        ]
        is True
    )
    assert payload["capabilities"]["features"]["diffs"]["opaque_ids"] is True


class _FakeBridgeSession:
    def __init__(self, tmp_path: Path) -> None:
        self.store = type(
            "FakeStore", (), {"session_artifact_root": tmp_path / "session-artifacts"}
        )()

    def close(self) -> None:
        pass

    def run_turn(self, message: str) -> int:
        _ = message
        return 0


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


def _swarm_trace_event(
    *, phase: str, message: str, task_id: str | None = None, verbosity: str = "compact"
):
    from alysis_code.swarm_trace import build_swarm_trace_event

    return build_swarm_trace_event(
        run_id="run", phase=phase, message=message, task_id=task_id, verbosity=verbosity
    )


def test_safe_ide_task_statuses_never_allow_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Invariant: "interrupted" is engine-owned. The IDE must never be able to
    # set it through plan edits, even though it renders via generic status.
    assert "interrupted" not in forge_protocol.SAFE_IDE_TASK_STATUSES

    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path, monkeypatch, instruction="Guard the interrupted status invariant."
    )
    bridge.process_line(
        _request(
            "forge.plan.updateTask",
            {
                "session_id": plan_result["session_id"],
                "plan_id": plan_result["plan_id"],
                "workspace_trusted": True,
                "task_id": "T01",
                "status": "interrupted",
            },
            request_id="set-interrupted",
        )
        + "\n"
    )
    response = _response_by_id(out, "set-interrupted")
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_task_status"


def test_stdio_bridge_forge_swarm_lifecycle_events_replay_and_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    runner_started = threading.Event()

    def stub_swarm_runner(**kwargs: Any) -> int:
        captured.update(kwargs)
        sink = kwargs["trace_sink"]
        sink.emit(
            _swarm_trace_event(
                phase="worktree.lifecycle",
                message="Preparing worktree on branch feat/t01-a.",
                task_id="T01",
                verbosity="full",
            )
        )
        sink.emit(
            _swarm_trace_event(
                phase="worker.lifecycle",
                message="Worker started (attempt 1).",
                task_id="T01",
            )
        )
        runner_started.set()
        event = kwargs["cancellation_event"]
        assert event.wait(timeout=10), "forge.swarm.cancel never reached the engine"
        sink.emit(
            _swarm_trace_event(
                phase="worker.lifecycle",
                message="Worker interrupted at cooperative checkpoint; worktree preserved.",
                task_id="T01",
            )
        )
        sink.close()
        return 130

    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Run a swarm through the bridge.",
        forge_swarm_runner=stub_swarm_runner,
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]

    untrusted = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.start",
        {"session_id": session_id, "plan_id": plan_id},
        request_id="swarm-untrusted",
    )
    assert untrusted["ok"] is False
    assert untrusted["error"]["code"] == "workspace_trust_required"

    bad_grants = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.start",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "approval_scope_grants": [{"kind": "shell_run"}],
        },
        request_id="swarm-grants",
    )
    assert bad_grants["ok"] is False
    assert bad_grants["error"]["code"] == "invalid_approval_scope_grant"

    start = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.start",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "parallel": 3,
            "approval_scope_grants": [],
        },
        request_id="swarm-start",
    )
    assert start["ok"] is True, start
    assert start["result"]["status"] == "started"
    assert start["result"]["parallel"] == 3
    job_id = start["result"]["job_id"]
    assert runner_started.wait(timeout=5)

    assert captured["parallel"] == 3
    assert captured["mode"] == "auto"
    assert captured["dry_run"] is False
    assert captured["keep_worktrees"] is True
    assert captured["review"] is False
    assert captured["merge_strategy"] == "review"
    assert callable(captured["worker_approval_handler"])
    assert captured["cancellation_event"] is not None

    bridge.process_line(
        _request(
            "forge.swarm.start",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
            },
            request_id="swarm-busy",
        )
        + "\n"
    )
    busy = _response_by_id(out, "swarm-busy")
    assert busy["ok"] is False
    assert busy["error"]["code"] == "session_busy"

    bridge.process_line(
        _request(
            "forge.swarm.status",
            {"session_id": session_id, "job_id": job_id},
            request_id="swarm-status",
        )
        + "\n"
    )
    status = _response_by_id(out, "swarm-status")
    assert status["ok"] is True
    assert status["result"]["kind"] == "forge_swarm"
    assert status["result"]["status"] == "running"
    assert status["result"]["task_status_counts"]

    bridge.process_line(
        _request(
            "forge.swarm.result",
            {"job_id": job_id},
            request_id="swarm-progress",
        )
        + "\n"
    )
    progress = _response_by_id(out, "swarm-progress")
    assert progress["ok"] is True
    assert progress["result"]["complete"] is False
    assert progress["result"]["cancellable"] is True

    # Simulated reconnect mid-run: replay must include the swarm task events.
    bridge.process_line(
        _request(
            "session.getEvents",
            {"session_id": session_id, "max_events": 200},
            request_id="swarm-replay",
        )
        + "\n"
    )
    replay = _response_by_id(out, "swarm-replay")
    assert replay["ok"] is True
    swarm_events = [
        event
        for event in replay["result"]["events"]
        if event["type"] == "swarm_worker_state_changed"
    ]
    states = [event["payload"]["state"] for event in swarm_events]
    assert "scheduled" in states
    assert "started" in states
    assert any(
        event.get("type") == "info_emitted"
        and "swarm_started" in str(event.get("payload", {}).get("message", ""))
        for event in replay["result"]["events"]
    )

    bridge.process_line(
        _request(
            "forge.swarm.cancel",
            {"session_id": session_id, "job_id": job_id},
            request_id="swarm-cancel",
        )
        + "\n"
    )
    cancel = _response_by_id(out, "swarm-cancel")
    assert cancel["ok"] is True
    assert cancel["result"]["status"] == "cancellation_requested"

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "warning_emitted"
            and "swarm_cancelled" in str(line.get("payload", {}).get("message", ""))
        ),
        timeout=5.0,
    )

    bridge.process_line(
        _request("forge.swarm.result", {"job_id": job_id}, request_id="swarm-final") + "\n"
    )
    final = _response_by_id(out, "swarm-final")
    assert final["ok"] is True
    assert final["result"]["status"] == "cancelled"
    assert final["result"]["cancelled"] is True
    assert final["result"]["result"]["exit_code"] == 130

    bridge.process_line(
        _request(
            "session.getEvents",
            {"session_id": session_id, "max_events": 200},
            request_id="swarm-replay-final",
        )
        + "\n"
    )
    replay_final = _response_by_id(out, "swarm-replay-final")
    interrupted_states = [
        event["payload"]["state"]
        for event in replay_final["result"]["events"]
        if event["type"] == "swarm_worker_state_changed"
    ]
    assert "interrupted" in interrupted_states


def test_stdio_bridge_forge_swarm_completed_result_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stub_swarm_runner(**kwargs: Any) -> int:
        paths = kwargs["paths"]
        summary = {
            "schema_version": 1,
            "status": "clean",
            "clean": True,
            "exit_code": 0,
            "verification_status": "passed",
            "reason_codes": [],
            "interrupted": False,
            "interrupted_task_ids": [],
        }
        summary_path = paths.execution_dir / "swarm_summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        (paths.execution_dir / "swarm_summary.md").write_text("# Swarm Summary\n", encoding="utf-8")
        kwargs["trace_sink"].emit(
            _swarm_trace_event(
                phase="merge.lifecycle",
                message="`T01` merged (feat/t01-a), commit abc123",
                task_id="T01",
            )
        )
        return 0

    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Run a swarm to completion.",
        forge_swarm_runner=stub_swarm_runner,
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]

    start = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.start",
        {"session_id": session_id, "plan_id": plan_id, "workspace_trusted": True},
        request_id="swarm-run",
    )
    assert start["ok"] is True, start
    job_id = start["result"]["job_id"]

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and "swarm_completed" in str(line.get("payload", {}).get("message", ""))
        ),
        timeout=5.0,
    )

    bridge.process_line(
        _request("forge.swarm.result", {"job_id": job_id}, request_id="swarm-done") + "\n"
    )
    done = _response_by_id(out, "swarm-done")
    assert done["ok"] is True
    assert done["result"]["complete"] is True
    assert done["result"]["status"] == "completed"
    assert done["result"]["run_status"] == "clean"
    assert done["result"]["clean"] is True
    assert done["result"]["exit_code"] == 0
    assert done["result"]["task_status_counts"]
    assert done["result"]["redacted"] is True

    merged_event = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "swarm_worker_state_changed"
            and line.get("payload", {}).get("state") == "merged"
        ),
        timeout=5.0,
    )
    assert merged_event["payload"]["worker_id"] == "T01"


def test_stdio_bridge_forge_swarm_terminal_failure_does_not_rerecord_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stub_swarm_runner(**kwargs: Any) -> int:
        sessions_dir = kwargs["paths"].execution_sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)
        usage_payload = {
            "event_type": "llm_usage",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "role": "worker",
            "requested_model": "test-model",
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
            "usage_source": "api",
        }
        (sessions_dir / "attempt.jsonl").write_text(
            json.dumps({"type": "llm_usage", "payload": usage_payload}) + "\n",
            encoding="utf-8",
        )
        return 0

    # Result validation happens after usage is durably committed. Force that
    # stage to fail so the runner enters its secondary durable failure path.
    monkeypatch.setattr(
        stdio_bridge,
        "forge_swarm_result_payload",
        lambda *_args, **_kwargs: {"oversized": "x" * 20_000},
    )
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Exercise terminal usage idempotency.",
        forge_swarm_runner=stub_swarm_runner,
    )
    session_id = plan_result["session_id"]
    start = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.start",
        {
            "session_id": session_id,
            "plan_id": plan_result["plan_id"],
            "workspace_trusted": True,
        },
        request_id="swarm-terminal-failure",
    )
    assert start["ok"] is True, start
    job_id = start["result"]["job_id"]
    thread = bridge._jobs[job_id].thread
    assert thread is not None
    thread.join(timeout=5.0)
    assert thread.is_alive() is False

    listed = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.list",
        {"session_id": session_id},
        request_id="swarm-terminal-failure-list",
    )
    durable = next(item for item in listed["result"]["jobs"] if item["job_id"] == job_id)
    assert durable["state"] == "failed"
    assert durable["usage"]["total_tokens"] == 10


def test_stdio_bridge_forge_swarm_usage_is_isolated_between_jobs_for_one_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dirs: list[Path] = []
    usage_totals = iter((10, 25))

    def stub_swarm_runner(**kwargs: Any) -> int:
        total_tokens = next(usage_totals)
        paths = kwargs["paths"]
        sessions_dir = paths.execution_sessions_dir
        session_dirs.append(sessions_dir)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        usage_payload = {
            "event_type": "llm_usage",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "role": "worker",
            "requested_model": "test-model",
            "prompt_tokens": total_tokens - 3,
            "completion_tokens": 3,
            "total_tokens": total_tokens,
            "usage_source": "api",
        }
        (sessions_dir / "attempt.jsonl").write_text(
            json.dumps({"type": "llm_usage", "payload": usage_payload}) + "\n",
            encoding="utf-8",
        )
        summary = {
            "schema_version": 1,
            "status": "clean",
            "clean": True,
            "exit_code": 0,
            "verification_status": "passed",
            "reason_codes": [],
            "interrupted": False,
            "interrupted_task_ids": [],
        }
        paths.execution_dir.mkdir(parents=True, exist_ok=True)
        (paths.execution_dir / "swarm_summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
        (paths.execution_dir / "swarm_summary.md").write_text("# Swarm Summary\n", encoding="utf-8")
        return 0

    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Run two independently accounted swarms.",
        forge_swarm_runner=stub_swarm_runner,
    )
    session_id = plan_result["session_id"]
    job_ids: list[str] = []
    for index in range(2):
        started = _send_bridge_request(
            bridge,
            out,
            "forge.swarm.start",
            {
                "session_id": session_id,
                "plan_id": plan_result["plan_id"],
                "workspace_trusted": True,
            },
            request_id=f"swarm-isolated-{index}",
        )
        assert started["ok"] is True, started
        job_id = started["result"]["job_id"]
        job_ids.append(job_id)
        thread = bridge._jobs[job_id].thread
        assert thread is not None
        thread.join(timeout=5.0)
        assert thread.is_alive() is False

    listed = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.list",
        {"session_id": session_id},
        request_id="swarm-isolated-list",
    )
    usage_by_job = {
        item["job_id"]: item["usage"]["total_tokens"] for item in listed["result"]["jobs"]
    }
    assert usage_by_job[job_ids[0]] == 10
    assert usage_by_job[job_ids[1]] == 25
    assert len(session_dirs) == 2
    assert session_dirs[0] != session_dirs[1]
    assert all(path.parent.name == "ide_swarm_jobs" for path in session_dirs)


def test_stdio_bridge_forge_swarm_durable_list_resume_and_restart_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    session_dirs: list[Path] = []

    def stub_swarm_runner(**kwargs: Any) -> int:
        nonlocal attempts
        attempts += 1
        paths = kwargs["paths"]
        sessions_dir = paths.execution_sessions_dir
        session_dirs.append(sessions_dir)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        total_tokens = 10 if attempts == 1 else 25
        usage_payload = {
            "event_type": "llm_usage",
            "timestamp": f"2026-01-01T00:00:0{attempts}+00:00",
            "role": "worker",
            "requested_model": "test-model",
            "prompt_tokens": total_tokens - 3,
            "completion_tokens": 3,
            "total_tokens": total_tokens,
            "usage_source": "api",
        }
        (sessions_dir / f"attempt-{attempts}.jsonl").write_text(
            json.dumps({"type": "llm_usage", "payload": usage_payload}) + "\n",
            encoding="utf-8",
        )
        if attempts == 1:
            raise RuntimeError("simulated worker crash")
        summary = {
            "schema_version": 1,
            "status": "clean",
            "clean": True,
            "exit_code": 0,
            "verification_status": "passed",
            "reason_codes": [],
            "interrupted": False,
            "interrupted_task_ids": [],
        }
        paths.execution_dir.mkdir(parents=True, exist_ok=True)
        (paths.execution_dir / "swarm_summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
        (paths.execution_dir / "swarm_summary.md").write_text("# Swarm Summary\n", encoding="utf-8")
        return 0

    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Recover a durable swarm after a worker crash.",
        forge_swarm_runner=stub_swarm_runner,
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]
    start = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.start",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "idempotency_key": "durable-resume-test",
        },
        request_id="durable-start",
    )
    assert start["ok"] is True, start
    job_id = start["result"]["job_id"]
    first_thread = bridge._jobs[job_id].thread
    assert first_thread is not None
    first_thread.join(timeout=5.0)
    assert first_thread.is_alive() is False

    listed = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.list",
        {"session_id": session_id},
        request_id="durable-list",
    )
    durable_job = next(job for job in listed["result"]["jobs"] if job["job_id"] == job_id)
    assert durable_job["state"] == "failed"
    assert durable_job["resumable"] is True

    bridge.close()
    restarted_out = io.StringIO()
    restarted = StdioBridge(
        stdout=restarted_out,
        create_session_fn=lambda **_kwargs: _FakeBridgeSession(tmp_path),
        forge_swarm_runner=stub_swarm_runner,
    )
    created_session = _send_bridge_request(
        restarted,
        restarted_out,
        "session.create",
        {
            "workspace": os.fspath(tmp_path),
            "mode": "readonly",
            "model": "test-model",
            "session_id": session_id,
        },
        request_id="durable-restart-session",
    )
    assert created_session["ok"] is True, created_session
    restarted.process_line(
        _request(
            "forge.open",
            {"session_id": session_id, "plan_id": plan_id},
            request_id="durable-restart-open",
        )
        + "\n"
    )
    opened = _response_by_id(restarted_out, "durable-restart-open")
    assert opened["ok"] is True, opened
    zero_session_grants = _send_bridge_request(
        restarted,
        restarted_out,
        "permission.session.list",
        {"session_id": session_id},
        request_id="durable-restart-zero-grants",
    )
    assert zero_session_grants["result"]["grants"] == []
    restarted_list = _send_bridge_request(
        restarted,
        restarted_out,
        "forge.swarm.list",
        {"session_id": session_id},
        request_id="durable-restart-list",
    )
    durable_job = next(job for job in restarted_list["result"]["jobs"] if job["job_id"] == job_id)
    recovery_grant = {
        "kind": "forge_swarm_resume",
        "scope": {
            "type": "forge_swarm_resume_v1",
            "session_id": session_id,
            "plan_id": plan_id,
            "job_id": job_id,
            "revision": durable_job["revision"],
        },
    }

    no_fresh_grant = _send_bridge_request(
        restarted,
        restarted_out,
        "forge.swarm.resume",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "job_id": job_id,
            "workspace_trusted": True,
            "expected_revision": durable_job["revision"],
            "approval_scope_grants": [],
        },
        request_id="durable-no-fresh-grant",
    )
    assert no_fresh_grant["ok"] is False
    assert no_fresh_grant["error"]["code"] == "swarm_fresh_permission_grant_required"

    wrong_job_scope = _send_bridge_request(
        restarted,
        restarted_out,
        "forge.swarm.resume",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "job_id": job_id,
            "workspace_trusted": True,
            "expected_revision": durable_job["revision"],
            "approval_scope_grants": [
                {
                    "kind": "forge_swarm_resume",
                    "scope": {
                        "type": "forge_swarm_resume_v1",
                        "session_id": session_id,
                        "plan_id": "different-plan",
                        "job_id": job_id,
                        "revision": durable_job["revision"],
                    },
                }
            ],
        },
        request_id="durable-wrong-recovery-scope",
    )
    assert wrong_job_scope["ok"] is False
    assert wrong_job_scope["error"]["code"] == "invalid_approval_scope_grant"

    changed_scope = _send_bridge_request(
        restarted,
        restarted_out,
        "forge.swarm.resume",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "job_id": job_id,
            "workspace_trusted": True,
            "expected_revision": durable_job["revision"],
            "approval_scope_grants": [
                recovery_grant,
                {
                    "kind": "shell_run",
                    "scope": exact_command_scope("python -m pytest"),
                },
            ],
        },
        request_id="durable-scope-change",
    )
    assert changed_scope["ok"] is False
    assert changed_scope["error"]["code"] == "swarm_permission_scope_changed"

    resumed = _send_bridge_request(
        restarted,
        restarted_out,
        "forge.swarm.resume",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "job_id": job_id,
            "workspace_trusted": True,
            "expected_revision": durable_job["revision"],
            "approval_scope_grants": [recovery_grant],
        },
        request_id="durable-resume",
    )
    assert resumed["ok"] is True
    assert resumed["result"]["status"] == "resumed"
    assert resumed["result"]["state"] == "queued"
    _wait_for_line(
        restarted_out,
        lambda line: (
            line.get("type") == "info_emitted"
            and "swarm_completed" in str(line.get("payload", {}).get("message", ""))
        ),
        timeout=5.0,
    )
    grants_after_resume = _send_bridge_request(
        restarted,
        restarted_out,
        "permission.session.list",
        {"session_id": session_id},
        request_id="durable-resume-action-grants",
    )
    assert grants_after_resume["result"]["grants"] == []

    restarted.close()
    result_out = io.StringIO()
    result_bridge = StdioBridge(
        stdout=result_out,
        create_session_fn=lambda **_kwargs: _FakeBridgeSession(tmp_path),
    )
    assert (
        _send_bridge_request(
            result_bridge,
            result_out,
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "readonly",
                "model": "test-model",
                "session_id": session_id,
            },
            request_id="durable-result-session",
        )["ok"]
        is True
    )
    result_bridge.process_line(
        _request(
            "forge.open",
            {"session_id": session_id, "plan_id": plan_id},
            request_id="durable-result-open",
        )
        + "\n"
    )
    assert _response_by_id(result_out, "durable-result-open")["ok"] is True
    recovered = _send_bridge_request(
        result_bridge,
        result_out,
        "forge.swarm.result",
        {"session_id": session_id, "job_id": job_id},
        request_id="durable-restart-result",
    )
    assert recovered["ok"] is True
    assert recovered["result"]["status"] == "completed"
    assert recovered["result"]["state"] == "succeeded"
    assert recovered["result"]["clean"] is True
    assert recovered["result"]["resume_count"] == 1
    assert recovered["result"]["usage"]["total_tokens"] == 35
    assert session_dirs[0] == session_dirs[1]


def test_stdio_bridge_forge_review_start_result_and_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path, monkeypatch, instruction="Review a task asynchronously."
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]

    review_payload = {
        "session_id": session_id,
        "plan_id": plan_id,
        "task_id": "T01",
        "approved": True,
        "confidence": "high",
        "summary": "looks good",
    }
    monkeypatch.setattr(stdio_bridge, "forge_review_result", lambda *_a, **_k: dict(review_payload))

    start = _send_bridge_request(
        bridge,
        out,
        "forge.review.start",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "task_id": "T01",
            "workspace_trusted": True,
        },
        request_id="review-start",
    )
    assert start["ok"] is True
    assert start["result"]["status"] == "started"
    assert start["result"]["task_id"] == "T01"
    job_id = start["result"]["job_id"]

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and "forge_review_completed" in str(line.get("payload", {}).get("message", ""))
        ),
        timeout=5.0,
    )
    bridge.process_line(
        _request("forge.review.result", {"job_id": job_id}, request_id="review-result") + "\n"
    )
    result = _response_by_id(out, "review-result")
    assert result["ok"] is True
    assert result["result"]["approved"] is True
    assert result["result"]["task_id"] == "T01"

    # Cancellation: a cancel that lands mid-provider-call takes effect at the
    # post-call checkpoint and the result is discarded.
    review_started = threading.Event()
    release_review = threading.Event()

    def blocking_review(*_a: Any, **_k: Any) -> dict[str, Any]:
        review_started.set()
        assert release_review.wait(timeout=10)
        return dict(review_payload)

    monkeypatch.setattr(stdio_bridge, "forge_review_result", blocking_review)
    start2 = _send_bridge_request(
        bridge,
        out,
        "forge.review.start",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "task_id": "T01",
            "workspace_trusted": True,
        },
        request_id="review-start-2",
    )
    assert start2["ok"] is True
    job2 = start2["result"]["job_id"]
    assert review_started.wait(timeout=5)

    bridge.process_line(
        _request(
            "forge.cancel",
            {"session_id": session_id, "plan_id": plan_id},
            request_id="review-cancel",
        )
        + "\n"
    )
    cancel = _response_by_id(out, "review-cancel")
    assert cancel["ok"] is True
    assert cancel["result"]["status"] == "cancellation_requested"
    release_review.set()

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "warning_emitted"
            and "forge_review_cancelled" in str(line.get("payload", {}).get("message", ""))
        ),
        timeout=5.0,
    )
    bridge.process_line(
        _request("forge.review.result", {"job_id": job2}, request_id="review-result-2") + "\n"
    )
    cancelled = _response_by_id(out, "review-result-2")
    assert cancelled["ok"] is True
    assert cancelled["result"]["status"] == "cancelled"
    assert cancelled["result"]["cancelled"] is True


def test_stdio_bridge_forge_swarm_reconcile_dead_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path, monkeypatch, instruction="Reconcile a dead swarm run."
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]
    run_dir = tmp_path / ".alysis" / "runs" / plan_id
    plan_path = run_dir / "plan" / "plan.json"
    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    base_task = dict(plan_data["tasks"][0])
    plan_data["tasks"][0]["status"] = "done"

    def _clone_task(task_id: str, status: str) -> dict[str, Any]:
        clone = json.loads(json.dumps(base_task))
        clone["id"] = task_id
        clone["status"] = status
        clone["branch"] = f"feat/{task_id.lower()}"
        return clone

    plan_data["tasks"].append(_clone_task("T02", "interrupted"))
    plan_data["tasks"].append(_clone_task("T03", "planned"))
    plan_path.write_text(json.dumps(plan_data), encoding="utf-8")

    merge_dir = run_dir / "execution" / "merge_results"
    merge_dir.mkdir(parents=True, exist_ok=True)
    (merge_dir / "T01.json").write_text(
        json.dumps({"task_id": "T01", "success": True, "merge_commit_hash": "abc123"}),
        encoding="utf-8",
    )

    worktree = run_dir / "worktrees" / "T02" / "repo"
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=worktree, check=True)
    (worktree / "demo.py").write_text("print('base')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=worktree,
        check=True,
    )
    (worktree / "demo.py").write_text("print('harvest me')\n", encoding="utf-8")
    (worktree / "untracked_note.txt").write_text("note\n", encoding="utf-8")

    def reconcile(request_id: str, **extra: Any) -> dict[str, Any]:
        bridge.process_line(
            _request(
                "forge.swarm.reconcile",
                {"session_id": session_id, "plan_id": plan_id, **extra},
                request_id=request_id,
            )
            + "\n"
        )
        return _response_by_id(out, request_id)

    report = reconcile("reconcile-report")
    assert report["ok"] is True
    states = {item["task_id"]: item["state"] for item in report["result"]["tasks"]}
    assert states == {"T01": "merged", "T02": "interrupted", "T03": "unstarted"}
    by_id = {item["task_id"]: item for item in report["result"]["tasks"]}
    assert by_id["T01"]["merge_commit_hash"] == "abc123"
    assert by_id["T02"]["worktree_present"] is True
    assert by_id["T02"]["diff_available"] is True
    assert by_id["T03"]["worktree_present"] is False
    assert report["result"]["read_only"] is True

    report_again = reconcile("reconcile-report-2")
    assert report_again["result"]["tasks"] == report["result"]["tasks"]

    harvest_untrusted = reconcile("reconcile-harvest-untrusted", action="harvest")
    assert harvest_untrusted["ok"] is False
    assert harvest_untrusted["error"]["code"] == "workspace_trust_required"

    harvest = reconcile(
        "reconcile-harvest",
        action="harvest",
        workspace_trusted=True,
        base_branch="main",
    )
    assert harvest["ok"] is True
    harvested = {item["task_id"]: item for item in harvest["result"]["actions"]}
    assert harvested["T02"]["harvested"] is True
    diff_artifact = tmp_path / harvested["T02"]["diff_artifact"]
    diff_text = diff_artifact.read_text(encoding="utf-8")
    assert "harvest me" in diff_text
    meta = json.loads((diff_artifact.parent / "T02.json").read_text(encoding="utf-8"))
    assert meta["untracked_files"] == ["untracked_note.txt"]
    refreshed = {item["task_id"]: item for item in harvest["result"]["tasks"]}
    assert refreshed["T02"]["harvest_artifact_present"] is True

    harvest_again = reconcile(
        "reconcile-harvest-2",
        action="harvest",
        workspace_trusted=True,
        base_branch="main",
    )
    assert harvest_again["ok"] is True
    assert {item["task_id"]: item for item in harvest_again["result"]["actions"]}["T02"][
        "harvested"
    ] is True

    discard_unconfirmed = reconcile(
        "reconcile-discard-unconfirmed", action="discard", workspace_trusted=True
    )
    assert discard_unconfirmed["ok"] is False
    assert discard_unconfirmed["error"]["code"] == "confirmation_required"

    discard = reconcile("reconcile-discard", action="discard", workspace_trusted=True, yes=True)
    assert discard["ok"] is True
    discarded = {item["task_id"]: item for item in discard["result"]["actions"]}
    assert discarded["T02"]["discarded"] is True
    assert not worktree.exists()
    refreshed_after_discard = {item["task_id"]: item for item in discard["result"]["tasks"]}
    assert refreshed_after_discard["T02"]["worktree_present"] is False
    # The harvested diff survives the discard for later review.
    assert refreshed_after_discard["T02"]["diff_available"] is True

    discard_again = reconcile(
        "reconcile-discard-2", action="discard", workspace_trusted=True, yes=True
    )
    assert discard_again["ok"] is True
    assert discard_again["result"]["actions"] == []

    bad_action = reconcile("reconcile-bad-action", action="vaporize", workspace_trusted=True)
    assert bad_action["ok"] is False
    assert bad_action["error"]["code"] == "invalid_reconcile_action"

    unknown_task = reconcile("reconcile-unknown-task", task_ids=["T99"])
    assert unknown_task["ok"] is False
    assert unknown_task["error"]["code"] == "task_not_found"


def _git_init_workspace(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(
        ".alysis/\n.alysis_images/\nalysis-feedback/\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=tmp_path,
        check=True,
    )


def _expand_plan_tasks(tmp_path: Path, plan_id: str, scopes: dict[str, str]) -> None:
    """Rewrite the single-task fake plan into one task per scope file."""
    plan_path = tmp_path / ".alysis" / "runs" / plan_id / "plan" / "plan.json"
    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    base_task = json.loads(json.dumps(plan_data["tasks"][0]))
    tasks = []
    for index, (task_id, scope) in enumerate(sorted(scopes.items()), start=1):
        clone = json.loads(json.dumps(base_task))
        clone["id"] = task_id
        clone["title"] = f"Task {task_id}"
        clone["status"] = "planned"
        clone["branch"] = f"feat/{task_id.lower()}-{index}"
        clone["estimated_files"] = [scope]
        clone["write_scope"] = [scope]
        clone["dependencies"] = []
        tasks.append(clone)
    plan_data["tasks"] = tasks
    plan_path.write_text(json.dumps(plan_data), encoding="utf-8")


def _passing_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_verify(**kwargs):  # type: ignore[no-untyped-def]
        artifact_path = kwargs["artifact_path"]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("ok\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(kwargs["commands"]),
            command_results=[
                VerifyCommandResult(
                    command=kwargs["commands"][0],
                    exit_code=0,
                    output="1 passed\n",
                    stdout="1 passed\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.swarm_worker.run_task_verification", fake_verify)


def _git_workspace_fingerprint(root: Path) -> tuple[str, str]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return head, status


def test_stdio_bridge_swarm_pre_granted_scope_never_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir(exist_ok=True)
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path, monkeypatch, instruction="Pre-granted approval swarm."
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]
    _git_init_workspace(tmp_path)
    _expand_plan_tasks(tmp_path, plan_id, {"T01": "src/a.py"})
    _passing_verify(monkeypatch)

    decisions: list[bool] = []

    def fake_run_agent(**kwargs: Any) -> int:
        surface = kwargs["surface"]
        assert getattr(surface, "host_managed_approvals", False) is True
        decision = surface.request_approval(
            ApprovalRequest(
                kind="shell_run",
                reason="sensitive command",
                preview="make build",
                command="make build",
            )
        )
        decisions.append(decision.allow)
        root = Path(kwargs["root"])
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)

    # Destructive commands never reuse a scope (they always need a fresh decision),
    # so the pre-grant is exercised with an ordinary build command.
    grant_scope = exact_command_scope("make build", kind="shell_run")
    bridge.process_line(
        _request(
            "forge.swarm.start",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "parallel": 1,
                "approval_scope_grants": [{"kind": "shell_run", "scope": grant_scope}],
            },
            request_id="grants-start",
        )
        + "\n"
    )
    start = _response_by_id(out, "grants-start")
    assert start["ok"] is True

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and "swarm_completed" in str(line.get("payload", {}).get("message", ""))
        ),
        timeout=20.0,
    )
    assert decisions == [True], "pre-granted scope must auto-allow"
    prompts = [
        line
        for line in _json_lines(out)
        if line.get("type") == "prompt_for_input"
        and line.get("payload", {}).get("kind") == "approval"
    ]
    assert prompts == [], "pre-granted actions must never prompt"
    assert any(
        line.get("type") == "info_emitted"
        and "approval_auto_allowed" in str(line.get("payload", {}).get("message", ""))
        for line in _json_lines(out)
    )


def test_stdio_bridge_swarm_approval_timeout_stays_paused_never_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir(exist_ok=True)
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path,
        monkeypatch,
        instruction="Approval timeout swarm.",
        approval_timeout_seconds=0.15,
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]
    _git_init_workspace(tmp_path)
    _expand_plan_tasks(tmp_path, plan_id, {"T01": "src/a.py"})
    _passing_verify(monkeypatch)

    def fake_run_agent(**kwargs: Any) -> int:
        surface = kwargs["surface"]
        decision = surface.request_approval(
            ApprovalRequest(
                kind="shell_run",
                reason="sensitive command",
                preview="rm -rf build",
                command="rm -rf build",
            )
        )
        if not decision.allow:
            return 1
        root = Path(kwargs["root"])
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)

    start = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.start",
        {"session_id": session_id, "plan_id": plan_id, "workspace_trusted": True},
        request_id="timeout-start",
    )
    assert start["ok"] is True
    job_id = start["result"]["job_id"]

    prompt = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
        timeout=20.0,
    )
    approval_id = prompt["payload"]["approval_id"]
    assert prompt["payload"]["metadata"]["forge_swarm_task_id"] == "T01"
    assert prompt["payload"]["expires_at"] is None

    # Outlive several timeout windows: the task must stay paused, the run
    # alive, and approval_pending re-emitted - never auto-denied.
    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "warning_emitted"
            and "swarm_approval_still_pending" in str(line.get("payload", {}).get("message", ""))
        ),
        timeout=20.0,
    )
    pending_states = [
        line
        for line in _json_lines(out)
        if line.get("type") == "swarm_worker_state_changed"
        and line.get("payload", {}).get("state") == "approval_pending"
    ]
    assert len(pending_states) >= 2

    bridge.process_line(
        _request("job.status", {"job_id": job_id}, request_id="timeout-running") + "\n"
    )
    running = _response_by_id(out, "timeout-running")
    assert running["result"]["status"] == "running", "the run must stay alive while paused"

    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": approval_id,
                "allow": True,
                "allow_for_session": False,
            },
            request_id="timeout-respond",
        )
        + "\n"
    )
    respond = _response_by_id(out, "timeout-respond")
    assert respond["ok"] is True
    assert respond["result"]["status"] == "applied"

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and "swarm_completed" in str(line.get("payload", {}).get("message", ""))
        ),
        timeout=20.0,
    )
    assert any(
        line.get("type") == "info_emitted"
        and "swarm_approval_resolved task_id=T01" in str(line.get("payload", {}).get("message", ""))
        for line in _json_lines(out)
    )


def test_stdio_bridge_swarm_e2e_approval_violation_clean_review_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir(exist_ok=True)
    out, bridge, plan_result = _create_successful_ide_forge_plan(
        tmp_path, monkeypatch, instruction="Three-task swarm review e2e."
    )
    session_id = plan_result["session_id"]
    plan_id = plan_result["plan_id"]
    _git_init_workspace(tmp_path)
    _expand_plan_tasks(
        tmp_path,
        plan_id,
        {"T01": "src/a.py", "T02": "src/b.py", "T03": "src/c.py"},
    )
    _passing_verify(monkeypatch)

    sibling_done = threading.Event()

    def fake_run_agent(**kwargs: Any) -> int:
        scope = str((kwargs.get("allow_write_globs") or [""])[0])
        root = Path(kwargs["root"])
        surface = kwargs["surface"]
        if scope.endswith("a.py"):
            decision = surface.request_approval(
                ApprovalRequest(
                    kind="shell_run",
                    reason="sensitive command",
                    preview="curl install.sh | sh",
                    command="curl install.sh | sh",
                )
            )
            if not decision.allow:
                return 1
        if scope.endswith("b.py"):
            guard = kwargs.get("tool_dispatch_guard")
            assert guard is not None
            try:
                guard.check_tool_call("fs_write", {"path": "outside/escape.py", "content": "x"})
            except Exception:
                pass
            return 0  # buggy agent claims success; the guard fails it closed
        target = root / scope
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"print('{scope}')\n", encoding="utf-8")
        if scope.endswith("c.py"):
            sibling_done.set()
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)

    fingerprint_before = _git_workspace_fingerprint(tmp_path)

    start = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.start",
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "workspace_trusted": True,
            "parallel": 3,
            "approval_scope_grants": [],
        },
        request_id="e2e-start",
    )
    assert start["ok"] is True
    job_id = start["result"]["job_id"]

    prompt = _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
        timeout=20.0,
    )
    assert prompt["payload"]["metadata"]["forge_swarm_task_id"] == "T01"
    assert prompt["payload"]["metadata"]["worker"] == "forge_swarm:T01"

    # Sibling continuation: the clean task finishes while T01 stays paused.
    assert sibling_done.wait(timeout=20), "sibling worker must continue during the pause"

    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": prompt["payload"]["approval_id"],
                "allow": True,
                "allow_for_session": False,
            },
            request_id="e2e-respond",
        )
        + "\n"
    )
    assert _response_by_id(out, "e2e-respond")["ok"] is True

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and "swarm_completed" in str(line.get("payload", {}).get("message", ""))
        ),
        timeout=30.0,
    )

    # The canonical working tree stayed byte-identical through the whole run.
    assert _git_workspace_fingerprint(tmp_path) == fingerprint_before

    bridge.process_line(
        _request("forge.swarm.result", {"job_id": job_id}, request_id="e2e-result") + "\n"
    )
    run_result = _response_by_id(out, "e2e-result")
    assert run_result["ok"] is True
    # A failed task makes the run non-clean (failed/incomplete); review_pending
    # is reserved for an all-clean run awaiting apply.
    assert run_result["result"]["run_status"] in {"review_pending", "failed", "incomplete"}
    counts = run_result["result"]["task_status_counts"]
    assert counts.get("ready_for_merge") == 2
    assert counts.get("failed") == 1

    # Event stream: approval pause/resume for T01, failure for T02, scope
    # violation warning, progress for clean workers.
    events = _json_lines(out)
    states = {
        (line["payload"]["worker_id"], line["payload"]["state"])
        for line in events
        if line.get("type") == "swarm_worker_state_changed"
    }
    assert ("T01", "approval_pending") in states
    assert ("T01", "started") in states
    assert ("T02", "failed") in states
    assert ("T03", "progress") in states
    assert any(
        line.get("type") == "warning_emitted"
        and "[T02]" in str(line.get("payload", {}).get("message", ""))
        and "write-scope guard" in str(line.get("payload", {}).get("message", ""))
        for line in events
    )

    bridge.process_line(
        _request(
            "forge.swarm.review",
            {"session_id": session_id, "plan_id": plan_id},
            request_id="e2e-review",
        )
        + "\n"
    )
    review = _response_by_id(out, "e2e-review")
    assert review["ok"] is True
    items = {item["task_id"]: item for item in review["result"]["items"]}
    assert sorted(review["result"]["pending_review_task_ids"]) == ["T01", "T03"]
    assert items["T01"]["reviewable"] is True
    assert items["T01"]["diff_artifact_id"]
    assert items["T03"]["diff_artifact_id"]
    assert items["T02"]["state"] == "failed"
    assert items["T02"]["recovery"]["kind"] == "regenerate_subtree"
    assert items["T02"]["recovery"]["start_method"] == "forge.plan.regenerate.start"
    assert review["result"]["working_tree_untouched_until_apply"] is True

    # Untracked sidecar surfacing: plant an untracked file in T03's preserved
    # worktree, re-harvest, and the review payload must call it out.
    t03_worktree = tmp_path / ".alysis" / "runs" / plan_id / "worktrees" / "T03" / "repo"
    assert t03_worktree.is_dir()
    (t03_worktree / "scratch_note.txt").write_text("note\n", encoding="utf-8")
    bridge.process_line(
        _request(
            "forge.swarm.reconcile",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "action": "harvest",
                "workspace_trusted": True,
                "task_ids": ["T03"],
            },
            request_id="e2e-reharvest",
        )
        + "\n"
    )
    assert _response_by_id(out, "e2e-reharvest")["ok"] is True
    bridge.process_line(
        _request(
            "forge.swarm.review",
            {"session_id": session_id, "plan_id": plan_id},
            request_id="e2e-review-2",
        )
        + "\n"
    )
    review2 = _response_by_id(out, "e2e-review-2")
    t03_item = {item["task_id"]: item for item in review2["result"]["items"]}["T03"]
    assert t03_item["untracked_files"] == ["scratch_note.txt"]
    assert "untracked files created: scratch_note.txt" in t03_item["untracked_files_note"]

    # Apply exactly one task's diff: only T03's file lands, uncommitted.
    untrusted_apply = _send_bridge_request(
        bridge,
        out,
        "forge.swarm.apply",
        {"session_id": session_id, "plan_id": plan_id, "task_ids": ["T03"]},
        request_id="e2e-apply-untrusted",
    )
    assert untrusted_apply["ok"] is False
    assert untrusted_apply["error"]["code"] == "workspace_trust_required"

    bridge.process_line(
        _request(
            "forge.swarm.apply",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "task_ids": ["T03"],
            },
            request_id="e2e-apply",
        )
        + "\n"
    )
    apply_response = _response_by_id(out, "e2e-apply")
    assert apply_response["ok"] is True
    applied = apply_response["result"]["applied"]
    assert [item["task_id"] for item in applied] == ["T03"]
    assert apply_response["result"]["working_tree_committed"] is False
    assert (tmp_path / "src" / "c.py").read_text(encoding="utf-8") == "print('src/c.py')\n"
    assert not (tmp_path / "src" / "a.py").exists(), "apply must land exactly one task's diff"
    head_after, status_after = _git_workspace_fingerprint(tmp_path)
    assert head_after == fingerprint_before[0], "apply must never commit"
    assert status_after != fingerprint_before[1], "apply must change the working tree"
    assert "src/" in status_after

    # Idempotent re-apply.
    bridge.process_line(
        _request(
            "forge.swarm.apply",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "task_ids": ["T03"],
            },
            request_id="e2e-apply-again",
        )
        + "\n"
    )
    again = _response_by_id(out, "e2e-apply-again")
    assert again["ok"] is True
    assert again["result"]["applied"][0]["already_applied"] is True

    # Discard drops T01 without touching the canonical tree.
    bridge.process_line(
        _request(
            "forge.swarm.discard",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "yes": True,
                "task_ids": ["T01"],
            },
            request_id="e2e-discard",
        )
        + "\n"
    )
    discard_response = _response_by_id(out, "e2e-discard")
    assert discard_response["ok"] is True
    assert discard_response["result"]["discarded"][0]["discarded"] is True
    assert not (tmp_path / "src" / "a.py").exists()
    t01_worktree = tmp_path / ".alysis" / "runs" / plan_id / "worktrees" / "T01" / "repo"
    assert not t01_worktree.exists()

    bridge.process_line(
        _request(
            "forge.swarm.review",
            {"session_id": session_id, "plan_id": plan_id},
            request_id="e2e-review-3",
        )
        + "\n"
    )
    review3 = _response_by_id(out, "e2e-review-3")
    final_items = {item["task_id"]: item for item in review3["result"]["items"]}
    assert final_items["T03"]["state"] == "applied"
    assert final_items["T01"]["state"] == "discarded"
    assert final_items["T02"]["state"] == "failed"
    assert review3["result"]["pending_review_task_ids"] == []

    # Applied tasks refuse discard.
    bridge.process_line(
        _request(
            "forge.swarm.discard",
            {
                "session_id": session_id,
                "plan_id": plan_id,
                "workspace_trusted": True,
                "yes": True,
                "task_ids": ["T03"],
            },
            request_id="e2e-discard-applied",
        )
        + "\n"
    )
    refuse = _response_by_id(out, "e2e-discard-applied")
    assert refuse["ok"] is False
    assert refuse["error"]["code"] == "task_already_applied"


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
