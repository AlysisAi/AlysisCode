"""Policy refusals retain local provenance through dispatch and the session log."""

import io
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.agent.errors import AgentRuntimeError, ApprovalReviewDeniedError
from alysis_code.agent_loop import ToolDef, build_tools, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.mcp.manager import McpHostToolBinding
from alysis_code.session_store import SessionStore, read_session_events
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.tools.errors import ToolError


class _Client:
    model = "test-model"
    temperature = 0.2

    def __init__(self, calls):
        self.responses = iter(
            [
                LLMResponse(content="", tool_calls=calls, raw={}),
                LLMResponse(content="The attempts finished.", tool_calls=[], raw={}),
            ]
        )

    def chat(self, **kwargs):
        return next(self.responses)


def _session(tmp_path: Path, mode="auto"):
    return create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode=mode,
        yes=False,
        max_steps=5,
        no_log=False,
        api_key_override="fixture-key",
        session_log_dir_override=tmp_path / "sessions",
        session_id_override="guardrail-metadata",
        surface=NoopSurface(),
    )


@pytest.mark.parametrize("message", ["Η εγγραφή δεν επιτρέπεται.", "不允许写入。"])
def test_log_uses_local_denial_provenance_and_resets_it_for_the_next_tool(tmp_path, message):
    session = _session(tmp_path)

    def run(args):
        if args["attempt"] == 0:
            raise AgentRuntimeError(message, denial_code="readonly_denied")
        if args["attempt"] == 1:
            return {
                "error": "Document conversion declined: unsupported format",
                "error_code": "readonly_denied",
                "guardrail_denial": {"error_code": "readonly_denied"},
            }
        raise AgentRuntimeError("Blocked by an upstream service")

    session.tools = {
        "probe": ToolDef(
            name="probe",
            description="Attempt a fixture operation",
            parameters={"type": "object", "properties": {"attempt": {"type": "integer"}}},
            run=run,
        )
    }
    session.client = _Client(
        [ToolCall(id=f"c{i}", name="probe", arguments={"attempt": i}) for i in range(3)]
    )
    try:
        session.run_turn("Attempt the fixture operations and report their results.")
    finally:
        session.close()
    results = [
        event["payload"]
        for event in read_session_events(tmp_path / "sessions/guardrail-metadata.jsonl")
        if event["type"] == "tool_result"
    ]
    assert len(results) == 3
    assert results[0]["guardrail_denial"] == {"error_code": "readonly_denied"}
    assert results[0]["result"]["error"] == message
    assert results[0]["result"]["error_code"] == "readonly_denied"
    assert "guardrail_denial" not in results[1]
    assert "guardrail_denial" not in results[2]


@pytest.mark.parametrize(
    "tool_name,arguments,mode,code",
    [
        ("fs_write", {"path": "ordinary.txt", "content": "x"}, "review", "approval_required"),
        ("fs_write", {"path": ".git/config", "content": "x"}, "auto", "protected_path_denied"),
        ("fs_write", {"path": "../outside.txt", "content": "x"}, "auto", "path_escapes_workspace"),
    ],
)
def test_real_filesystem_guards_emit_codes_without_writing(
    tmp_path, tool_name, arguments, mode, code
):
    root = tmp_path / "workspace"
    root.mkdir()
    store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path / "sessions",
        session_id="guard-probe",
        cwd=str(root),
        repo_root=str(root),
    )
    tools = build_tools(
        root=root,
        console=Console(file=io.StringIO()),
        surface=NoopSurface(),
        store=store,
        mode=mode,
        yes=False,
        non_interactive=True,
    )
    with pytest.raises(ToolError) as failure:
        tools[tool_name].run(arguments)
    assert failure.value.denial_code == code
    assert failure.value.result_payload["error_code"] == code
    assert not (root / arguments["path"]).exists()


def test_mcp_readonly_guard_never_dispatches_and_reviewer_denials_keep_their_code():
    dispatched = []
    binding = McpHostToolBinding(
        tool_name="write",
        tool_alias="mcp_fixture_write",
        description="Fixture",
        parameters={},
        run_handler=lambda args: dispatched.append(args),
        session_mode="readonly",
    )
    with pytest.raises(ToolError) as failure:
        binding.run({})
    assert failure.value.denial_code == "readonly_denied"
    assert not dispatched
    review = ApprovalReviewDeniedError("fs_write", reason="Δεν εγκρίνεται")
    assert review.denial_code == "approval_review_denied"
    assert review.result_payload["dispatch_state"] == "not_dispatched"
