from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

path = Path(__file__).resolve().parents[1] / "scripts/qa/bots_coding_context_study.py"
spec = importlib.util.spec_from_file_location("coding_context_study", path)
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


def test_shared_budget_reserves_parent_review_and_counts_real_attempts():
    actual = []

    def chat(**kwargs):
        actual.append(kwargs)
        return SimpleNamespace(usage=None, tool_calls=[])

    budget = study.Budget(lambda: None, max_calls=5)
    parent = budget.wrap(SimpleNamespace(chat=chat), "parent")
    child = budget.wrap(SimpleNamespace(chat=chat), "child")
    parent(max_tokens=9000)
    child()
    child()
    with pytest.raises(RuntimeError, match="allowance"):
        child()
    parent()
    parent()
    with pytest.raises(RuntimeError, match="allowance"):
        parent()
    assert len(actual) == len(budget.records) == 5
    assert all(call["max_tokens"] == 3000 and call["stream"] is False for call in actual)
    assert [r["phase"] for r in budget.records] == ["parent", "child", "child", "parent", "parent"]


def test_uncertain_provider_failure_prevents_all_further_calls():
    def fail(**kwargs):
        raise TimeoutError("synthetic transport failure")

    budget = study.Budget(lambda: None)
    child = budget.wrap(SimpleNamespace(chat=fail), "child")
    with pytest.raises(study.StudyProviderFailure):
        child()
    parent = budget.wrap(SimpleNamespace(chat=lambda **kw: pytest.fail("must not call")), "parent")
    with pytest.raises(RuntimeError, match="provider failed"):
        parent()
    assert len(budget.records) == 1 and budget.records[0]["status"] == "unconfirmed"


def test_enforced_phases_reserve_single_tool_free_reports_and_share_total():
    calls = []
    client = SimpleNamespace(
        chat=lambda **kw: calls.append(kw) or SimpleNamespace(usage=None, tool_calls=[])
    )
    budget = study.Budget(lambda: None, max_calls=4, total_calls=4, enforce_phases=True)
    parent = budget.wrap(client, "parent")
    child = budget.wrap(client, "child")
    child_report = budget.wrap(client, "child_report")
    parent_report = budget.wrap(client, "parent_report")
    parent()
    child()
    for execution in (parent, child):
        with pytest.raises(study.StudyCallAllowanceReached):
            execution()
    child_report(tools=[{"name": "must_be_removed"}])
    with pytest.raises(study.StudyCallAllowanceReached):
        child_report()
    parent_report(tools=[{"name": "must_be_removed"}])
    assert calls[-2]["tools"] is calls[-1]["tools"] is None
    assert len(calls) == len(budget.records) == 4
    budget.stage += 1
    budget.stage_calls = 0
    with pytest.raises(study.StudyCallAllowanceReached):
        parent()


def test_report_tool_attempt_is_counted_but_rejected_before_dispatch():
    client = SimpleNamespace(
        chat=lambda **kw: SimpleNamespace(
            usage=None, tool_calls=[SimpleNamespace(name="coding_task")]
        )
    )
    budget = study.Budget(lambda: None, enforce_phases=True)
    with pytest.raises(study.StudyReportToolCallRejected):
        budget.wrap(client, "parent_report")()
    assert budget.records[0]["status"] == "completed"
    assert budget.records[0]["tools"] == ["coding_task"]


@pytest.mark.parametrize("failure", [TimeoutError("private diagnostic"), KeyboardInterrupt()])
def test_failed_report_never_retries_or_exposes_private_exception(failure):
    def fail(**kwargs):
        raise failure

    budget = study.Budget(lambda: None, enforce_phases=True)
    expected = study.StudyProviderFailure if isinstance(failure, Exception) else KeyboardInterrupt
    with pytest.raises(expected) as caught:
        budget.wrap(SimpleNamespace(chat=fail), "child_report")()
    assert "private diagnostic" not in str(caught.value)
    with pytest.raises(RuntimeError, match="provider failed"):
        budget.wrap(SimpleNamespace(chat=lambda **kw: pytest.fail("no retry")), "parent_report")()
    assert len(budget.records) == 1


