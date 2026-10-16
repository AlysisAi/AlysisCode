"""Machine protocol contract: NDJSON only, and exactly one terminal event."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code import swarm_orchestrator
from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl.commands import forge as forge_commands
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.forge_events import (
    EVENT_ERROR,
    EVENT_PLAN_INVALID,
    EVENT_PLAN_SAVED,
    EVENT_REVIEW_RESULT,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_STARTED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_STARTED,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_NOT_ACCEPTED,
    EXIT_OK,
    SCHEMA_VERSION,
    TERMINAL_EVENT_NAMES,
    ForgeEventEmitter,
    ForgeEventProtocolError,
    ForgeMachineSession,
    active_emitter,
    machine_session,
    parse_event_line,
)
from alysis_code.plan_repair import PlannerRepairReport, record_plan_repair
from alysis_code.review_gate import ReviewOutcome

ALL_FORGE_COMMANDS = ("plan", "show", "status", "review", "exec", "swarm", "attach")


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_CONTEXT_WINDOW": "200000",
        "ALYSIS_MAX_OUTPUT_TOKENS": "8192",
    }


def _parse_ndjson_stream(text: str) -> list[dict]:
    """Parse machine output, failing on any line that is not a protocol event.

    This is the "no stray prints" assertion: in machine mode every single stdout line
    must be a valid event envelope, not prose that happens to sit next to one.
    """
    events: list[dict] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parsed = parse_event_line(raw)
        if parsed is None:
            raise AssertionError(f"stray non-event line on stdout: {raw!r}")
        assert parsed["v"] == SCHEMA_VERSION
        assert set(parsed) == {"v", "event", "ts", "run_id", "data"}
        assert isinstance(parsed["ts"], str) and parsed["ts"]
        assert parsed["run_id"] is None or isinstance(parsed["run_id"], str)
        events.append(parsed)
    return events


def _assert_exactly_one_terminal(events: list[dict]) -> dict:
    terminal = [event for event in events if event["event"] in TERMINAL_EVENT_NAMES]
    assert len(terminal) == 1, f"expected exactly one terminal event, got {len(terminal)}"
    assert terminal[0] is events[-1], "the terminal event must be the last event emitted"
    return terminal[0]


def _machine_events(result) -> list[dict]:  # type: ignore[no-untyped-def]
    events = _parse_ndjson_stream(result.stdout)
    assert events, "machine mode produced no events"
    assert events[0]["event"] == EVENT_RUN_STARTED
    _assert_exactly_one_terminal(events)
    return events


def _events_by_name(events: list[dict], name: str) -> list[dict]:
    return [event for event in events if event["event"] == name]


def _prepare_run(repo: Path, *, title: str = "Implement feature slice") -> tuple[str, object]:
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Machine protocol test"
    plan["summary"] = "Machine protocol test"
    task = add_task(
        plan,
        title=title,
        description=f"Task created from planning chat: {title}",
        estimated_files=["src/feature.py"],
    )
    task["write_scope"] = []
    save_plan(paths, plan)
    return str(task["id"]), paths


# --------------------------------------------------------------------------------------
# Emitter and session unit contract
# --------------------------------------------------------------------------------------


def test_emitter_writes_versioned_envelope() -> None:
    stream = io.StringIO()
    emitter = ForgeEventEmitter(
        command="forge.test",
        stream=stream,
        run_id="run-1",
        clock=lambda: "2026-01-01T00:00:00+00:00",
    )
    emitter.emit(EVENT_PLAN_SAVED, {"task_count": 2})

    payload = json.loads(stream.getvalue().strip())
    assert payload == {
        "v": SCHEMA_VERSION,
        "event": EVENT_PLAN_SAVED,
        "ts": "2026-01-01T00:00:00+00:00",
        "run_id": "run-1",
        "data": {"task_count": 2},
    }


def test_emitter_rejects_undeclared_event_names() -> None:
    emitter = ForgeEventEmitter(command="forge.test", stream=io.StringIO())
    with pytest.raises(ForgeEventProtocolError):
        emitter.emit("plan_almost_saved", {})


def test_disabled_emitter_writes_nothing() -> None:
    stream = io.StringIO()
    emitter = ForgeEventEmitter(command="forge.test", enabled=False, stream=stream)
    assert emitter.emit(EVENT_RUN_STARTED, {}) is False
    assert emitter.run_completed(ok=True) is False
    assert stream.getvalue() == ""


def test_events_after_the_terminal_event_are_dropped() -> None:
    stream = io.StringIO()
    emitter = ForgeEventEmitter(command="forge.test", stream=stream)
    emitter.run_completed(ok=True)
    assert emitter.emit(EVENT_TASK_STARTED, {"task_id": "T01"}) is False
    assert emitter.error(message="too late") is False

    events = _parse_ndjson_stream(stream.getvalue())
    _assert_exactly_one_terminal(events)


def test_session_emits_terminal_event_on_success() -> None:
    stream = io.StringIO()
    with machine_session("forge.test", machine=True, stream=stream):
        pass

    terminal = _assert_exactly_one_terminal(_parse_ndjson_stream(stream.getvalue()))
    assert terminal["event"] == EVENT_RUN_COMPLETED
    assert terminal["data"]["ok"] is True
    assert terminal["data"]["exit_code"] == EXIT_OK


def test_session_maps_exit_one_to_work_not_accepted() -> None:
    stream = io.StringIO()
    with pytest.raises(typer.Exit):
        with machine_session("forge.test", machine=True, stream=stream):
            raise typer.Exit(code=EXIT_NOT_ACCEPTED)

    terminal = _assert_exactly_one_terminal(_parse_ndjson_stream(stream.getvalue()))
    assert terminal["event"] == EVENT_RUN_COMPLETED
    assert terminal["data"]["ok"] is False
    assert terminal["data"]["exit_code"] == EXIT_NOT_ACCEPTED


def test_session_maps_exit_two_to_command_error() -> None:
    stream = io.StringIO()
    with pytest.raises(typer.Exit):
        with machine_session("forge.test", machine=True, stream=stream):
            raise typer.Exit(code=EXIT_ERROR)

    terminal = _assert_exactly_one_terminal(_parse_ndjson_stream(stream.getvalue()))
    assert terminal["event"] == EVENT_ERROR
    assert terminal["data"]["exit_code"] == EXIT_ERROR


def test_session_emits_error_on_unhandled_exception_and_reraises() -> None:
    stream = io.StringIO()
    with pytest.raises(RuntimeError, match="boom"):
        with machine_session("forge.test", machine=True, stream=stream):
            raise RuntimeError("boom")

    terminal = _assert_exactly_one_terminal(_parse_ndjson_stream(stream.getvalue()))
    assert terminal["event"] == EVENT_ERROR
    assert terminal["data"]["kind"] == "exception"
    assert terminal["data"]["message"] == "boom"
    assert terminal["data"]["exception_type"] == "RuntimeError"


def test_session_emits_error_on_keyboard_interrupt_and_reraises() -> None:
    stream = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        with machine_session("forge.test", machine=True, stream=stream):
            raise KeyboardInterrupt

    terminal = _assert_exactly_one_terminal(_parse_ndjson_stream(stream.getvalue()))
    assert terminal["event"] == EVENT_ERROR
    assert terminal["data"]["kind"] == "interrupted"
    assert terminal["data"]["exit_code"] == EXIT_INTERRUPTED


def test_session_does_not_add_a_second_terminal_event() -> None:
    stream = io.StringIO()
    with pytest.raises(typer.Exit):
        with machine_session("forge.test", machine=True, stream=stream) as events:
            events.run_completed(ok=False, exit_code=EXIT_NOT_ACCEPTED, data={"task": "T01"})
            raise typer.Exit(code=EXIT_NOT_ACCEPTED)

    terminal = _assert_exactly_one_terminal(_parse_ndjson_stream(stream.getvalue()))
    assert terminal["data"]["task"] == "T01"


def test_session_registers_and_restores_the_active_emitter() -> None:
    assert active_emitter() is None
    with machine_session("forge.test", machine=True, stream=io.StringIO()) as events:
        assert active_emitter() is events
    assert active_emitter() is None


def test_inert_session_never_becomes_the_active_emitter() -> None:
    with machine_session("forge.test", machine=False, stream=io.StringIO()):
        assert active_emitter() is None


def test_session_restores_active_emitter_even_when_the_body_raises() -> None:
    emitter = ForgeEventEmitter(command="forge.test", stream=io.StringIO())
    with pytest.raises(RuntimeError):
        with ForgeMachineSession(emitter):
            raise RuntimeError("boom")
    assert active_emitter() is None


# --------------------------------------------------------------------------------------
# Every forge command speaks the protocol
# --------------------------------------------------------------------------------------


def test_every_forge_command_accepts_machine_flag() -> None:
    runner = CliRunner()
    forge_group = typer.main.get_command(alysis_app).commands["forge"]
    for command in ALL_FORGE_COMMANDS:
        result = runner.invoke(alysis_app, ["forge", command, "--help"])
        assert result.exit_code == 0, f"forge {command} --help failed"
        assert any(
            "--machine" in parameter.opts for parameter in forge_group.commands[command].params
        ), f"forge {command} is missing --machine"


def test_show_emits_ndjson_only_on_success(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, _ = _prepare_run(repo)

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "show", "--path", os.fspath(repo), "--machine"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    events = _machine_events(result)
    terminal = events[-1]
    assert terminal["event"] == EVENT_RUN_COMPLETED
    assert terminal["data"]["ok"] is True
    assert terminal["data"]["task_count"] == 1
    assert terminal["data"]["tasks"][0]["id"] == task_id


def test_show_reports_draft_status_and_the_blocking_reasons(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # _prepare_run leaves write_scope empty, so this plan is not execution-ready.
    _prepare_run(repo)

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "show", "--path", os.fspath(repo), "--machine"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    data = _machine_events(result)[-1]["data"]
    assert data["plan_status"] == "draft"
    assert data["plan_status_blocking_reasons"]
    assert data["host_repaired"] is False
    assert data["host_repaired_fields"] == []
    assert data["forced_draft"] is False


def test_show_reports_execution_ready_for_a_runnable_plan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _task_id, paths = _prepare_run(repo)
    plan = load_plan(paths)
    plan["tasks"][0]["write_scope"] = ["src/feature.py"]
    save_plan(paths, plan)

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "show", "--path", os.fspath(repo), "--machine"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    data = _machine_events(result)[-1]["data"]
    assert data["plan_status"] == "execution_ready"
    assert data["plan_status_blocking_reasons"] == []


def test_show_surfaces_host_repaired_fields_recorded_on_the_plan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _task_id, paths = _prepare_run(repo)
    plan = load_plan(paths)
    record_plan_repair(
        plan,
        PlannerRepairReport(
            terminal_state="host_repaired",
            host_repaired=True,
            host_repaired_fields=["plan_update.tasks_add[0].write_scope"],
        ),
    )
    save_plan(paths, plan)

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "show", "--path", os.fspath(repo), "--machine"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    data = _machine_events(result)[-1]["data"]
    assert data["host_repaired"] is True
    assert data["host_repaired_fields"] == ["plan_update.tasks_add[0].write_scope"]


def test_show_reports_repairs_in_human_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _task_id, paths = _prepare_run(repo)
    plan = load_plan(paths)
    record_plan_repair(
        plan,
        PlannerRepairReport(
            terminal_state="host_repaired",
            host_repaired=True,
            host_repaired_fields=["plan_update.tasks_add[0].write_scope"],
        ),
    )
    save_plan(paths, plan)

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "show", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "draft (not execution-ready)" in result.stdout
    assert "host-repaired" in result.stdout
    assert "plan_update.tasks_add[0].write_scope" in result.stdout


def test_swarm_plan_invalid_carries_plan_status_and_repair_fields(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Plan the planner could not make runnable"
    record_plan_repair(
        plan,
        PlannerRepairReport(
            terminal_state="forced_draft",
            forced_draft=True,
            host_repaired=True,
            host_repaired_fields=["assistant_message"],
        ),
    )
    save_plan(paths, plan)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--machine",
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == EXIT_ERROR
    invalid = _events_by_name(_machine_events(result), EVENT_PLAN_INVALID)
    assert invalid
    data = invalid[0]["data"]
    # A consumer sees why the plan is not runnable and that it was salvaged.
    assert data["plan_status"] == "draft"
    assert data["plan_status_blocking_reasons"]
    assert data["host_repaired"] is True
    assert data["host_repaired_fields"] == ["assistant_message"]
    assert data["forced_draft"] is True


def test_status_emits_ndjson_only_on_success(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run(repo)

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "status", "--path", os.fspath(repo), "--machine"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    terminal = _machine_events(result)[-1]
    assert terminal["event"] == EVENT_RUN_COMPLETED
    assert terminal["data"]["run_id"]


def test_group_level_machine_flag_is_equivalent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run(repo)

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "--machine", "status", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert _machine_events(result)[-1]["event"] == EVENT_RUN_COMPLETED


@pytest.mark.parametrize("command", ["show", "status"])
def test_missing_run_emits_terminal_error(command: str, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    result = CliRunner().invoke(
        alysis_app,
        ["forge", command, "--path", os.fspath(repo), "--machine"],
        env=_env(tmp_path),
    )

    assert result.exit_code == EXIT_ERROR
    events = _machine_events(result)
    assert _events_by_name(events, EVENT_PLAN_INVALID)
    terminal = events[-1]
    assert terminal["event"] == EVENT_ERROR
    assert terminal["data"]["exit_code"] == EXIT_ERROR
    assert terminal["data"]["message"]


def test_attach_emits_terminal_error_without_a_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = tmp_path / "asset.txt"
    source.write_text("hello\n", encoding="utf-8")

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "attach", os.fspath(source), "--path", os.fspath(repo), "--machine"],
        env=_env(tmp_path),
    )

    assert result.exit_code == EXIT_ERROR
    assert _machine_events(result)[-1]["event"] == EVENT_ERROR


def test_attach_emits_ndjson_only_on_success(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run(repo)
    source = tmp_path / "asset.txt"
    source.write_text("hello\n", encoding="utf-8")

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "attach", os.fspath(source), "--path", os.fspath(repo), "--machine"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    terminal = _machine_events(result)[-1]
    assert terminal["event"] == EVENT_RUN_COMPLETED
    assert terminal["data"]["asset"]["stored_path"]


def _review_outcome(run_dir: Path, task_id: str, *, approved: bool) -> ReviewOutcome:
    json_path = run_dir / "execution" / "reviews" / f"{task_id}.json"
    md_path = run_dir / "execution" / "reviews" / f"{task_id}.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text("{}", encoding="utf-8")
    md_path.write_text("# review\n", encoding="utf-8")
    return ReviewOutcome(
        task_id=task_id,
        approved=approved,
        confidence="high" if approved else "medium",
        summary="ok" if approved else "changes needed",
        blocking_issues_count=0 if approved else 1,
        non_blocking_issues_count=0,
        json_path=json_path,
        markdown_path=md_path,
    )


@pytest.mark.parametrize("approved", [True, False])
def test_review_exits_zero_and_reports_the_verdict_as_data(
    approved: bool,
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, paths = _prepare_run(repo)

    monkeypatch.setattr(
        cli_mod,
        "review_task",
        lambda **_kwargs: _review_outcome(paths.run_dir, task_id, approved=approved),
    )

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "review",
            task_id,
            "--path",
            os.fspath(repo),
            "--machine",
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )

    # A review that rejects work is a completed review, not a crashed command.
    assert result.exit_code == 0
    events = _machine_events(result)
    review_events = _events_by_name(events, EVENT_REVIEW_RESULT)
    assert len(review_events) == 1
    assert review_events[0]["data"]["approved"] is approved
    terminal = events[-1]
    assert terminal["event"] == EVENT_RUN_COMPLETED
    assert terminal["data"]["ok"] is True
    assert terminal["data"]["exit_code"] == EXIT_OK
    assert terminal["data"]["review"]["approved"] is approved


def test_review_emits_terminal_error_for_unknown_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run(repo)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "review",
            "T99",
            "--path",
            os.fspath(repo),
            "--machine",
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == EXIT_ERROR
    assert _machine_events(result)[-1]["event"] == EVENT_ERROR


def _invoke_exec(repo: Path, task_id: str, tmp_path: Path, *, machine: bool = True):  # type: ignore[no-untyped-def]
    argv = [
        "forge",
        "exec",
        task_id,
        "--path",
        os.fspath(repo),
        "--model",
        "test-model",
        "--api-key",
        "k",
        "--no-log",
    ]
    if machine:
        argv.append("--machine")
    return CliRunner().invoke(alysis_app, argv, env=_env(tmp_path))


def test_exec_emits_task_lifecycle_and_terminal_event_on_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, _ = _prepare_run(repo)

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        target = root / "src" / "feature.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VALUE = 1\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = _invoke_exec(repo, task_id, tmp_path)

    assert result.exit_code == EXIT_OK
    events = _machine_events(result)
    assert _events_by_name(events, EVENT_TASK_STARTED)
    completed = _events_by_name(events, EVENT_TASK_COMPLETED)
    assert len(completed) == 1
    assert completed[0]["data"]["task_id"] == task_id
    terminal = events[-1]
    assert terminal["event"] == EVENT_RUN_COMPLETED
    assert terminal["data"]["ok"] is True


def test_exec_failure_is_a_completed_run_with_a_failed_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, _ = _prepare_run(repo)

    def fake_run_agent(**_kwargs) -> int:
        return 1

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = _invoke_exec(repo, task_id, tmp_path)

    assert result.exit_code == EXIT_NOT_ACCEPTED
    events = _machine_events(result)
    failed = _events_by_name(events, EVENT_TASK_FAILED)
    assert len(failed) == 1
    assert failed[0]["data"]["task_id"] == task_id
    terminal = events[-1]
    assert terminal["event"] == EVENT_RUN_COMPLETED
    assert terminal["data"]["ok"] is False
    assert terminal["data"]["exit_code"] == EXIT_NOT_ACCEPTED


def test_exec_reports_an_engine_exception_as_a_failed_task(tmp_path: Path, monkeypatch) -> None:
    """An exception the command handles is a failed task, not a crashed command."""
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, _ = _prepare_run(repo)

    def exploding_run_agent(**_kwargs) -> int:
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(cli_mod, "run_agent", exploding_run_agent)

    result = _invoke_exec(repo, task_id, tmp_path)

    assert result.exit_code == EXIT_NOT_ACCEPTED
    events = _machine_events(result)
    assert _events_by_name(events, EVENT_TASK_FAILED)
    assert events[-1]["event"] == EVENT_RUN_COMPLETED
    assert events[-1]["data"]["ok"] is False


def test_terminal_event_is_emitted_when_an_exception_escapes_the_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The invariant's hardest case: an unhandled exception on the way out."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run(repo)

    def explode(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("asset view exploded")

    monkeypatch.setattr(forge_commands, "forge_asset_view_entries", explode)

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "show", "--path", os.fspath(repo), "--machine"],
        env=_env(tmp_path),
    )

    # The exception is not swallowed: it still propagates out of the command.
    assert isinstance(result.exception, RuntimeError)
    terminal = _assert_exactly_one_terminal(_parse_ndjson_stream(result.stdout))
    assert terminal["event"] == EVENT_ERROR
    assert terminal["data"]["kind"] == "exception"
    assert terminal["data"]["exception_type"] == "RuntimeError"
    assert terminal["data"]["message"] == "asset view exploded"


def test_exec_emits_terminal_error_for_unknown_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run(repo)

    result = _invoke_exec(repo, "T99", tmp_path)

    assert result.exit_code == EXIT_ERROR
    events = _machine_events(result)
    assert _events_by_name(events, EVENT_PLAN_INVALID)
    assert events[-1]["event"] == EVENT_ERROR


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("in_progress", EVENT_TASK_STARTED),
        ("done", EVENT_TASK_COMPLETED),
        ("already_satisfied", EVENT_TASK_COMPLETED),
        ("failed", EVENT_TASK_FAILED),
        ("verify_failed", EVENT_TASK_FAILED),
        ("changes_requested", EVENT_TASK_FAILED),
        ("merge_conflict", EVENT_TASK_FAILED),
        ("interrupted", EVENT_TASK_FAILED),
    ],
)
def test_swarm_task_transitions_emit_lifecycle_events(
    status: str,
    expected: str,
    tmp_path: Path,
) -> None:
    """Swarm task events come from the one place every transition passes through."""
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, paths = _prepare_run(repo)
    plan = load_plan(paths)
    stream = io.StringIO()

    with machine_session("forge.swarm", machine=True, stream=stream):
        swarm_orchestrator._mark_status(paths, plan, task_id, status)

    events = _parse_ndjson_stream(stream.getvalue())
    lifecycle = [event for event in events if event["event"] not in TERMINAL_EVENT_NAMES]
    assert len(lifecycle) == 1
    assert lifecycle[0]["event"] == expected
    assert lifecycle[0]["data"] == {
        "task_id": task_id,
        "status": status,
        "run_id": paths.run_id,
        "source": "swarm",
    }


@pytest.mark.parametrize("status", ["planned", "todo", "ready_for_merge"])
def test_swarm_intermediate_statuses_emit_no_task_event(status: str, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, paths = _prepare_run(repo)
    plan = load_plan(paths)
    stream = io.StringIO()

    with machine_session("forge.swarm", machine=True, stream=stream):
        swarm_orchestrator._mark_status(paths, plan, task_id, status)

    events = _parse_ndjson_stream(stream.getvalue())
    assert [event["event"] for event in events] == [EVENT_RUN_COMPLETED]


def test_swarm_task_transitions_are_silent_in_human_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, paths = _prepare_run(repo)
    plan = load_plan(paths)

    swarm_orchestrator._mark_status(paths, plan, task_id, "done")

    assert load_plan(paths)["tasks"][0]["status"] == "done"


def test_swarm_dry_run_emits_ndjson_only(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run(repo)
    monkeypatch.setattr(cli_mod, "run_swarm", lambda **_kwargs: EXIT_OK)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--dry-run",
            "--machine",
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == EXIT_OK
    terminal = _machine_events(result)[-1]
    assert terminal["event"] == EVENT_RUN_COMPLETED
    assert terminal["data"]["dry_run"] is True


def test_swarm_unexpected_exception_is_a_command_error_not_a_failed_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run(repo)

    def exploding_run_swarm(**_kwargs) -> int:
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(cli_mod, "run_swarm", exploding_run_swarm)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--machine",
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )

    # Exit 2 keeps exit 1 meaning "ran fine, work not accepted".
    assert result.exit_code == EXIT_ERROR
    terminal = _machine_events(result)[-1]
    assert terminal["event"] == EVENT_ERROR
    assert terminal["data"]["exit_code"] == EXIT_ERROR


def test_swarm_reports_plan_invalid_when_no_tasks_are_execution_ready(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Empty plan"
    save_plan(paths, plan)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--machine",
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == EXIT_ERROR
    events = _machine_events(result)
    assert _events_by_name(events, EVENT_PLAN_INVALID)
    assert events[-1]["event"] == EVENT_ERROR


# --------------------------------------------------------------------------------------
# Human mode is untouched
# --------------------------------------------------------------------------------------


def test_direct_python_calls_do_not_enable_machine_mode() -> None:
    """Unfilled typer parameters are truthy OptionInfo objects, not booleans."""
    unfilled = typer.Option(False, "--machine")
    assert forge_commands._machine_enabled(None, unfilled) is False
    assert forge_commands._machine_enabled(None, False) is False
    assert forge_commands._machine_enabled(None, True) is True


def test_human_mode_prints_prose_and_no_events(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run(repo)

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "status", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "run_id" in result.stdout
    assert all(parse_event_line(line) is None for line in result.stdout.splitlines())


def test_plan_stdout_is_pure_ndjson_when_prompted_goal_needs_intent(tmp_path: Path) -> None:
    """`forge plan` prompts; in machine mode the prompt must not reach stdout.

    This runs a real subprocess because the click test runner echoes prompts to stdout
    itself, which would mask exactly the defect this asserts against.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ)
    env.update(_env(tmp_path))
    # Enforce the test's credential-free premise even on a developer or CI host that
    # exports a provider key. Reading the inherited values is neither necessary nor safe.
    env.pop("ALYSIS_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alysis_code",
            "forge",
            "plan",
            "--path",
            os.fspath(repo),
            "--machine",
        ],
        input="Machine protocol\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )

    assert completed.returncode == EXIT_ERROR, completed.stderr
    events = _parse_ndjson_stream(completed.stdout)
    assert events[0]["event"] == EVENT_RUN_STARTED
    assert not _events_by_name(events, EVENT_PLAN_SAVED)
    terminal = _assert_exactly_one_terminal(events)
    assert terminal["event"] == EVENT_ERROR
    assert "requires --intent" in terminal["data"]["message"]
    # The prompt still has to go somewhere a human driving it can see.
    assert "Forge goal" in completed.stderr
