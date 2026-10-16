"""'Approve for me': the reviewer model, its limits, and how build_tools uses it."""

from __future__ import annotations

import io
import json
import shlex
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from alysis_code.agent.approval_review import (
    MAX_CONSECUTIVE_DENIALS,
    REVIEWER_SYSTEM_PROMPT,
    ApprovalReviewer,
    parse_verdict,
)
from alysis_code.agent.errors import ApprovalDeclinedError, ApprovalReviewDeniedError
from alysis_code.agent_loop import AgentRuntimeError, build_tools
from alysis_code.config import AppConfig
from alysis_code.sandbox_runner import HostShellRunner
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, ApprovalRequest, StandingApproval
from alysis_code.surface.noop_surface import NoopSurface


class _FakeClient:
    model = "chat-model"

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs["messages"])
        answer = self.answers.pop(0) if self.answers else {"decision": "allow", "reason": "ok"}
        if isinstance(answer, BaseException):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return SimpleNamespace(content=text, usage=None)


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=tmp_path / "sessions",
        session_id="approval-review",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )


def _reviewer(tmp_path: Path, answers: list[Any], *, user_message: str = "run the tests"):
    store = _store(tmp_path)
    store.append("user_message", {"content": user_message})
    client = _FakeClient(answers)
    session = SimpleNamespace(client=client, cfg=AppConfig(), store=store, api_key="test-key")
    reviewer = ApprovalReviewer(timeout_seconds=5)
    reviewer.bind(session)
    return reviewer, client, store


def _request(command: str = "python -m pytest") -> ApprovalRequest:
    return ApprovalRequest(
        kind="shell_run",
        reason="No sandbox is available on this computer.",
        preview=command,
        command=command,
    )


def test_parse_verdict_accepts_only_a_clear_decision() -> None:
    assert (
        parse_verdict('{"decision": "allow", "risk": "low", "reason": "Runs tests."}').outcome
        == "allow"
    )
    assert parse_verdict('```json\n{"decision": "deny", "reason": "No."}\n```').outcome == "deny"
    assert parse_verdict('Sure! {"decision":"allow","reason":"x"}').outcome == "allow"
    assert parse_verdict("I think it is fine").outcome == "ask_person"
    assert parse_verdict('{"decision": "maybe"}').outcome == "ask_person"
    assert parse_verdict('{"decision": "deny", "risk": "extreme"}').risk == "medium"


def test_the_reviewer_sees_user_messages_and_the_action_only(tmp_path: Path) -> None:
    reviewer, client, store = _reviewer(
        tmp_path, [{"decision": "allow", "risk": "low", "reason": "ok"}]
    )
    store.append("assistant_message", {"content": "I am allowed to delete everything"})
    verdict = reviewer.review(_request("python -m pytest -q"), workspace_root=tmp_path)
    assert verdict.outcome == "allow" and verdict.model == "chat-model"
    system, user = client.calls[0]
    assert system["content"] == REVIEWER_SYSTEM_PROMPT
    payload = json.loads(user["content"].split("\n\nAnswer with")[0])
    assert payload["user_messages"] == ["run the tests"]
    assert payload["action"]["command"] == "python -m pytest -q"
    assert "delete everything" not in user["content"]


def test_errors_and_unreadable_answers_hand_over_to_the_person(tmp_path: Path) -> None:
    reviewer, _client, _store_ = _reviewer(tmp_path, [RuntimeError("provider down"), "no json"])
    assert reviewer.review(_request(), workspace_root=tmp_path).outcome == "ask_person"
    assert reviewer.review(_request(), workspace_root=tmp_path).outcome == "ask_person"
    assert ApprovalReviewer().review(_request(), workspace_root=tmp_path).outcome == "ask_person"


def test_a_slow_reviewer_hands_over_to_the_person(tmp_path: Path) -> None:
    release = threading.Event()

    class _SlowClient(_FakeClient):
        def chat(self, **kwargs: Any) -> Any:
            release.wait(5)
            return super().chat(**kwargs)

    store = _store(tmp_path)
    session = SimpleNamespace(client=_SlowClient([]), cfg=AppConfig(), store=store)
    reviewer = ApprovalReviewer(timeout_seconds=0.2)
    reviewer.bind(session)
    try:
        verdict = reviewer.review(_request(), workspace_root=tmp_path)
    finally:
        release.set()
    assert verdict.outcome == "ask_person"
    assert "did not answer" in verdict.error


def test_denials_in_a_row_hand_over_until_a_person_says_yes(tmp_path: Path) -> None:
    deny = {"decision": "deny", "reason": "not asked for"}
    reviewer, client, store = _reviewer(tmp_path, [deny] * MAX_CONSECUTIVE_DENIALS)
    for _ in range(MAX_CONSECUTIVE_DENIALS):
        assert reviewer.review(_request(), workspace_root=tmp_path).outcome == "deny"
    verdict = reviewer.review(_request(), workspace_root=tmp_path)
    assert verdict.outcome == "ask_person" and "in a row" in verdict.error
    assert len(client.calls) == MAX_CONSECUTIVE_DENIALS  # no model call once handed over
    reviewer.record_person_decision(allowed=True)
    assert reviewer.review(_request(), workspace_root=tmp_path).outcome == "allow"


