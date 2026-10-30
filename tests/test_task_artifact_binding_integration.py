import json
from pathlib import Path

import pytest

from alysis_code.agent.acceptance_contract import AcceptanceCriterionKind, AcceptanceCriterionStatus
from alysis_code.agent.task_artifact_binding import TOOL_NAME
from alysis_code.agent_loop import ToolDef, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.surface import NoopSurface


@pytest.mark.parametrize("save", [True, False])
@pytest.mark.parametrize(
    "order",
    [
        "first",
        "late",
        "batched",
        "double-batched",
        "retry",
        "repeat",
        "context-first",
        "context-batched",
        "context-retry",
        "unknown-context",
        "list-first",
        "list-batched",
        "read-first",
        "write-batched",
        "batch-retry",
        "reverse-batch-retry",
        "double-batch-retry",
        "repeat-revised",
        "repeat-batched",
        "repeat-reordered",
        "repeat-nonpath",
        "repeat-metadata",
        "repeat-duplicate",
    ],
)
def test_actual_turn_binding_precedes_work_and_cannot_certify_missing_output(tmp_path, save, order):
    from test_agent_loop_one_shot_follow_through import _init_git_repo_with_commit

    root = tmp_path / "workspace"
    _init_git_repo_with_commit(root)
    folder = root / "records"
    folder.mkdir()
    (folder / "Planning notes.txt").write_text("original notes", encoding="utf-8")
    (folder / "Supplies.csv").write_text("original supplies", encoding="utf-8")
    output = folder / "Workshop bundle.zip"
    literal = r"sample\n"
    instruction = (
        f'Create "Workshop bundle.zip" inside "{folder}" containing Planning notes.txt '
        f'and Supplies.csv from that folder. Preserve both originals. Write "{literal}" in the output.'
    )
    entries = [
        {"name": name, "directory": directory, "role": role}
        for name, directory, role in [
            ("Workshop bundle.zip", str(folder), "required_output"),
            (str(folder), "", "location_context"),
            ("Planning notes.txt", str(folder), "preservation_target"),
            ("Supplies.csv", str(folder), "preservation_target"),
            (literal, "", "non_path"),
        ]
    ]
    session = create_session(
        cfg=AppConfig(
            model="test-model", stream=False, routing_mode="code_only", web_search_mode="off"
        ),
        root=root,
        mode="fullaccess",
        yes=False,
        max_steps=6,
        no_log=False,
        api_key_override="fixture-key",
        surface=NoopSurface(),
        enable_compaction=False,
        verification_enabled=True,
        session_log_dir_override=tmp_path / "sessions",
    )

    action_calls = []

    def action(_arguments):
        action_calls.append(True)
        if save:
            output.write_bytes(b"fixture output; contents are deliberately not validated")
        return {"content": [{"type": "text", "text": "Saved successfully"}]}

    tool = ToolDef(
        name="mcp__fixture__save",
        description="Fixture action",
        parameters={"type": "object", "properties": {}},
        run=action,
    )
    session.tools[tool.name] = tool
    context_tool = ToolDef(
        name="context_query",
        description="Read trusted context",
        parameters={"type": "object", "properties": {}},
        run=lambda _: {"context": "available"},
        metadata={"read_only_context": True}
        if order != "unknown-context"
        else {"read_only": True, "annotations": {"readOnlyHint": True}},
    )
    session.tools[context_tool.name] = context_tool
    assert session.tools["fs_list"].metadata["read_only_context"]
    assert session.tools["fs_read"].metadata["read_only_context"]
    assert not session.tools["fs_write"].metadata["read_only_context"]
    assert not session.tools["shell_run"].metadata["read_only_context"]
    assert not session.tools["verify_run"].metadata["read_only_context"]
    session.tool_list = [item.as_openai_tool() for item in session.tools.values()]

    class Client:
        model = "test-model"
        temperature = 0.2
        count = 0

        def chat(self, **kwargs):
            self.count += 1
            if self.count == 1:
                guidance = next(
                    m["content"]
                    for m in kwargs["messages"]
                    if isinstance(m.get("content"), str) and "Detected mentions: " in m["content"]
                )
                mentions = json.loads(guidance.split("Detected mentions: ", 1)[1])
                assert literal in mentions
                assert literal.replace("\\", "/") not in mentions
            if any(
                e["type"] == "task_artifact_interpretation" for e in session.store.events_snapshot()
            ):
                system = "\n".join(
                    m["content"]
                    for m in kwargs["messages"]
                    if m.get("role") == "system" and isinstance(m.get("content"), str)
                )
                assert "Detected mentions: " not in system
                assert "Submit task_artifacts alone" not in system
            names = {item["function"]["name"] for item in kwargs.get("tools", [])}
            assert TOOL_NAME in names
            binding = [(TOOL_NAME, {"paths": entries})]
            work = [(tool.name, {})]
            context = [(context_tool.name, {})]
            sequence = [binding, work]
            if order == "late":
                sequence.reverse()
            elif order == "batched":
                sequence = [binding + work]
            elif order == "double-batched":
                sequence = [binding + binding, work]
            elif order == "retry":
                sequence.insert(0, [(TOOL_NAME, {"paths": []})])
            elif order == "repeat":
                sequence.append(binding)
            elif order == "repeat-revised":
                changed = [{**entries[0], "role": "existing_input"}, *entries[1:]]
                sequence.append([(TOOL_NAME, {"paths": changed})])
            elif order == "repeat-batched":
                sequence.append(binding + work)
            elif order == "repeat-reordered":
                sequence.append([(TOOL_NAME, {"paths": list(reversed(entries))})])
            elif order == "repeat-nonpath":
                sequence.append(
                    [
                        (
                            TOOL_NAME,
                            {
                                "paths": [
                                    *entries,
                                    {
                                        "name": "originals",
                                        "directory": "",
                                        "role": "non_path",
                                    },
                                ]
                            },
                        )
                    ]
                )
            elif order == "repeat-metadata":
                sequence.append(
                    [
                        (
                            TOOL_NAME,
                            {
                                "paths": [
                                    {**entry, "evidence_quote": "Untrusted legacy excerpt"}
                                    for entry in entries
                                ]
                            },
                        )
                    ]
                )
            elif order == "repeat-duplicate":
                sequence.append([(TOOL_NAME, {"paths": [*entries, dict(entries[0])]})])
            elif order in {"context-first", "unknown-context"}:
                sequence.insert(0, context)
            elif order == "context-batched":
                sequence = [context + binding, work]
            elif order == "context-retry":
                invalid = {**entries[0], "evidence": instruction}
                sequence = [[(TOOL_NAME, {"paths": [invalid]})], context, binding, work]
            elif order == "list-first":
                sequence.insert(0, [("fs_list", {})])
            elif order == "list-batched":
                sequence = [context + binding + [("fs_list", {})], work]
            elif order == "read-first":
                sequence.insert(0, [("fs_read", {"path": "records/Planning notes.txt"})])
            elif order == "write-batched":
                sequence = [
                    binding + [("fs_write", {"path": "probe.txt", "content": "probe"})],
                    work,
                ]
            elif order in {"batch-retry", "reverse-batch-retry", "double-batch-retry"}:
                mixed = work + binding if order == "reverse-batch-retry" else binding + work
                sequence = ([mixed] * (2 if order == "double-batch-retry" else 1)) + [binding, work]
            if self.count <= len(sequence):
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id=f"{self.count}-{index}", name=name, arguments=arguments)
                        for index, (name, arguments) in enumerate(sequence[self.count - 1])
                    ],
                    raw={},
                )
            return LLMResponse(
                content="The application reported a save. Contents are unverified.",
                tool_calls=[],
                raw={},
            )

    session.client = Client()
    try:
        session.run_turn(instruction)
        events = session.store.events_snapshot()
        bindings = [event for event in events if event["type"] == "task_artifact_interpretation"]
        accepted = order in {
            "first",
            "retry",
            "repeat",
            "repeat-revised",
            "repeat-batched",
            "repeat-reordered",
            "repeat-nonpath",
            "repeat-metadata",
            "repeat-duplicate",
            "context-first",
            "context-batched",
            "context-retry",
            "list-first",
            "list-batched",
            "read-first",
            "batch-retry",
            "reverse-batch-retry",
            "double-batch-retry",
        }
        assert len(bindings) == int(accepted)
        contract = session._turn_execution_state.acceptance_contract
        if accepted:
            outputs = [
                item
                for item in contract.criteria
                if item.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
            ]
            assert len(outputs) == 1
            assert outputs[0].path_refs[0].absolute_path == output.as_posix()
            assert (outputs[0].status == AcceptanceCriterionStatus.PASSED) == save
            metadata = [
                item for item in contract.evidence if item.category == "artifact_file_metadata"
            ]
            assert metadata
            assert all("contents not verified" in item.summary for item in metadata)
        else:
            assert any(ref.raw_text == "notes.txt" for ref in contract.path_refs)
        assert (folder / "Planning notes.txt").read_text(encoding="utf-8") == "original notes"
        assert (folder / "Supplies.csv").read_text(encoding="utf-8") == "original supplies"
        results = [
            e["payload"]["result"]
            for e in events
            if e["type"] == "tool_result" and e["payload"]["name"] == TOOL_NAME
        ]
        if order in {
            "repeat",
            "repeat-batched",
            "repeat-reordered",
            "repeat-nonpath",
            "repeat-metadata",
            "repeat-duplicate",
        }:
            assert len(results) == 2 and results[0] == results[1]
            assert not results[1]["completion_verified"] and "error" not in results[1]
        if order == "repeat-revised":
            assert len(results) == 2 and results[1]["error"] == "requirements_already_in_use"
        if order in {"batch-retry", "reverse-batch-retry", "double-batch-retry"}:
            assert len(action_calls) == 1  # only the isolated post-binding call ran
            assert any(
                e["type"] == "controller_intervention"
                and e["payload"].get("class") == "task_artifact_serialization"
                for e in events
            )
        if order == "batched":
            assert not action_calls and not output.exists()
        if order == "write-batched":
            assert not (root / "probe.txt").exists()
        if order == "context-retry":
            errors = [
                e["payload"]["result"]
                for e in events
                if e["type"] == "tool_result"
                and e["payload"]["name"] == TOOL_NAME
                and "error" in e["payload"]["result"]
            ]
            assert len(errors) == 1
            assert (
                "Additional properties" in errors[0]["message"]
                and "evidence" in errors[0]["message"]
            )
            assert errors[0]["retryable"] and errors[0]["expected_schema"]["properties"]["paths"]
    finally:
        session.close()


