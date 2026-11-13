"""Facts the host states once before a turn ends, and requirements the model declares.

The host reads no requirement from the wording of a request. Two general
mechanisms take the place of the English phrase patterns that used to:

* facts that are the same in every language (a command the user wrote was not
  run or last failed, a file the turn created does not parse, a path the user
  wrote is missing) are stated to the model once, and the model decides;
* the model may declare what the request requires (a command that must pass,
  an output's format, and so on), and the host then holds the turn to it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.finish_facts import (
    FinishFacts,
    collect_finish_facts,
    command_was_run_as,
)
from alysis_code.agent.task_artifact_binding import TOOL_NAME
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.surface import ApprovalDecision, NoopSurface

PYTHON = '"' + sys.executable.replace("\\", "/") + '"'


# --- the facts ----------------------------------------------------------------


@pytest.mark.parametrize(
    "written,observed,expected",
    [
        ("pytest -q", "pytest -q", True),
        ("pytest -q", ".venv/bin/pytest -q tests/test_calc.py", True),
        ("pytest -q", "cd app && pytest -q", True),
        ("python check.py", f"{PYTHON} check.py", True),
        ("pytest -q", "python -m pytest -q", True),
        ("pytest -q", "pytest -q; echo $?", True),
        ("pytest -q", "(cd app && pytest -q)", True),
        ("pytest -q", 'bash -lc "pytest -q"', True),
        ("python main.py", "python3 main.py", True),
        ("FOO=1 make test", "make test", True),
        # The written words must stand together in one command of what ran.
        ("npm test", "npm run test -- --watch=false", False),
        ("npm test", "npm install && ls test", False),
        ("python main.py", "python -m py_compile main.py", False),
        ("npm test", "npm run build", False),
        ("pytest -q", "pytest tests/", False),
        ("make test", "make", False),
    ],
)
def test_a_written_command_is_found_inside_what_ran(written, observed, expected) -> None:
    assert command_was_run_as(written, observed) is expected


def test_facts_about_commands(tmp_path: Path) -> None:
    facts = collect_finish_facts(
        root=tmp_path,
        request_commands=["pytest -q", "python check.py", "make lint", "npm test"],
        required_commands=["npm test"],  # declared: the completion gate enforces it
        observed_commands=[
            ("pytest -q", False, 1),
            ("pytest -q tests/", True, 0),  # the later run decides
            (f"{PYTHON} check.py", False, 3),
        ],
    )
    assert facts.commands_not_run == ("make lint",)
    assert facts.commands_failed == (("python check.py", 3),)
    message = facts.message()
    assert "`make lint`" in message and "`python check.py` (exit code 3)" in message
    assert "pytest" not in message and "npm test" not in message


def test_a_command_started_in_the_background_counts_as_run(tmp_path: Path) -> None:
    facts = collect_finish_facts(
        root=tmp_path,
        request_commands=["npm run dev"],
        observed_commands=[("npm run dev", None, None)],  # started as a service: outcome unknown
    )
    assert not facts


def test_facts_about_files_and_paths(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out/good.json").write_text('{"total": 3}', encoding="utf-8")
    (tmp_path / "out/bad.json").write_text('{"total": 3,}', encoding="utf-8")
    (tmp_path / "settings.toml").write_text("name = = 1\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# not structured", encoding="utf-8")
    # Not reported: settings files with comments, a byte order mark, and YAML,
    # where templates and custom tags are ordinary.
    (tmp_path / "tsconfig.json").write_text('{\n  // strict mode\n  "strict": true\n}\n')
    (tmp_path / "bom.json").write_text('\ufeff{"a": 1}', encoding="utf-8")
    (tmp_path / "stack.yaml").write_text("Value: !Sub ${Name}-bucket\n{{ if .x }}\n")
    facts = collect_finish_facts(
        root=tmp_path,
        request_commands=[],
        created_paths=[
            "out/good.json",
            "out/bad.json",
            "settings.toml",
            "notes.md",
            "gone.json",
            "tsconfig.json",
            "bom.json",
            "stack.yaml",
        ],
        written_paths=["out/good.json", "reports/summary.md"],
    )
    assert [path for path, _ in facts.unparseable_files] == ["out/bad.json", "settings.toml"]
    assert facts.unparseable_files[0][1].startswith("JSON: ")
    assert facts.missing_paths == ("reports/summary.md",)
    assert "reports/summary.md" in facts.message()


def test_facts_about_a_service_the_turn_started(tmp_path: Path) -> None:
    from alysis_code.agent.acceptance_contract import durable_service_problem

    ready = {
        "service_id": "svc_a",
        "ownership": "DURABLE_SERVICE",
        "status": "running",
        "alive": True,
        "readiness": {"type": "tcp", "status": "ready", "port": 8080},
    }
    assert durable_service_problem(ready) is None
    dead = {**ready, "status": "exited", "alive": False}
    problem = durable_service_problem(dead)
    assert problem and "svc_a" in problem
    facts = collect_finish_facts(root=tmp_path, request_commands=[], services_not_ready=[problem])
    assert facts and "svc_a" in facts.message()


def test_the_turn_tracks_services_it_started_and_stopped(tmp_path: Path) -> None:
    from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect

    state = TurnExecutionState(execution_requested=True)
    started = {"service_id": "svc_a", "ownership": "DURABLE_SERVICE", "status": "running"}
    for tool, arguments, result in (
        ("shell_service_start", {"cmd": "python -m http.server"}, started),
        ("shell_service_start", {"cmd": "npm run dev"}, {**started, "service_id": "svc_b"}),
        ("shell_service_stop", {"service_id": "svc_b"}, {"service_id": "svc_b"}),
        ("shell_background", {"cmd": "sleep 5"}, {"process_id": "p1"}),
        # Looking at a service from an earlier turn does not make it this turn's.
        ("shell_service_status", {"service_id": "svc_old"}, {**started, "service_id": "svc_old"}),
        # A service replaced by a new one was stopped on purpose.
        (
            "shell_service_start",
            {"cmd": "npm run dev", "replace_service_id": "svc_c"},
            {**started, "service_id": "svc_d"},
        ),
    ):
        _record_tool_effect(
            root=tmp_path,
            state=state,
            tool_name=tool,
            arguments=arguments,
            status="ok",
            result=result,
            known_verification_commands=[],
        )
    assert state.turn_started_service_ids - state.turn_stopped_service_ids == {"svc_a", "svc_d"}
    assert "npm run dev" in state.turn_started_commands
    assert "sleep 5" in state.turn_started_commands


def test_no_facts_no_message(tmp_path: Path) -> None:
    facts = collect_finish_facts(
        root=tmp_path, request_commands=["pytest -q"], observed_commands=[("pytest -q", True, 0)]
    )
    assert not facts and facts.message() == "" and FinishFacts().as_payload()["missing_paths"] == []


# --- the turn -----------------------------------------------------------------

DISPOSITION_MARKER = "classify one assistant reply"


class _Client:
    """Scripted tool calls and replies; a separate scripted verdict for the reply check."""

    model = "test-model"
    temperature = 0.2

    def __init__(self, *steps: Any, disposition: str = "ANSWER") -> None:
        self.steps = list(steps)
        self.disposition = disposition
        self.disposition_calls = 0
        self.tool_names: list[set[str]] = []
        self.offered: list[bool] = []

    def chat(self, *, messages: list[dict[str, Any]], tools=None, **_: Any) -> LLMResponse:
        if any(DISPOSITION_MARKER in str(message.get("content") or "") for message in messages):
            self.disposition_calls += 1
            return LLMResponse(content=self.disposition, tool_calls=[], raw={})
        self.tool_names.append({tool.get("function", {}).get("name") for tool in tools or []})
        self.offered.append(
            any("task_artifacts is optional" in str(m.get("content") or "") for m in messages)
        )
        step = self.steps.pop(0) if self.steps else "Done."
        if isinstance(step, str):
            return LLMResponse(content=step, tool_calls=[], raw={})
        calls = step if isinstance(step, list) else [step]  # a list is one batch of calls
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id=f"call-{len(self.steps)}-{index}", name=name, arguments=arguments)
                for index, (name, arguments) in enumerate(calls)
            ],
            raw={},
        )


class _Surface(NoopSurface):
    def request_approval(self, request):
        return ApprovalDecision(True)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_GATE_DOWNGRADE", raising=False)
    monkeypatch.delenv("ALYSIS_ZERO_ACTIVITY_DISPOSITION_CHECK", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "check.py").write_text("import calc\nassert calc.add(2, 3) == 5\n", encoding="utf-8")
    return root


def _run(project: Path, instruction: str, client: _Client):
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", web_search_mode="off"),
        root=project,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="fixture-key",
        surface=_Surface(),
        enable_compaction=False,
        verification_enabled=False,
        enable_chat_turn_step_budget=True,
        session_log_dir_override=project.parent / "sessions",
    )
    session.client = client
    try:
        assert session.run_turn(instruction) == 0
        events = list(read_session_events(session.store.path))
        state = session._turn_execution_state
    finally:
        session.close()
    stated = [
        event["payload"]["finish_facts"]
        for event in events
        if event["type"] == "completion_gate_nudge" and event["payload"].get("finish_facts")
    ]
    final = [event["payload"]["content"] for event in events if event["type"] == "final"]
    return stated, final, state


COMMAND = f"{PYTHON} check.py"
EDIT = ("fs_write", {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n\n"})
BREAK = ("fs_write", {"path": "calc.py", "content": "def add(a, b):\n    return a - b\n"})
RUN = ("shell_run", {"cmd": COMMAND})

REQUESTS = [
    f"Tidy calc.py. `{COMMAND}` must pass.",
    f"Τακτοποίησε το calc.py. Το `{COMMAND}` πρέπει να περνάει.",
    f"整理 calc.py。`{COMMAND}` 必须通过",
    f"clean up calc.py pls, `{COMMAND}` gotta be green",
]


@pytest.mark.parametrize("request_text", REQUESTS)
def test_a_written_command_that_was_not_run_is_stated_once(project, request_text) -> None:
    client = _Client(EDIT, "Tidied calc.py.", RUN, "Tidied calc.py; the check passes.")
    stated, final, _ = _run(project, request_text, client)
    assert len(stated) == 1
    assert stated[0]["commands_not_run"] == [COMMAND]
    assert stated[0]["commands_failed"] == []
    assert "the check passes" in final[-1]
    # The declaration was on offer, not asked for: the model went straight to work.
    assert TOOL_NAME in client.tool_names[0]
    assert client.offered[0] is True


@pytest.mark.parametrize("request_text", REQUESTS[:2])
def test_a_written_command_whose_last_run_failed_is_stated_once(project, request_text) -> None:
    client = _Client(BREAK, RUN, "Changed calc.py.", EDIT, RUN, "Fixed; the check passes.")
    stated, final, _ = _run(project, request_text, client)
    assert len(stated) == 1
    assert stated[0]["commands_not_run"] == []
    assert [item[0] for item in stated[0]["commands_failed"]] == [COMMAND]
    assert "the check passes" in final[-1]


@pytest.mark.parametrize("request_text", REQUESTS[:2])
def test_a_written_command_that_ran_and_passed_states_nothing(project, request_text) -> None:
    client = _Client(EDIT, RUN, "Tidied calc.py; the check passes.")
    stated, final, _ = _run(project, request_text, client)
    assert stated == []
    assert len(final) == 1 and final[0].startswith("Tidied calc.py; the check passes.")


def test_the_facts_are_stated_once_even_if_they_still_hold(project) -> None:
    # The model decides the command need not be run; the host does not insist.
    client = _Client(EDIT, "Tidied calc.py.", "The check is not needed for a formatting change.")
    stated, final, _ = _run(project, REQUESTS[0], client)
    assert len(stated) == 1
    assert "not needed" in final[-1]


@pytest.mark.parametrize(
    "request_text",
    [f"What does `{COMMAND}` do?", f"Τι κάνει το `{COMMAND}`;"],
)
def test_an_answer_that_did_no_work_is_not_confronted_with_facts(project, request_text) -> None:
    client = _Client("It imports calc and checks add.", disposition="ANSWER")
    stated, final, _ = _run(project, request_text, client)
    assert stated == []
    assert len(final) == 1 and final[0].startswith("It imports calc and checks add.")
    assert client.disposition_calls == 1


@pytest.mark.parametrize(
    "request_text",
    [
        f"What exactly does `{COMMAND}` check? Change nothing.",
        f"Τι ακριβώς ελέγχει το `{COMMAND}`;",
    ],
)
def test_a_question_answered_by_reading_files_is_not_confronted(project, request_text) -> None:
    client = _Client(("fs_read", {"path": "check.py"}), "It asserts that add(2, 3) is 5.")
    stated, final, _ = _run(project, request_text, client)
    assert stated == []
    assert len(final) == 1 and final[0].startswith("It asserts")
    assert client.disposition_calls == 0  # reading is evidence enough that this is an answer


def test_a_reply_that_claims_work_it_did_not_do_is_confronted(project) -> None:
    client = _Client("I ran it and it passes.", RUN, "It passes.", disposition="WORK_CLAIM")
    stated, final, _ = _run(project, f"Τρέξε το `{COMMAND}`.", client)
    assert len(stated) == 1
    assert stated[0]["commands_not_run"] == [COMMAND]
    assert final[-1].startswith("It passes.")


def test_a_new_file_that_does_not_parse_is_stated(project) -> None:
    client = _Client(
        ("fs_write", {"path": "out/totals.json", "content": '{"total": 5,}'}),
        "Wrote the totals.",
        ("fs_write", {"path": "out/totals.json", "content": '{"total": 5}'}),
        "Wrote the totals as valid JSON.",
    )
    stated, final, _ = _run(project, "Γράψε τα σύνολα στο out/totals.json.", client)
    assert len(stated) == 1
    assert [path for path, _ in stated[0]["unparseable_files"]] == ["out/totals.json"]
    assert stated[0]["missing_paths"] == []
    assert "valid JSON" in final[-1]


def test_a_written_path_that_is_missing_is_stated(project) -> None:
    client = _Client(
        ("fs_write", {"path": "summary.md", "content": "# Summary\n"}),
        "Wrote the summary.",
        ("fs_write", {"path": "notes/summary.md", "content": "# Summary\n"}),
        "Wrote notes/summary.md.",
    )
    stated, final, _ = _run(project, "Φτιάξε το notes/summary.md με μια σύνοψη.", client)
    assert len(stated) == 1
    assert stated[0]["missing_paths"] == ["notes/summary.md"]
    assert final[-1].startswith("Wrote notes/summary.md.")


def test_a_path_the_turn_removed_itself_is_not_reported_missing(project) -> None:
    (project / "build").mkdir()
    (project / "build/output.log").write_text("old\n", encoding="utf-8")
    client = _Client(("fs_delete", {"path": "build/output.log"}), "Deleted the log.")
    stated, final, _ = _run(project, "Σβήσε το build/output.log.", client)
    assert not (project / "build/output.log").exists()
    assert stated == []
    assert final[-1].startswith("Deleted the log.")


def test_facts_are_stated_even_after_the_gate_used_its_repair_round(project) -> None:
    """The facts have their own once-only turn; they neither need nor use the gate's."""
    client = _Client(
        (TOOL_NAME, {"paths": [], "commands": [COMMAND]}),  # only the command is declared
        "Done.",  # the declared command is still open: the gate sends the model back
        RUN,
        "Ran the check.",  # the gate is clear; notes/summary.md was never written
        ("fs_write", {"path": "notes/summary.md", "content": "# Summary\n"}),
        "Ran the check and wrote notes/summary.md.",
    )
    stated, final, state = _run(
        project, f"Φτιάξε το notes/summary.md και τρέξε το `{COMMAND}`.", client
    )
    assert [item["missing_paths"] for item in stated] == [["notes/summary.md"]]
    assert final[-1].startswith("Ran the check and wrote notes/summary.md.")
    # Declaring only a requirement kept the mentions the host had detected.
    assert [ref.display_path for ref in state.acceptance_contract.path_refs] == ["notes/summary.md"]