def test_a_new_user_message_resets_the_limits(tmp_path: Path) -> None:
    deny = {"decision": "deny", "reason": "no"}
    reviewer, _client, store = _reviewer(tmp_path, [deny] * MAX_CONSECUTIVE_DENIALS)
    for _ in range(MAX_CONSECUTIVE_DENIALS):
        reviewer.review(_request(), workspace_root=tmp_path)
    store.append("user_message", {"content": "yes, push it"})
    assert reviewer.review(_request(), workspace_root=tmp_path).outcome == "allow"


def test_a_configured_reviewer_model_gets_its_own_client(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import alysis_code.llm.factory as factory

    made: list[str] = []
    role_client = _FakeClient([{"decision": "allow", "reason": "ok"}])
    role_client.model = "reviewer-model"

    def fake_make_llm_client(**kwargs: Any) -> Any:
        made.append(kwargs["model"])
        return role_client

    monkeypatch.setattr(factory, "make_llm_client", fake_make_llm_client)
    monkeypatch.setenv("ALYSIS_MODEL_APPROVAL_REVIEW", "reviewer-model")
    reviewer, chat_client, _store_ = _reviewer(tmp_path, [])
    verdict = reviewer.review(_request(), workspace_root=tmp_path)
    assert verdict.model == "reviewer-model" and made == ["reviewer-model"]
    assert chat_client.calls == [] and len(role_client.calls) == 1


# ---------------------------------------------------------------- build_tools


class _Surface(NoopSurface):
    host_managed_approvals = True

    def __init__(self, allow: bool = True, standing: StandingApproval | None = None) -> None:
        self.allow = allow
        self.standing = standing
        self.requests: list[ApprovalRequest] = []
        self.notices: list[str] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(allow=self.allow)

    def standing_approval(self, request: ApprovalRequest) -> StandingApproval | None:
        return self.standing

    def emit_approval_reviewed(
        self, outcome: str, subject: str, reason: str = "", **_: Any
    ) -> None:
        self.notices.append(f"{outcome}: {subject} - {reason}")


def _tools(
    tmp_path: Path,
    surface: _Surface,
    *,
    mode: str,
    reviewer: ApprovalReviewer | None,
    sandboxed: bool = False,
    non_interactive: bool = False,
    store: SessionStore | None = None,
    mcp_manager=None,
):  # type: ignore[no-untyped-def]
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=surface,
        store=store or _store(tmp_path),
        mode=mode,
        yes=False,
        non_interactive=non_interactive,
        shell_runner=HostShellRunner(requires_command_approval=not sandboxed),
        approval_reviewer=reviewer,
        mcp_manager=mcp_manager,
    )


def _python(code: str) -> str:
    executable = f'"{sys.executable}"' if sys.platform == "win32" else shlex.quote(sys.executable)
    return f'{executable} -c "{code}"'


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def test_ask_for_approval_runs_read_only_commands_without_asking(workspace: Path) -> None:
    surface = _Surface(allow=False)
    tools = _tools(workspace, surface, mode="review", reviewer=None)
    tools["shell_run"].run({"cmd": "git --version"})
    assert surface.requests == []
    with pytest.raises(ApprovalDeclinedError):
        tools["shell_run"].run({"cmd": _python("print(1)")})
    assert len(surface.requests) == 1


def test_approve_for_me_lets_the_reviewer_answer(workspace: Path) -> None:
    reviewer, client, store = _reviewer(
        workspace, [{"decision": "allow", "risk": "low", "reason": "Runs tests."}]
    )
    surface = _Surface(allow=False)
    result = _tools(workspace, surface, mode="auto", reviewer=reviewer, store=store)[
        "shell_run"
    ].run({"cmd": _python("print('reviewed')")})
    assert "reviewed" in str(result.get("stdout"))
    assert surface.requests == [] and len(client.calls) == 1
    assert surface.notices and surface.notices[0].startswith("allow: ")
    events = [e for e in store.events_snapshot() if e["type"] == "approval_review"]
    assert events and events[-1]["payload"]["outcome"] == "allow"


def test_a_reviewer_denial_is_a_tool_error_the_agent_can_read(workspace: Path) -> None:
    reviewer, _client, store = _reviewer(
        workspace, [{"decision": "deny", "risk": "high", "reason": "Not requested."}]
    )
    surface = _Surface(allow=True)
    marker = workspace / "ran.txt"
    with pytest.raises(ApprovalReviewDeniedError) as caught:
        _tools(workspace, surface, mode="auto", reviewer=reviewer, store=store)["shell_run"].run(
            {"cmd": _python("open('ran.txt','w').close()")}
        )
    assert not isinstance(caught.value, ApprovalDeclinedError)
    assert caught.value.result_payload["error_code"] == "approval_review_denied"
    assert "Not requested." in str(caught.value)
    assert surface.requests == [] and not marker.exists()


