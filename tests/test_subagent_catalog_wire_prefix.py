"""Exercise real child bootstrap/resume through offline provider serializers."""

from __future__ import annotations

import copy
import json
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_subagents import _build_main_tools
from test_wire_request_diagnostics import _capturing_client

from alysis_code import agent_loop
from alysis_code.agent import session as session_mod
from alysis_code.config import AppConfig
from alysis_code.provider_telemetry import reset_provider_telemetry_for_tests
from alysis_code.subagents import SubagentDefinition


@pytest.fixture(autouse=True)
def _offline_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def denied(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Child wire tests must use MockTransport, never the network.")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    reset_provider_telemetry_for_tests()
    yield
    reset_provider_telemetry_for_tests()


@contextmanager
def _children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> Iterator[tuple[Any, dict[str, SubagentDefinition], list[Any], list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []
    children: list[session_mod.AgentSession] = []
    registry = {
        "reader": SubagentDefinition(
            name="reader",
            description="Inspect source.",
            system_prompt="Inspect the assigned source without editing.",
            mode="readonly",
            allow_tools=("fs_read", "fs_list"),
            allow_workspace_writes=False,
        )
    }

    def offline_client(**_kwargs: Any) -> Any:
        return _capturing_client(protocol, requests)

    def create_child(**kwargs: Any) -> session_mod.AgentSession:
        kwargs.update(
            api_key_override="offline-test-key",
            session_log_dir_override=tmp_path / "sessions",
            enable_compaction=False,
            verification_enabled=False,
        )
        child = session_mod.create_session(**kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(session_mod, "_make_session_llm_client", offline_client)
    # The scheduler supports this legacy indirection as well as an explicit
    # factory. Bind it before constructing tools so neither path can escape.
    monkeypatch.setattr(agent_loop, "create_session", create_child)
    tools = _build_main_tools(
        tmp_path=tmp_path,
        cfg=AppConfig(model="test-model", stream=False, skills_enabled=False),
        skills_enabled=False,
        subagents_enabled=True,
        non_interactive=True,
        subagent_registry=registry,
    )
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    try:
        yield tools, registry, children, requests
    finally:
        scheduler.shutdown(cancel_pending=True)
        for child in children:
            child.close()


def _resume(tools: Any, run_id: str, task: str) -> dict[str, Any]:
    resumed = tools["subagent_resume"].run({"run_id": run_id, "task": task})
    assert "error" not in resumed
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    child = scheduler._children[resumed["run_id"]]
    child.completion.result(timeout=10)
    child.worker_bookkeeping_completion.result(timeout=10)
    result = tools["subagent_wait"].run({"run_id": resumed["run_id"]})
    return result["results"][resumed["run_id"]]


@pytest.mark.parametrize("protocol", ["responses", "compat", "anthropic"])
def test_real_child_catalog_keeps_fresh_prefix_through_two_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    with _children(tmp_path, monkeypatch, protocol) as (tools, _registry, children, requests):
        first = tools["subagent_run"].run({"name": "reader", "task": "Inspect first source."})
        assert first["status"] == "success"
        second = _resume(tools, first["run_id"], "Review the original finding.")
        assert second["status"] == "success"
        third = _resume(tools, second["run_id"], "Summarize the retained finding.")
        assert third["status"] == "success"

        assert len(children) == len(requests) == 3
        assert len({child.store.session_id for child in children}) == 3
        assert len({child.provider_session_id for child in children}) == 1
        history_key = "input" if protocol == "responses" else "messages"
        for previous, current in zip(requests, requests[1:], strict=False):
            # Keep earlier continuation notices and tasks, not only the fresh
            # bootstrap. Dropping a previous resume notice breaks this prefix.
            assert current[history_key][: len(previous[history_key])] == previous[history_key]
            assert current.get("instructions") == previous.get("instructions")
            assert current.get("system") == previous.get("system")
            assert current["tools"] == previous["tools"]
        first_history = requests[0][history_key]
        # The actual turn loop adds request-local context after its persistent
        # messages. Compare the bootstrap and first task, not those suffixes.
        task_index = next(
            index
            for index, item in enumerate(first_history)
            if "Inspect first source." in json.dumps(item)
        )
        first_prefix = first_history[: task_index + 1]
        for child, request in zip(children, requests, strict=True):
            assert request[history_key][: len(first_prefix)] == first_prefix
            catalog = [
                message
                for message in child.messages
                if "<available_tool_catalog>" in str(message.get("content", ""))
            ]
            assert len(catalog) == 1
            assert child.messages.index(catalog[0]) < next(
                index
                for index, message in enumerate(child.messages)
                if message.get("content") == "Inspect first source."
            )
            assert set(child.tools) == {"fs_read", "fs_list"}
        for request in requests[1:]:
            if protocol == "responses":
                assert request["instructions"] == requests[0]["instructions"]
            elif protocol == "anthropic":
                assert request["system"] == requests[0]["system"]
        retained = [message.get("content") for message in children[-1].messages]
        assert retained.count("Inspect first source.") == 1
        assert retained.count("Review the original finding.") == 1
        assert retained.count("Summarize the retained finding.") == 1


@pytest.mark.parametrize("protocol", ["responses", "compat", "anthropic"])
def test_resume_refreshes_tool_scope_and_preserves_history_after_permission_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    with _children(tmp_path, monkeypatch, protocol) as (tools, registry, children, requests):
        first = tools["subagent_run"].run({"name": "reader", "task": "Inspect retained source."})
        assert first["status"] == "success"
        initial_wire = copy.deepcopy(requests)
        registry["reader"] = replace(
            registry["reader"], allow_tools=("fs_list", "unavailable_tool"), deny_tools=("fs_read",)
        )
        failed = _resume(tools, first["run_id"], "This attempt has invalid permissions.")
        assert failed["error_code"] == "subagent_allowlist_unavailable"
        assert requests == initial_wire
        registry["reader"] = replace(registry["reader"], allow_tools=("fs_list",))
        recovered = _resume(tools, failed["run_id"], "Continue with the allowed listing tool.")
        assert recovered["status"] == "success"
        assert len(requests) == 2
        child = children[-1]
        assert set(child.tools) == {"fs_list"}
        assert child.workspace_writes_allowed is False
        catalog = next(
            message["content"]
            for message in child.messages
            if "<available_tool_catalog>" in str(message.get("content", ""))
        )
        assert "fs_list" in catalog
        assert "fs_read" not in catalog
        assert "unavailable_tool" not in catalog
        tool_names = {
            tool.get("name") or tool.get("function", {}).get("name")
            for entry in requests[-1]["tools"]
            for tool in (entry["tools"] if entry.get("type") == "namespace" else [entry])
        }
        assert tool_names == {"fs_list"}
        retained = [message.get("content") for message in child.messages]
        assert retained.count("Inspect retained source.") == 1
        assert "This attempt has invalid permissions." not in retained