@pytest.mark.parametrize(
    "source", ["user_message", "assistant_message", "tool_result", "compaction"]
)
def test_followup_binding_uses_durable_user_text_without_enrolling_external_access(
    tmp_path, source
):
    from test_agent_loop_one_shot_follow_through import _init_git_repo_with_commit

    root = tmp_path / "workspace"
    _init_git_repo_with_commit(root)
    key = str(tmp_path / "external" / "資料-κλειδί")
    session = create_session(
        cfg=AppConfig(
            model="test-model", stream=False, routing_mode="code_only", web_search_mode="off"
        ),
        root=root,
        mode="fullaccess",
        yes=False,
        max_steps=2,
        no_log=False,
        api_key_override="fixture-key",
        surface=NoopSurface(),
        enable_compaction=False,
        verification_enabled=True,
        session_log_dir_override=tmp_path / "sessions",
    )
    # There is no retained model history. Only the durable user event can ground
    # the exact name; a tool/assistant/compaction event with the same text cannot.
    session.store.append(source, {"content": f'Create "{key}" as valid JSON.'})
    access_checks = []

    def deny_access(path, initial):
        access_checks.append(str(path))
        return False

    session.artifact_output_access = deny_access

    class Client:
        model = "test-model"
        temperature = 0.2
        count = 0

        def chat(self, **kwargs):
            self.count += 1
            if self.count == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="followup-binding",
                            name=TOOL_NAME,
                            arguments={
                                "paths": [
                                    {"name": key, "directory": "", "role": "existing_input"},
                                    {
                                        "name": "result.txt",
                                        "directory": "",
                                        "role": "required_output",
                                    },
                                ]
                            },
                        )
                    ],
                    raw={},
                )
            return LLMResponse(
                content="No file actions performed; outputs remain unverified.",
                tool_calls=[],
                raw={},
            )

    session.client = Client()
    try:
        session.run_turn("Use the same input to create result.txt. Do not modify the input.")
        events = session.store.events_snapshot()
        results = [
            e["payload"]["result"]
            for e in events
            if e["type"] == "tool_result" and e["payload"].get("name") == TOOL_NAME
        ]
        assert len(results) == 1
        if source == "user_message":
            assert "error" not in results[0]
            assert not results[0]["completion_verified"]
            contract = session._turn_execution_state.acceptance_contract
            assert contract.allowed_output_paths == {"result.txt"}
            assert Path(key) in {Path(ref.absolute_path) for ref in contract.path_refs}
            assert not any(
                c.kind == AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA for c in contract.criteria
            )
            # Merely remembering an input must not enroll it or inspect it.
            assert Path(key) not in {Path(path) for path in access_checks}
            assert Path(key) not in {
                Path(path) for path in contract.artifact_output_observer.baseline
            }
        else:
            assert results[0]["error"] == "invalid_task_artifacts"
        assert not (root / "result.txt").exists()
        assert not (tmp_path / "external").exists()
    finally:
        session.close()
