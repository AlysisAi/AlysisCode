from __future__ import annotations

from pathlib import Path

import pytest
from test_greenfield_verification_bootstrap import (
    MODULE_SOURCE,
    _events,
    _ScriptedClient,
    _session,
    _write_tool,
)

from alysis_code.agent.verification import TurnExecutionState, uncovered_new_agent_modules
from alysis_code.llm.openai_compat import LLMResponse, ToolCall


def test_deleted_module_is_omitted_but_recreation_requires_coverage(tmp_path: Path) -> None:
    state = TurnExecutionState(
        execution_requested=True, agent_created_paths={"temporary.py", "retained.py"}
    )
    temporary = tmp_path / "temporary.py"
    retained = tmp_path / "retained.py"
    temporary.write_text(MODULE_SOURCE)
    retained.write_text(MODULE_SOURCE)
    assert uncovered_new_agent_modules(tmp_path, state) == ["retained.py", "temporary.py"]

    temporary.unlink()
    assert uncovered_new_agent_modules(tmp_path, state) == ["retained.py"]
    assert state.agent_created_paths == {"temporary.py", "retained.py"}

    temporary.write_text("def multiply(a, b):\n    return a * b\n")
    assert uncovered_new_agent_modules(tmp_path, state) == ["retained.py", "temporary.py"]

    (tmp_path / "test_temporary.py").write_text("from temporary import multiply\n")
    assert uncovered_new_agent_modules(tmp_path, state) == ["retained.py"]


@pytest.mark.parametrize("recreate", [False, True])
def test_final_coverage_marker_uses_files_remaining_after_actual_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recreate: bool
) -> None:
    monkeypatch.setenv("ALYSIS_ZERO_COVERAGE_ADVISORY", "on")
    session = _session(tmp_path)
    responses = [
        LLMResponse(
            content="Creating the requested files.",
            tool_calls=[
                _write_tool("create-temporary", "temporary.py", MODULE_SOURCE),
                _write_tool("create-retained", "retained.py", MODULE_SOURCE),
            ],
            raw={},
        ),
        LLMResponse(
            content="Removing the temporary file.",
            tool_calls=[
                ToolCall(id="delete", name="fs_delete", arguments={"path": "temporary.py"})
            ],
            raw={},
        ),
    ]
    if recreate:
        responses.append(
            LLMResponse(
                content="Recreating the second requested module.",
                tool_calls=[_write_tool("recreate", "temporary.py", MODULE_SOURCE)],
                raw={},
            )
        )
    report = "Created retained.py; no tests were added."
    if recreate:
        report += " Recreated temporary.py."
    else:
        report += " Removed temporary.py."
    responses.extend([LLMResponse(content=report, tool_calls=[], raw={})] * 2)
    session.client = _ScriptedClient(responses)
    try:
        instruction = (
            "Create retained.py and temporary.py with add functions, then remove temporary.py."
        )
        if recreate:
            instruction += " Finally recreate temporary.py."
        assert session.run_turn(instruction) == 0
        log_path = session.store.path
    finally:
        session.close()

    assert (tmp_path / "temporary.py").is_file() is recreate
    assert (tmp_path / "retained.py").is_file()
    deletes = [item for item in _events(log_path, "tool_result") if item.get("name") == "fs_delete"]
    assert len(deletes) == 1 and deletes[0]["result"]["deleted"] is True
    modules = ["retained.py", "temporary.py"] if recreate else ["retained.py"]
    markers = _events(log_path, "zero_coverage_modules")
    assert markers and markers[-1]["modules"] == modules
    final = _events(log_path, "final")[-1]["content"]
    assert f"New modules without detected test references: {', '.join(modules)} —" in final
    assert "bounded text scan" in final
    assert "does not measure executed coverage" in final