# --- declared requirements ------------------------------------------------------


@pytest.mark.parametrize("request_text", [f"Run `{COMMAND}`.", f"Τρέξε το `{COMMAND}`."])
def test_a_declared_command_holds_the_turn_until_it_has_passed(project, request_text) -> None:
    client = _Client(
        (TOOL_NAME, {"paths": [], "commands": [COMMAND]}),
        "It passes.",  # claimed without running: the declared requirement is still open
        RUN,
        "It passes.",
    )
    stated, final, state = _run(project, request_text, client)
    criteria = [item for item in state.acceptance_contract.criteria if item.commands]
    assert [item.commands for item in criteria] == [(COMMAND,)]
    assert criteria[0].status.value == "PASSED"
    assert client.steps == []  # the gate sent the model back, and it ran the command
    assert stated == []  # the gate enforced it; no separate statement of facts was needed


def test_a_declaration_on_offer_may_lead_the_batch_that_starts_the_work(project) -> None:
    declare = (TOOL_NAME, {"paths": [], "commands": [COMMAND]})
    client = _Client([declare, ("fs_read", {"path": "check.py"}), RUN], "It passes.")
    stated, _, state = _run(project, f"Τρέξε το `{COMMAND}` και πες μου αν περνάει.", client)
    criteria = [item for item in state.acceptance_contract.criteria if item.commands]
    assert [(item.commands, item.status.value) for item in criteria] == [((COMMAND,), "PASSED")]
    assert stated == []