@pytest.mark.parametrize("summary_mode", ["model", "tool", "failure"])
def test_real_engine_capped_turn_uses_reserved_summary_without_replaying(tmp_path, summary_mode):
    from alysis_code.agent.session import create_session
    from alysis_code.config import AppConfig
    from alysis_code.llm.openai_compat import LLMResponse, ToolCall

    session = create_session(
        cfg=AppConfig(
            model=study.MODEL,
            routing_mode="code_only",
            subagents_enabled=False,
            skills_enabled=False,
            custom_tools_enabled=False,
            web_tools_enabled=False,
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=False,
        api_key_override="fixture-no-provider-access",
        session_log_dir_override=tmp_path / "logs",
    )
    calls = []

    def chat(**kwargs):
        calls.append(kwargs)
        if kwargs.get("tools") is not None:
            return LLMResponse(
                content="Inspecting",
                tool_calls=[ToolCall(id="inspect-once", name="fs_list", arguments={"path": "."})],
                raw={},
            )
        if summary_mode == "failure":
            raise TimeoutError("private diagnostic")
        return LLMResponse(
            content="Inspected files; implementation remains incomplete.",
            tool_calls=[]
            if summary_mode == "model"
            else [ToolCall(id="forbidden", name="fs_list", arguments={"path": "."})],
            raw={},
        )

    session.client = SimpleNamespace(chat=chat, model=study.MODEL, temperature=0.2)
    budget = study.Budget(lambda: None, max_calls=3, total_calls=3, enforce_phases=True)
    report_chat = budget.wrap(session.client, "child_report")
    execute_chat = budget.wrap(session.client, "child")
    session.client.chat = execute_chat
    try:
        result = study.run_child_with_report(
            session, "Inspect and implement this repository task.", report_chat
        )
        events = session.store.events_snapshot()
        assert session.client.chat is execute_chat
    finally:
        session.close()
    assert result["exit_code"] == 1
    assert result["error_type"] == "StudyCallAllowanceReached"
    assert result["report"]
    assert result["report_source"] == ("model" if summary_mode == "model" else "runtime_fallback")
    assert len(calls) == 2 and calls[1]["tools"] is None
    assert [r["phase"] for r in budget.records] == ["child", "child_report"]
    results = [e for e in events if e.get("type") == "tool_result"]
    assert len(results) == 1
    assert "private diagnostic" not in json.dumps(events)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.z.ai/api/paas/v4",
        "https://api.example.test/v1",
        "http://api.z.ai/api/coding/paas/v4",
        "https://api.z.ai/api/coding/paas/v4?key=placeholder",
    ],
)
def test_non_plan_or_insecure_endpoint_rejected(url):
    with pytest.raises(RuntimeError, match="Coding Plan"):
        study.validate_plan(SimpleNamespace(base_url=url, protocol="openai_compat"))


def test_plan_endpoint_accepted_without_credential_access():
    study.validate_plan(
        SimpleNamespace(base_url="https://api.z.ai/api/coding/paas/v4", protocol="openai_compat")
    )


def test_child_allowance_failure_returns_to_parent_without_stale_result():
    child = SimpleNamespace(
        messages=[{"role": "assistant", "content": "older success"}],
        last_turn_outcome={"state": "older success"},
    )

    def stop(request):
        raise study.StudyCallAllowanceReached("limit")

    child.run_turn = stop
    result = study.run_child_request(child, "new work")
    assert result == {
        "exit_code": 1,
        "error_type": "StudyCallAllowanceReached",
        "outcome": None,
        "report": None,
    }


def test_child_provider_exception_never_serializes_exception_text():
    child = SimpleNamespace(messages=[], last_turn_outcome={})

    def fail(request):
        raise TimeoutError("synthetic private diagnostic")

    child.run_turn = fail
    result = study.run_child_request(child, "work")
    assert result["error_type"] == "TimeoutError"
    assert "synthetic private diagnostic" not in str(result)


def test_successful_child_report_is_from_current_request():
    child = SimpleNamespace(
        messages=[{"role": "assistant", "content": "older success"}],
        last_turn_outcome={"state": "verified"},
    )

    def finish(request):
        child.messages.append({"role": "assistant", "content": "current report"})
        return 0

    child.run_turn = finish
    assert study.run_child_request(child, "work")["report"] == "current report"


def test_green_held_out_checks_do_not_hide_failing_repository_tests():
    turn = {
        "finished": True,
        "reply": "Implemented and verified",
        "independent_checks": {"exit_code": 0},
        "repository_tests": {"exit_code": 1},
    }
    assert not study.turn_passed(turn, delegated=False)
    turn["repository_tests"]["exit_code"] = 0
    assert study.turn_passed(turn, delegated=False)
    turn["reply"] = ""
    assert not study.turn_passed(turn, delegated=False)


def test_parent_final_cannot_hide_an_incomplete_child_or_failed_review():
    turn = {
        "finished": True,
        "reply": "Done",
        "independent_checks": {"exit_code": 0},
        "repository_tests": {"exit_code": 0},
        "delegations": [{"exit_code": 1}],
        "reviews": [{"diff": {"exit_code": 0}, "tests": {"exit_code": 0}}],
    }
    assert not study.turn_passed(turn, delegated=True)
    turn["delegations"][0]["exit_code"] = 0
    assert study.turn_passed(turn, delegated=True)
    turn["reviews"][0]["tests"]["exit_code"] = 1
    assert not study.turn_passed(turn, delegated=True)


