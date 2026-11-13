from pathlib import Path

import pytest

from alysis_code import agent_loop
from alysis_code.config import AppConfig
from alysis_code.execution_shared import build_task_acceptance_instruction
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


def test_task_requirements_exclude_generated_plan_metadata() -> None:
    requirements = build_task_acceptance_instruction(
        {
            "title": "Write the result",
            "description": "Create `answer.txt`.",
            "acceptance_criteria": ["The result must contain 42."],
            "estimated_files": ["unrelated.txt"],
            "write_scope": ["src/**"],
            "branch": "feature/helper",
        }
    )
    assert requirements == "Write the result\n\nCreate `answer.txt`.\n\nThe result must contain 42."


def test_managed_completion_uses_task_requirements_and_keeps_model_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_create_session = agent_loop.create_session
    observed_messages: list[str] = []
    context = "Create `answer.txt`.\n\nSetup guidance: Do not create setup.cfg/setup.py."

    class Client:
        model = "test-model"
        temperature = 0.0
        calls = 0

        def chat(self, *, messages, **_kwargs):
            observed_messages.extend(str(message.get("content", "")) for message in messages)
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="write-result",
                            name="fs_write",
                            arguments={"path": "answer.txt", "content": "42\n"},
                        )
                    ],
                    raw={},
                )
            return LLMResponse(content="Created answer.txt with 42.", tool_calls=[], raw={})

    def create_session(**kwargs):
        session = original_create_session(**kwargs)
        session.client = Client()
        return session

    monkeypatch.setattr(agent_loop, "create_session", create_session)
    result = agent_loop.run_agent(
        cfg=AppConfig(model="test-model", routing_mode="code_only", web_search_mode="off"),
        root=tmp_path,
        instruction=context,
        acceptance_instruction="Create `answer.txt`.",
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=True,
        verification_enabled=False,
        subagents_enabled=False,
        session_log_dir_override=tmp_path / "sessions",
        session_id_override="managed-task",
    )
    assert result == 0
    assert (tmp_path / "answer.txt").read_text() == "42\n"
    assert context in observed_messages
    events = list(read_session_events(tmp_path / "sessions" / "managed-task.jsonl"))
    contract = next(event["payload"] for event in events if event["type"] == "acceptance_contract")
    # The contract reads the task's requirements, not the setup guidance around
    # them; a mention is a reference, never a requirement by its wording.
    mentioned = {path for criterion in contract["criteria"] for path in criterion["paths"]}
    assert mentioned == {"answer.txt"}
    assert not [
        criterion
        for criterion in contract["criteria"]
        if criterion["kind"] == "required_artifact_path"
    ]