def test_a_declaration_after_work_has_started_is_refused_and_costs_nothing(project) -> None:
    declare = (TOOL_NAME, {"paths": [], "commands": [COMMAND]})
    client = _Client([RUN, declare], "It passes.")
    stated, final, state = _run(project, f"Run `{COMMAND}`.", client)
    assert not [item for item in state.acceptance_contract.criteria if item.commands]
    # The run that came first is still on record: nothing to state.
    assert stated == []
    assert final[-1].startswith("It passes.")
    # The refusal says it is final, so the model does not spend more responses on it.
    refusal = _declaration_results(project)[0]
    assert refusal["error"] == "requirements_already_in_use"
    assert "do not call task_artifacts again" in refusal["message"]


def _declaration_results(project) -> list[dict[str, Any]]:
    events = [
        event
        for path in sorted((project.parent / "sessions").rglob("*.jsonl"))
        for event in read_session_events(path)
    ]
    return [
        event["payload"]["result"]
        for event in events
        if event["type"] == "tool_result" and event["payload"].get("name") == TOOL_NAME
    ]


def test_a_doubled_declaration_can_still_be_sent_once(project) -> None:
    declare = (TOOL_NAME, {"commands": [COMMAND]})
    # The same call a third time would meet the guard on repeated failing calls,
    # so the corrected one is written differently.
    once = (TOOL_NAME, {"paths": [], "commands": [COMMAND]})
    client = _Client([declare, declare], [once, RUN], "It passes.")
    _, _, state = _run(project, f"Run `{COMMAND}`.", client)
    results = _declaration_results(project)
    assert [result.get("error") for result in results[:2]] == ["requirements_already_in_use"] * 2
    # Nothing has begun, so the refusal must not say the declaration is closed.
    assert all("do not call task_artifacts again" not in r["message"] for r in results[:2])
    assert "error" not in results[2]
    criteria = [item for item in state.acceptance_contract.criteria if item.commands]
    assert [(item.commands, item.status.value) for item in criteria] == [((COMMAND,), "PASSED")]


