"""Sensitive answer-only turns cannot acquire repository tools during run_turn."""

import copy
from types import SimpleNamespace

import pytest
from test_computer_use_completion_display import _Surface
from test_computer_use_recovery import _ScriptedComputerUseClient

from alysis_code import computer_use_platform_policy as policy
from alysis_code.agent import task_artifact_binding, task_evidence
from alysis_code.agent.session import create_session
from alysis_code.agent.turn import core
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall


@pytest.fixture(autouse=True)
def mac_host(monkeypatch):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="darwin"))


def run_answer(tmp_path, monkeypatch, *, mode, request_tool, sensitive=True):
    responses = []
    if request_tool:
        responses.append(
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="attempt", name="task_artifacts", arguments={"paths": []})],
                raw={},
            )
        )
    responses.append(LLMResponse(content="Answer-only reply.", tool_calls=[], raw={}))
    client = _ScriptedComputerUseClient(responses)
    surface = _Surface()
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode=mode,
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="fixture",
        surface=surface,
        sensitive_media=sensitive,
        verification_enabled=False,
    )
    session.client = client
    if sensitive:
        assert session.tools == {} and session.tool_list == []
    observed = {
        "tools": [],
        "unknown": [],
        "begin": [],
        "acceptance": [],
        "bind": [],
        "refresh": [],
    }

    def spy(owner, name, key):
        original = getattr(owner, name)

        def observe(*args, **kwargs):
            observed[key].append(True)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, observe)

    spy(task_evidence, "begin_task_evidence", "begin")
    spy(core, "build_acceptance_contract", "acceptance")
    spy(core, "_refresh_execute_turn_verification_selection", "refresh")
    spy(task_artifact_binding, "bind_path_interpretation", "bind")
    original_unknown = core.build_unknown_tool_recovery_payload

    def unknown(**kwargs):
        result = original_unknown(**kwargs)
        observed["unknown"].append(copy.deepcopy(result))
        return result

    monkeypatch.setattr(core, "build_unknown_tool_recovery_payload", unknown)
    original_chat = core._main_agent_chat

    def chat(**kwargs):
        observed["tools"].append(copy.deepcopy(kwargs.get("tools") or []))
        return original_chat(**kwargs)

    monkeypatch.setattr(core, "_main_agent_chat", chat)
    try:
        code = session.run_turn("Read the supplied context and give an answer.")
        observed["code"] = code
        observed["messages"] = list(surface.messages)
        observed["contract"] = session._turn_execution_state.acceptance_contract
        return observed
    finally:
        session.close()


@pytest.mark.parametrize("mode", ["auto", "review", "readonly"])
@pytest.mark.parametrize("request_tool", [False, True])
def test_sensitive_answer_only_has_no_repo_setup_or_dispatch(
    tmp_path, monkeypatch, mode, request_tool
):
    observed = run_answer(tmp_path, monkeypatch, mode=mode, request_tool=request_tool)
    assert observed["code"] == 0
    assert observed["tools"] == ([[], []] if request_tool else [[]])
    assert observed["messages"] == ["Answer-only reply."]
    assert (
        observed["begin"] == observed["acceptance"] == observed["bind"] == observed["refresh"] == []
    )
    assert observed["contract"] is None
    assert len(observed["unknown"]) == int(request_tool)
    if request_tool:
        assert observed["unknown"][0]["available_tool_names"] == []


def test_ordinary_auto_retains_repository_setup_and_tool(tmp_path, monkeypatch):
    observed = run_answer(tmp_path, monkeypatch, mode="auto", request_tool=False, sensitive=False)
    assert observed["code"] == 0
    # The original setup begins evidence, then install_task_evidence reuses it.
    assert len(observed["begin"]) == 2
    assert len(observed["acceptance"]) == len(observed["refresh"]) == 1
    assert observed["contract"] is not None
    assert any(
        tool.get("function", {}).get("name") == "task_artifacts" for tool in observed["tools"][0]
    )
    assert observed["unknown"] == observed["bind"] == []