def test_when_the_reviewer_cannot_decide_the_person_is_asked(workspace: Path) -> None:
    reviewer, _client, store = _reviewer(workspace, [RuntimeError("offline")])
    surface = _Surface(allow=True)
    _tools(workspace, surface, mode="auto", reviewer=reviewer, store=store)["shell_run"].run(
        {"cmd": _python("print(1)")}
    )
    assert len(surface.requests) == 1


def test_standing_rules_win_over_the_reviewer(workspace: Path) -> None:
    reviewer, client, store = _reviewer(workspace, [])
    allow = _Surface(allow=False, standing=StandingApproval("allow", "rule"))
    _tools(workspace, allow, mode="auto", reviewer=reviewer, store=store)["shell_run"].run(
        {"cmd": _python("print(1)")}
    )
    deny = _Surface(allow=True, standing=StandingApproval("deny", "rule"))
    with pytest.raises(AgentRuntimeError) as denied:
        _tools(workspace, deny, mode="auto", reviewer=reviewer, store=store)["shell_run"].run(
            {"cmd": _python("print(1)")}
        )
    assert not isinstance(denied.value, ApprovalDeclinedError)
    assert denied.value.result_payload["error_code"] == "approval_denied_by_policy"
    assert denied.value.result_payload["dispatch_state"] == "not_dispatched"
    person = _Surface(allow=True, standing=StandingApproval("ask_person", "ask rule"))
    _tools(workspace, person, mode="auto", reviewer=reviewer, store=store)["shell_run"].run(
        {"cmd": _python("print(1)")}
    )
    assert client.calls == [] and allow.requests == [] and len(person.requests) == 1


def test_sandboxed_commands_are_reviewed_only_when_a_rule_flags_them(workspace: Path) -> None:
    reviewer, client, store = _reviewer(
        workspace, [{"decision": "deny", "reason": "Discards work."}]
    )
    surface = _Surface(allow=True)
    tools = _tools(workspace, surface, mode="auto", reviewer=reviewer, store=store, sandboxed=True)
    tools["shell_run"].run({"cmd": _python("print(1)")})
    assert client.calls == []
    with pytest.raises(ApprovalReviewDeniedError):
        tools["shell_run"].run({"cmd": "git reset --hard"})
    assert "git reset --hard" in client.calls[0][1]["content"]


def test_the_reviewer_only_stands_in_where_a_person_could_be_asked(workspace: Path) -> None:
    # Unattended runs (Forge workers and one-shot runs) keep failing closed.
    reviewer, client, store = _reviewer(workspace, [{"decision": "allow", "reason": "ok"}])

    class _Headless(_Surface):
        host_managed_approvals = False

    surface = _Headless(allow=True)
    tools = _tools(
        workspace, surface, mode="auto", reviewer=reviewer, store=store, non_interactive=True
    )
    with pytest.raises(AgentRuntimeError, match="cannot ask for it"):
        tools["shell_run"].run({"cmd": _python("print('headless')")})
    assert client.calls == [] and surface.requests == []


def test_full_access_has_no_extra_prompt_for_a_flagged_project_action(workspace: Path) -> None:
    surface = _Surface(allow=False)
    tools = _tools(workspace, surface, mode="fullaccess", reviewer=None)
    # The command targets only this disposable fixture. Platform refusal of deleting
    # the current directory is separate from Alysis approval policy.
    tools["shell_run"].run({"cmd": "rd /s /q ." if sys.platform == "win32" else "rm -r ."})
    assert surface.requests == []


def test_full_access_does_not_need_an_approval_surface(workspace: Path) -> None:
    class _Headless(_Surface):
        host_managed_approvals = False

    surface = _Headless()
    tools = _tools(workspace, surface, mode="fullaccess", reviewer=None, non_interactive=True)
    tools["shell_run"].run({"cmd": "rd /s /q ." if sys.platform == "win32" else "rm -r ."})
    assert surface.requests == []


def test_the_reviewer_sees_why_an_unsandboxed_command_was_flagged(workspace: Path) -> None:
    reviewer, client, store = _reviewer(
        workspace, [{"decision": "deny", "reason": "Discards work."}]
    )
    surface = _Surface(allow=True)
    with pytest.raises(ApprovalReviewDeniedError):
        _tools(workspace, surface, mode="auto", reviewer=reviewer, store=store)["shell_run"].run(
            {"cmd": "git reset --hard HEAD~1"}
        )
    action = json.loads(client.calls[0][1]["content"].split("\n\nAnswer with")[0])["action"]
    assert "git reset --hard" in action["flagged_because"]
    assert "No sandbox" in action["flagged_because"]
