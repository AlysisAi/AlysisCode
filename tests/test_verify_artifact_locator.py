from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from alysis_code import agent_loop
from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import SessionStore
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult

pytestmark = pytest.mark.usefixtures("approved_local_checks")


def _tools(
    tmp_path: Path,
    *,
    runtime_kind: RuntimeKind = RuntimeKind.ONE_SHOT,
    reader_available: bool = True,
) -> tuple[dict[str, ToolDef], SessionStore, str]:
    root = tmp_path / "workspace"
    root.mkdir()
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_details.py").write_text(
        "import unittest\n"
        "class Details(unittest.TestCase):\n"
        "    def test_report(self):\n"
        "        print('BEGIN_DETAILS:' + 'x' * 1200 + ':END_DETAILS')\n"
        "        self.assertEqual(2 + 2, 4)\n",
        encoding="utf-8",
    )
    executable = (
        subprocess.list2cmdline([Path(sys.executable).as_posix()])
        if os.name == "nt"
        else shlex.quote(sys.executable)
    )
    command = f"{executable} -m unittest discover -s tests -v"
    cfg = AppConfig(model="test-model", verify_commands=[command])
    cfg.extra_fields["verify_sandbox"] = {"mode": "off"}
    store = SessionStore(
        enabled=reader_available,
        sessions_dir=tmp_path / "sessions",
        session_id="verify-locator",
        cwd=str(root),
        repo_root=str(root),
    )
    tools = agent_loop.build_tools(
        root=root,
        console=None,
        store=store,
        mode="fullaccess",
        yes=True,
        cfg=cfg,
        non_interactive=True,
        skills_enabled=False,
        runtime_kind=runtime_kind,
        authoritative_verification_commands=[command],
    )
    return tools, store, command


@pytest.mark.parametrize("runtime_kind", [RuntimeKind.ONE_SHOT, RuntimeKind.INTERACTIVE_CHAT])
def test_truncated_verify_output_is_readable_through_current_session_tool(
    tmp_path: Path,
    runtime_kind: RuntimeKind,
) -> None:
    tools, store, _command = _tools(tmp_path, runtime_kind=runtime_kind)
    try:
        result = tools["verify_run"].run({})
        command_result = result["command_results"][0]
        assert result["all_passed"] is True
        assert command_result["real_execution"] is True
        assert command_result["output_truncated"] is True
        assert ":END_DETAILS" not in command_result["output_preview"]
        assert result["artifact_saved"] is True
        assert result["artifact_path"] is None
        assert result["artifact_readable_via_fs"] is False
        assert result["artifact_location"] == "external_session_store"
        assert str(store.session_artifact_root) not in json.dumps(result)

        locator = result["artifact_locator"]
        assert locator == "session_artifacts/verify/step001_verify_run.txt"
        assert "session_artifact_read" in result["full_output"]
        assert locator in result["full_output"]
        schema = tools["session_artifact_read"].as_openai_tool()["function"]["parameters"]
        assert schema["required"] == []
        assert {"locator", "handle", "list_handles"} <= schema["properties"].keys()
        read = tools["session_artifact_read"].run({"locator": locator})
        assert read["locator"] == locator
        assert read["truncated"] is False
        assert "BEGIN_DETAILS:" + "x" * 1200 + ":END_DETAILS" in read["content"]
        assert "Ran 1 test" in read["content"]
    finally:
        store.close()


@pytest.mark.parametrize("outside_session", [False, True])
def test_verify_does_not_advertise_missing_or_foreign_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outside_session: bool,
) -> None:
    tools, store, command = _tools(tmp_path)

    def verify(**kwargs: Any) -> VerifyRunResult:
        artifact = kwargs["artifact_path"]
        if outside_session:
            artifact = tmp_path / "different-session.txt"
            artifact.write_text("not owned by this session", encoding="utf-8")
        return VerifyRunResult(
            commands=[command],
            command_results=[VerifyCommandResult(command, 0, "ok", real_execution=True)],
            artifact_path=artifact,
        )

    monkeypatch.setattr(agent_loop, "run_task_verification", verify)
    try:
        result = tools["verify_run"].run({})
        assert result["artifact_saved"] is outside_session
        assert result["artifact_path"] is None
        assert "artifact_locator" not in result
        assert "full_output" not in result
    finally:
        store.close()