def test_a_rejected_declaration_says_truthfully_whether_it_can_be_sent_again(project) -> None:
    wrong = (TOOL_NAME, {"commands": ["rm -rf build"]})  # not a command the user wrote
    # Alone in its response: nothing has begun, so a corrected call is welcome.
    client = _Client(wrong, [(TOOL_NAME, {"commands": [COMMAND]}), RUN], "It passes.")
    _run(project, f"Run `{COMMAND}`.", client)
    first, second = _declaration_results(project)[:2]
    assert (first["error"], first["retryable"]) == ("invalid_task_artifacts", True)
    assert "error" not in second


def test_a_rejected_declaration_that_led_the_work_is_not_called_retryable(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "check.py").write_text("print('ok')\n", encoding="utf-8")
    wrong = (TOOL_NAME, {"commands": ["rm -rf build"]})
    client = _Client([wrong, RUN], "It passes.")
    _run(root, f"Run `{COMMAND}`.", client)
    result = _declaration_results(root)[0]
    assert (result["error"], result["retryable"]) == ("invalid_task_artifacts", False)
    assert "cannot be sent again in this turn" in result["message"]


@pytest.mark.parametrize("request_text", [f"Run `{COMMAND}`.", f"Τρέξε το `{COMMAND}`."])
def test_a_declaration_may_name_commands_alone(project, request_text) -> None:
    """Models leave out an empty list: a declaration without paths is one that names none."""
    client = _Client([(TOOL_NAME, {"commands": [COMMAND]}), RUN], "It passes.")
    _, _, state = _run(project, request_text, client)
    criteria = [item for item in state.acceptance_contract.criteria if item.commands]
    assert [(item.commands, item.status.value) for item in criteria] == [((COMMAND,), "PASSED")]
    assert "error" not in _declaration_results(project)[0]


