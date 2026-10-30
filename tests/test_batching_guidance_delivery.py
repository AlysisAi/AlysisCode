"""Exercise prompt delivery, not a model's willingness to batch tool calls."""

from __future__ import annotations

import copy
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code import agent_loop
from alysis_code.agent import session as session_mod
from alysis_code.agent.prompt_guidance import render_guidance
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse
from alysis_code.prompt_guidance_catalog import (
    ALL_GUIDANCE_PROFILES,
    GUIDANCE_PROFILES,
    PROMPT_FAMILIES,
    subagent_profile,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.subagents import built_in_subagents

_NETWORK_DENIED = "prompt delivery test forbids network access"
_SUBPROCESS_GUARD = f"""import socket
def deny(*args, **kwargs):
    raise RuntimeError({_NETWORK_DENIED!r})
socket.create_connection = deny
socket.getaddrinfo = deny
socket.socket.connect = deny
socket.socket.connect_ex = deny
"""


@pytest.fixture(autouse=True)
def _isolated_offline_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Block constructor HTTP too; a fake key is not a network boundary.

    The inherited sitecustomize also blocks sockets in ordinary Python child
    processes. These tests do not launch external HTTP tools or isolated Python.
    """

    def deny(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(_NETWORK_DENIED)

    guard = tmp_path / "network-guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(_SUBPROCESS_GUARD)
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, (str(guard), os.environ.get("PYTHONPATH"))))
    )
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(httpx.Client, "send", deny)
    monkeypatch.setattr(httpx.AsyncClient, "send", deny)


class _RecordingClient:
    """Capture the actual turn request without a provider or tool decisions."""

    def __init__(self, *, model: str, **_kwargs: Any) -> None:
        self.model = model
        self.temperature = 0.0
        self.requests: list[dict[str, Any]] = []
        self.report = f"Recorded request {model}; no source inspection or execution was performed."

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.requests.append(
            {"messages": copy.deepcopy(kwargs["messages"]), "tools": copy.deepcopy(kwargs["tools"])}
        )
        return LLMResponse(content=self.report, tool_calls=[], raw={})


@pytest.fixture
def recording_clients(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingClient]:
    clients: list[_RecordingClient] = []

    def create_client(**kwargs: Any) -> _RecordingClient:
        client = _RecordingClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(session_mod, "_make_session_llm_client", create_client)
    monkeypatch.setattr(agent_loop, "create_session", session_mod.create_session)
    return clients


def _session(tmp_path: Path, profile: str, **kwargs: Any) -> session_mod.AgentSession:
    return session_mod.create_session(
        cfg=AppConfig(
            model="offline-delivery-model",
            base_url="https://network-disabled.invalid/v1",
            routing_mode="code_only",
            skills_enabled=False,
            prompt_guidance={"default": profile, "model_profiles": {}},
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="unused-test-key",
        session_log_dir_override=tmp_path / "sessions",
        subagents_enabled=True,
        subagent_registry=built_in_subagents(include_visual_designer=False),
        enable_compaction=False,
        verification_enabled=False,
        **kwargs,
    )


def _assert_workflow_delivered(request: dict[str, Any], profile: str) -> str:
    systems = [m["content"] for m in request["messages"] if m.get("role") == "system"]
    workflow = render_guidance("workflow", profile)
    # Comparing the whole real rendered block detects lost/truncated/replaced
    # delivery without freezing a second copy of the prose in the test.
    assert sum(system.count(workflow) for system in systems) == 1
    assert sum(system.count('<alysis_guidance section="workflow">') for system in systems) == 1
    system = next(system for system in systems if workflow in system)
    # Saved aliases intentionally share a family template. Require exactly one
    # selected block, and reject every genuinely different parent or child block.
    other_workflows = {render_guidance("workflow", other) for other in ALL_GUIDANCE_PROFILES} - {
        workflow
    }
    for other in other_workflows:
        assert all(other not in candidate for candidate in systems)
    return system


@pytest.mark.parametrize("profile", GUIDANCE_PROFILES)
@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
def test_parent_turn_delivers_workflow_without_rewriting_role_or_history(
    tmp_path: Path,
    recording_clients: list[_RecordingClient],
    profile: str,
    one_shot: bool,
) -> None:
    appendix = "Local role boundary: explain supplied evidence and preserve the existing setting."
    session = _session(
        tmp_path, profile, one_shot_execution=one_shot, trusted_system_prompt_append=appendix
    )
    history = [
        {"role": "user", "content": "Retain this reference: the previous setting is EUR."},
        {"role": "assistant", "content": "The supplied reference says EUR."},
    ]
    session.messages.extend(copy.deepcopy(history))
    bootstrap = copy.deepcopy(session.messages[0])
    try:
        session.run_turn("Explain only the supplied EUR setting; do not edit or run commands.")
        assert session.runtime_kind == (
            RuntimeKind.ONE_SHOT if one_shot else RuntimeKind.INTERACTIVE_CHAT
        )
        assert len(recording_clients) == 1
        request = recording_clients[0].requests[0]
        system = _assert_workflow_delivered(request, profile)
        delegation = render_guidance("delegation", profile)
        assert system.count(delegation) == 1
        assert "Work directly by default" in delegation
        assert "without an explicit user request" in delegation
        assert system.count(appendix) == 1
        positions = [request["messages"].index(message) for message in history]
        assert positions[1] == positions[0] + 1
        assert session.messages[0] == bootstrap
        assert session.startup_messages[0] == bootstrap
    finally:
        session.close()


# Each family has one child profile regardless of parent density. The family
# integration matrix covers both parent variants; keep every child request here.
@pytest.mark.parametrize("profile", [f"{family}-expanded" for family in PROMPT_FAMILIES])
def test_fresh_and_resumed_child_deliver_workflow_and_preserve_ownership(
    tmp_path: Path, recording_clients: list[_RecordingClient], profile: str
) -> None:
    parent = _session(tmp_path, profile)
    private = "Parent-only unassigned history: preserve the unrelated export format."
    parent.messages.append({"role": "user", "content": private})
    first_task = "Explain this supplied constant: currency='EUR'; no edits or commands."
    followup_task = (
        "Using the earlier supplied constant, explain its literal spelling; no commands."
    )
    parent_history = copy.deepcopy(parent.messages)
    try:
        first = parent.tools["subagent_run"].run(
            {"name": "general", "task": first_task, "mode": "readonly", "workspace_view": "shared"}
        )
        assert "error" not in first, first
        fresh = recording_clients[1]
        assert len(fresh.requests) == 1
        child_profile = subagent_profile(profile)
        assert child_profile.endswith("-subagent")
        fresh_system = _assert_workflow_delivered(fresh.requests[0], child_profile)
        assert '<alysis_guidance section="delegation">' not in fresh_system
        assert fresh_system.count(parent.subagent_registry["general"].system_prompt) == 1
        resumed = parent.tools["subagent_resume"].run(
            {"run_id": first["run_id"], "task": followup_task}
        )
        assert "error" not in resumed, resumed
        joined = parent.tools["subagent_wait"].run({"run_id": resumed["run_id"], "timeout_s": 10})
        assert not joined["pending_run_ids"], joined
        assert len(recording_clients) == 3
        continued = recording_clients[2]
        assert len(continued.requests) == 1
        resumed_request = continued.requests[0]
        assert _assert_workflow_delivered(resumed_request, child_profile) == fresh_system
        assert continued.model == fresh.model == parent.client.model
        # The current tool catalog belongs to the rebuilt startup context in
        # both runs. Moving it behind restored dialogue breaks a reusable
        # prefix even when the tools and permissions have not changed.
        fresh_catalog = next(
            message
            for message in fresh.requests[0]["messages"]
            if str(message.get("content", "")).startswith("<available_tool_catalog>")
        )
        assert resumed_request["messages"].count(fresh_catalog) == 1
        assert resumed_request["messages"].index(fresh_catalog) < resumed_request["messages"].index(
            {"role": "user", "content": first_task}
        )
        assert {"role": "user", "content": first_task} in resumed_request["messages"]
        assert {"role": "assistant", "content": fresh.report} in resumed_request["messages"]
        assert {"role": "user", "content": followup_task} in resumed_request["messages"]
        retained_positions = [
            resumed_request["messages"].index(message)
            for message in (
                {"role": "user", "content": first_task},
                {"role": "assistant", "content": fresh.report},
                {"role": "user", "content": followup_task},
            )
        ]
        assert retained_positions == sorted(retained_positions)
        for request in (fresh.requests[0], resumed_request):
            assert private not in str(request["messages"])
            names = {tool["function"]["name"] for tool in request["tools"]}
            assert "fs_read" in names
            assert names.isdisjoint({"fs_edit", "fs_write", "verify_run", "shell_run"})
            assert "Parent conversation is not inherited" in request["messages"][0]["content"]
        assert parent.messages == parent_history
    finally:
        parent.close()


def test_offline_guard_applies_before_http_and_in_python_subprocesses() -> None:
    with pytest.raises(RuntimeError, match=_NETWORK_DENIED):
        httpx.get("https://network-disabled.invalid")
    child = subprocess.run(
        [sys.executable, "-c", "import socket; socket.create_connection(('127.0.0.1', 9))"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert child.returncode != 0
    assert _NETWORK_DENIED in child.stderr
