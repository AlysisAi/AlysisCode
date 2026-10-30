"""Approval decisions cannot replace an explicitly selected native runner.

The native execution boundary is a spy: these tests never launch a helper,
shell command, background process or external reviewer.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import alysis_code.windows_sandbox as native
from alysis_code.agent.approval_review import ApprovalReviewer
from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.background_runner import HostBackgroundRunner
from alysis_code.config import AppConfig, ConfigError
from alysis_code.sandbox_runner import (
    HostShellRunner,
    LazyShellRunner,
    build_shell_runner_from_settings,
)
from alysis_code.sandbox_settings import resolve_shell_sandbox_settings
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, ApprovalRequest, NoopSurface
from alysis_code.tools.shell import ShellError


class _Surface(NoopSurface):
    host_managed_approvals = True

    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []
        self.allow = True

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(allow=self.allow)


class _ReviewerClient:
    model = "test-model"

    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=json.dumps(
                {
                    "decision": self.decision,
                    "risk": "medium",
                    "user_authorization": "high",
                    "reason": "Test verdict.",
                }
            ),
            usage=None,
        )


@pytest.fixture
def routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "windows-native")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_FALLBACK", "ask")
    cfg = AppConfig(model="test-model")
    settings = resolve_shell_sandbox_settings(cfg)
    native_calls: list[dict[str, Any]] = []
    failure = {"phase": ""}

    def helper() -> Path:
        if failure["phase"] == "build":
            raise ConfigError("native helper rejected")
        return tmp_path / "unused-helper.exe"

    def execute(self: native.WindowsNativeRunner, **kwargs: Any):
        native_calls.append({"settings": self.settings, **kwargs})
        if failure["phase"] == "run":
            raise ConfigError("native execution rejected")
        return subprocess.CompletedProcess(kwargs["cmd"], 0, "native spy\n", "")

    def unexpected_host(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Approval/native failure must never execute a host fallback")

    monkeypatch.setattr(native, "resolve_helper", helper)
    monkeypatch.setattr(native.WindowsNativeRunner, "run", execute)
    monkeypatch.setattr(HostShellRunner, "run", unexpected_host)
    monkeypatch.setattr(HostBackgroundRunner, "start", unexpected_host)

    def tools_for(mode: str, decision: str = "allow"):
        store = SessionStore(
            enabled=False,
            sessions_dir=tmp_path / "sessions",
            session_id="native-approval-routing",
            cwd=str(tmp_path),
            repo_root=str(tmp_path),
        )
        store.append("user_message", {"content": "Run the approved test command."})
        surface = _Surface()
        client = _ReviewerClient(decision)
        reviewer = ApprovalReviewer(timeout_seconds=5)
        reviewer.bind(SimpleNamespace(client=client, cfg=cfg, store=store, api_key="test-key"))
        tools = build_tools(
            root=tmp_path,
            console=None,
            store=store,
            mode=mode,
            yes=False,
            cfg=cfg,
            surface=surface,
            approval_reviewer=reviewer,
            shell_runner=LazyShellRunner(
                lambda: build_shell_runner_from_settings(settings, tmp_path)
            ),
        )
        return tools, surface, client

    return SimpleNamespace(
        tools_for=tools_for, calls=native_calls, failure=failure, settings=settings
    )


@pytest.mark.parametrize("mode", ["review", "auto", "fullaccess"])
def test_approval_allow_keeps_explicit_native_runner(routing: Any, mode: str) -> None:
    tools, surface, reviewer = routing.tools_for(mode)
    result = tools["shell_run"].run({"cmd": "git reset --hard"})
    assert result["stdout"] == "native spy\n"
    assert len(routing.calls) == 1
    assert routing.calls[0]["settings"] is routing.settings
    assert routing.settings.backend == "windows-native"
    assert routing.settings.mode == "strict"
    assert routing.settings.unavailable_fallback == "ask"
    assert len(surface.requests) == 1
    assert surface.requests[0].metadata["always_ask_person"]
    assert surface.requests[0].metadata["mandatory_explicit_approval"]
    assert reviewer.calls == []


def test_person_denial_never_reaches_native_or_host_runner(routing: Any) -> None:
    # An AI ALLOW cannot answer the independent PowerShell decision.
    tools, surface, reviewer = routing.tools_for("auto", "allow")
    surface.allow = False
    with pytest.raises(ApprovalDeclinedError):
        tools["shell_run"].run({"cmd": "git reset --hard"})
    assert routing.calls == []
    assert len(surface.requests) == 1
    assert surface.requests[0].metadata["always_ask_person"]
    assert reviewer.calls == []


@pytest.mark.parametrize("mode", ["review", "auto", "fullaccess"])
@pytest.mark.parametrize("failure_phase", ["build", "run"])
def test_approved_native_failure_never_uses_generic_ask_fallback(
    routing: Any, mode: str, failure_phase: str
) -> None:
    routing.failure["phase"] = failure_phase
    tools, surface, reviewer = routing.tools_for(mode)
    with pytest.raises(ShellError, match="native (helper|execution) rejected"):
        tools["shell_run"].run({"cmd": "git reset --hard"})
    assert len(routing.calls) == (1 if failure_phase == "run" else 0)
    assert len(surface.requests) == 1
    assert surface.requests[0].metadata["always_ask_person"]
    assert surface.requests[0].metadata["mandatory_explicit_approval"]
    assert reviewer.calls == []