@pytest.mark.parametrize(
    "arguments",
    [
        {"paths": [], "keep_running": {}},
        {"paths": [{"name": "calc.py", "directory": "", "role": "required_output"}]},
        {"commands": ["rm -rf build"]},
    ],
)
def test_a_declaration_nobody_asked_for_records_nothing(project, arguments) -> None:
    """The tool is listed in every turn; where the request leaves nothing to
    declare, a call changes nothing, as when the tool was not there to call."""
    import shutil

    def criteria(state):
        return sorted(
            (item.kind.value, item.description) for item in state.acceptance_contract.criteria
        )

    twin = project.parent / "twin"
    shutil.copytree(project, twin)
    _, _, plain = _run(twin, "Τι κάνει το calc.py;", _Client("calc.py adds two numbers."))

    client = _Client((TOOL_NAME, arguments), "calc.py adds two numbers.")
    stated, final, state = _run(project, "Τι κάνει το calc.py;", client)
    assert client.offered[0] is False
    assert _declaration_results(project)[0]["error"] == "nothing_to_declare"
    assert criteria(state) == criteria(plain)
    assert stated == []
    assert final[-1].startswith("calc.py adds two numbers.")
    assert "Not verified" not in final[-1]


def test_a_command_the_user_did_not_write_cannot_be_declared(project) -> None:
    client = _Client((TOOL_NAME, {"paths": [], "commands": ["rm -rf build"]}), RUN, "Done.")
    _, _, state = _run(project, f"Run `{COMMAND}`.", client)
    assert not [item for item in state.acceptance_contract.criteria if item.commands]