def test_failed_verify_artifact_save_does_not_return_a_locator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, store, _command = _tools(tmp_path)
    artifact = store.runtime_artifact_path("verify", "step001_verify_run.txt")
    original_write = Path.write_text

    def write(path: Path, *args: Any, **kwargs: Any) -> int:
        if path == artifact:
            raise OSError("simulated artifact save failure")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    try:
        with pytest.raises(OSError, match="simulated artifact save failure"):
            tools["verify_run"].run({})
        assert not artifact.exists()
    finally:
        store.close()


def test_verify_does_not_advertise_unavailable_artifact_reader(tmp_path: Path) -> None:
    tools, store, _command = _tools(tmp_path, reader_available=False)
    try:
        assert "session_artifact_read" not in tools
        result = tools["verify_run"].run({})
        assert result["all_passed"] is True
        assert result["artifact_saved"] is True
        assert "artifact_locator" not in result
        assert "full_output" not in result
    finally:
        store.close()


class _VerifyThenReadClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0
        self.visible_verify: dict[str, Any] | None = None
        self.visible_read: dict[str, Any] | None = None

    def chat(self, *, messages: list[dict[str, Any]], **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="", tool_calls=[ToolCall("verify", "verify_run", {})], raw={}
            )
        latest = next(message for message in reversed(messages) if message.get("role") == "tool")
        payload = json.loads(latest["content"])
        if self.calls == 2:
            self.visible_verify = payload
            locator = payload["artifact_locator"]
            assert locator in payload["full_output"]
            return LLMResponse(
                content="",
                tool_calls=[ToolCall("read", "session_artifact_read", {"locator": locator})],
                raw={},
            )
        assert self.calls == 3
        self.visible_read = payload
        return LLMResponse(
            content="The required unittest check passed. I read its full saved output: one test passed.",
            tool_calls=[],
            raw={},
        )


@pytest.mark.parametrize("runtime_kind", [RuntimeKind.ONE_SHOT, RuntimeKind.INTERACTIVE_CHAT])
def test_verify_locator_survives_the_model_visible_turn_projection(
    tmp_path: Path,
    runtime_kind: RuntimeKind,
) -> None:
    _built_tools, fixture_store, command = _tools(tmp_path)
    fixture_store.close()
    cfg = AppConfig(
        model="test-model",
        routing_mode="code_only",
        stream=False,
        skills_enabled=False,
        verify_commands=[command],
    )
    cfg.extra_fields["verify_sandbox"] = {"mode": "off"}
    session = agent_loop.create_session(
        cfg=cfg,
        root=tmp_path / "workspace",
        mode="auto",
        yes=True,
        max_steps=5,
        no_log=False,
        api_key_override="offline-test-placeholder",
        runtime_kind=runtime_kind,
        one_shot_execution=runtime_kind == RuntimeKind.ONE_SHOT,
        session_log_dir_override=tmp_path / "runtime-sessions",
        session_id_override="visible-verify-locator",
        enable_compaction=False,
        enable_tool_output_offload=False,
        verify_cmd=[command],
    )
    client = _VerifyThenReadClient()
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Run the configured tests and read their full saved output.") == 0
        assert client.calls == 3
        assert client.visible_verify is not None
        assert client.visible_verify["artifact_path"] is None
        assert client.visible_verify["command_results"][0]["output_truncated"] is True
        assert client.visible_read is not None
        assert client.visible_read["locator"] == client.visible_verify["artifact_locator"]
        assert "BEGIN_DETAILS:" + "x" * 1200 + ":END_DETAILS" in client.visible_read["content"]
    finally:
        session.close()