def test_remaining_allowance_is_visible_without_mutating_conversation():
    seen = []
    client = SimpleNamespace(
        chat=lambda **kwargs: seen.append(kwargs) or SimpleNamespace(usage=None, tool_calls=[])
    )
    messages = [{"role": "user", "content": "preserved context"}]
    budget = study.Budget(lambda: None, max_calls=5, total_calls=5)
    parent = budget.wrap(client, "parent")
    child = budget.wrap(client, "child")
    parent(messages=messages)
    child(messages=messages)
    child(messages=messages)
    with pytest.raises(study.StudyCallAllowanceReached):
        child(messages=messages)
    parent(messages=messages)
    parent(messages=messages)
    budget.stage_calls = 0
    with pytest.raises(study.StudyCallAllowanceReached):
        parent(messages=messages)
    assert messages == [{"role": "user", "content": "preserved context"}]
    assert [record["allowance_including_this_call"] for record in budget.records] == [5, 2, 1, 2, 1]
    assert "1 model calls remain" in seen[2]["messages"][-1]["content"]
    assert seen[2]["messages"][-1]["role"] == "system"


@pytest.mark.parametrize("results", [[], ["unknown"], ["a", "a"]])
def test_saved_history_requires_exactly_one_result_for_each_call(results):
    messages = [{"role": "assistant", "tool_calls": [{"id": "a"}]}]
    messages.extend({"role": "tool", "tool_call_id": value} for value in results)
    with pytest.raises(ValueError):
        study.require_complete_tool_history(messages)


def test_complete_saved_history_may_end_with_tool_result():
    study.require_complete_tool_history(
        [
            {"role": "user", "content": "context"},
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "b"},
            {"role": "tool", "tool_call_id": "a"},
        ]
    )


@pytest.fixture
def continuation_source(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "ROOT", tmp_path)
    source = tmp_path / "reports" / "partial"
    workspace = source / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "notes.py").write_text("# preserved partial work\n")
    report = {
        "arm": "delegated",
        "profile": study.PROFILE,
        "model": study.MODEL,
        "reasoning_effort": "low",
        "live_model": True,
        "passed": False,
        "workspace": str(workspace),
        "fixture_hashes": study.fixture_hashes(),
        "provider_calls": [{"status": "completed"}],
        "turns": [
            {
                "stage": 1,
                "finished": True,
                "reply": "Still incomplete",
                "delegations": [{"error_type": "StudyCallAllowanceReached"}],
            }
        ],
    }
    study.save(source / "report.json", report)
    study.save(source / "requests.json", {"context": study.CONTEXT, "tasks": study.TASKS})
    study.save(
        source / "parent-messages.json", [{"role": "assistant", "content": "Still incomplete"}]
    )
    (source / "sessions").mkdir()
    events = [
        {"type": "user_message", "payload": {"content": "Preserved context"}},
        {
            "type": "assistant_message",
            "payload": {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "a", "function": {"name": "shell_run", "arguments": "{}"}}
                    ],
                }
            },
        },
        {"type": "tool_result", "payload": {"tool_call_id": "a", "result": {"exit_code": 0}}},
    ]
    (source / "sessions" / "child.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    return source


def test_continuation_keeps_source_and_complete_history(continuation_source):
    paths = [p for p in continuation_source.rglob("*") if p.is_file()]
    before = {str(p): p.read_bytes() for p in paths}
    result = study.load_continuation(continuation_source)
    assert result["provenance"]["original_passed"] is False
    assert result["provenance"]["synthesized_tool_results"] == 0
    assert result["child"][-1]["role"] == "tool"
    assert {str(p): p.read_bytes() for p in paths} == before


def test_continuation_refuses_uncertain_provider_attempt(continuation_source):
    path = continuation_source / "report.json"
    report = json.loads(path.read_text())
    report["provider_calls"][0]["status"] = "unconfirmed"
    study.save(path, report)
    with pytest.raises(ValueError, match="confirmed partial study"):
        study.load_continuation(continuation_source)


def test_continuation_refuses_synthetic_missing_result(continuation_source):
    path = continuation_source / "sessions" / "child.jsonl"
    path.write_text("\n".join(path.read_text().splitlines()[:-1]) + "\n")
    with pytest.raises(ValueError, match="no replay"):
        study.load_continuation(continuation_source)