@pytest.mark.parametrize(
    "instruction,arguments,message",
    [
        # A command is a whole code span of the request, not a fragment of its prose.
        ("Fill in the form and run `pytest -q`.", {"commands": ["rm"]}, "not written"),
        ("Make sure `pytest -q` passes.", {"commands": ["make"]}, "not written"),
        ("Run `pytest -q` then `git status`.", {"commands": ["pytest -q then git status"]}, "not"),
        # A port or a target is a number the request writes.
        ("Keep the server running.", {"keep_running": {"port": 8080}}, "not written"),
        (
            "Accuracy must be at least 90%.",
            {"thresholds": [{"metric": "accuracy", "operator": ">=", "value": 99}]},
            "not written",
        ),
        (
            "Accuracy must be at least 90%.",
            {"thresholds": [{"metric": "accuracy", "operator": ">=", "value": float("inf")}]},
            "finite",
        ),
        ("Run `pytest -q`.", {"commands": []}, "Nothing was declared"),
    ],
)
def test_a_declared_requirement_must_be_grounded_in_the_request(
    tmp_path: Path, instruction, arguments, message
) -> None:
    from alysis_code.agent.task_artifact_binding import (
        bind_declared_requirements,
        bind_path_interpretation,
    )

    arguments = {"paths": [], **arguments}
    with pytest.raises(ValueError, match=message):
        refs = bind_path_interpretation(
            root=tmp_path, instruction=instruction, arguments=arguments, prior_refs=[]
        )
        bind_declared_requirements(instruction=instruction, arguments=arguments, refs=refs)


def test_commands_in_a_fenced_block_can_be_declared() -> None:
    from alysis_code.agent.task_artifact_binding import bind_declared_requirements

    instruction = "Αυτά πρέπει να περνάνε:\n```bash\nnpm test\npytest -q\n```\n"
    declared = bind_declared_requirements(
        instruction=instruction,
        arguments={"paths": [], "commands": ["npm test", "pytest -q"]},
        refs=[],
    )
    assert declared.commands == ("npm test", "pytest -q")


def test_a_declared_target_is_read_from_lines_that_name_the_metric() -> None:
    from alysis_code.agent.acceptance_contract import (
        AcceptanceCriterionStatus,
        AcceptanceThreshold,
        EvidenceOrigin,
        _evaluate_thresholds,
    )

    def judge(metric, operator, value, output, unit=""):
        return _evaluate_thresholds(
            thresholds=(AcceptanceThreshold(metric, operator, value, unit),),
            output=output,
            passed=True,
            origin=EvidenceOrigin.DIRECT_BLACK_BOX,
        )[0]

    output = "epoch 30 of 30\nloss: 0.412\nακρίβεια: 0.93\nfiles: 1200\n"
    assert judge("ακρίβεια", ">=", 0.9, output) == AcceptanceCriterionStatus.PASSED
    assert judge("ακρίβεια", ">=", 0.95, output) == AcceptanceCriterionStatus.FAILED
    # A command that never names the metric says nothing about it.
    assert judge("accuracy", ">=", 0.9, output) is None
    # A timing, told by its unit, needs more than one sample.
    assert judge("latency", "<", 50, "latency: 42", unit="ms") == AcceptanceCriterionStatus.BLOCKED


def test_the_declaration_is_not_on_offer_without_a_written_command(project) -> None:
    client = _Client("calc.py adds two numbers.")
    _run(project, "Τι κάνει το calc.py;", client)
    # The tool is listed in every turn, so the tool list never changes with the
    # request; the offer is the guidance, and that is not sent.
    assert TOOL_NAME in client.tool_names[0]
    assert client.offered == [False]
