"""Check an installed wheel from a separate disposable consumer directory.

Run with isolated Python: python -I /path/to/this_script.py /path/to/install_target.
The current directory receives two small fixture files and must be disposable.
Dependencies must already be available to the selected interpreter.
"""

import json
import subprocess
import sys
from pathlib import Path

installed = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(installed))
from alysis_code.agent.task_state import SessionTaskState  # noqa: E402

import alysis_code  # noqa: E402 -- deliberately import only after choosing the wheel
from alysis_code.agent.acceptance_contract import (  # noqa: E402
    AcceptanceCriterionKind,
    AcceptanceCriterionStatus,
    build_acceptance_contract,
)
from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect  # noqa: E402

assert Path(alysis_code.__file__).resolve().is_relative_to(installed)
root = Path.cwd()
assert not (root / "src" / "alysis_code").exists()
typed = build_acceptance_contract(
    root=root,
    instruction="Implement `src/dispatcher.py` and run no more than `concurrency_limit` jobs.",
)
assert typed.path_refs and not any(c.commands for c in typed.criteria)
assert any(c.kind == AcceptanceCriterionKind.PUBLIC_SYMBOL_INTERFACE for c in typed.criteria)

(root / "output.txt").write_text("correct", encoding="utf-8")
(root / "check_output.py").write_text(
    'from pathlib import Path\nassert Path("output.txt").read_text() == "correct"\n',
    encoding="utf-8",
)
command = '"' + sys.executable.replace("\\", "/") + '" check_output.py'
task = SessionTaskState(
    task_id="wheel:task:1",
    session_id="wheel",
    sequence=1,
    objective=f"Create output.txt. Run `{command}`.",
)
contract = build_acceptance_contract(root=root, instruction="continue", task_state=task)
state = TurnExecutionState(
    execution_requested=True,
    expected_verification_commands={command},
    acceptance_contract=contract,
)
criterion = next(c for c in contract.criteria if c.commands)


def check():
    result = subprocess.run(
        [sys.executable, "check_output.py"], cwd=root, capture_output=True, text=True
    )
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="shell_run",
        arguments={"cmd": command},
        status="ok",
        result={
            "cmd": command,
            "effective_cmd": command,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "cwd": str(root),
        },
        known_verification_commands=[command],
        verification_authoritative=True,
    )
    return result.returncode


assert check() == 0
assert criterion.status == AcceptanceCriterionStatus.PASSED
(root / "output.txt").write_text("wrong", encoding="utf-8")
_record_tool_effect(
    root=root,
    state=state,
    tool_name="fs_write",
    arguments={"path": "output.txt"},
    status="ok",
    result={"path": "output.txt"},
    known_verification_commands=[command],
)
assert state.verification_relevant_edit_generation == 1
assert criterion.status != AcceptanceCriterionStatus.PASSED
assert not state.covered_verification_commands
assert check() != 0
assert criterion.status == AcceptanceCriterionStatus.FAILED
(root / "output.txt").write_text("correct", encoding="utf-8")
_record_tool_effect(
    root=root,
    state=state,
    tool_name="fs_write",
    arguments={"path": "output.txt"},
    status="ok",
    result={"path": "output.txt"},
    known_verification_commands=[command],
)
assert check() == 0
assert criterion.status == AcceptanceCriterionStatus.PASSED
assert contract.evidence[-1].generation == 2
assert contract.evidence[-1].task_id == task.task_id
assert contract.evidence[-1].result_id
print(
    json.dumps(
        {
            "passed": True,
            "module": alysis_code.__file__,
            "task_id": task.task_id,
            "generation": contract.generation,
            "cwd": str(root),
        },
        indent=2,
    )
)
