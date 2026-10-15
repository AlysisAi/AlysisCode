"""Provider-free harness contracts; these do not score fabricated model answers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts.qa import subagent_live_eval as live_eval
from scripts.qa.subagent_eval_cases import all_cases
from scripts.qa.subagent_eval_evidence import completed_turns, summarize_evidence
from scripts.qa.subagent_eval_subscription import observe_root_turns
from scripts.qa.subagent_live_eval import (
    DEFAULT_BOUNDS,
    acceptance,
    check_budget,
    dump,
    freeze_source,
    load_plan,
    observed_token_bound,
    prepare_attempt,
    run_attempt,
    screen_manifest,
    verify_source,
)


def source_freeze(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    (origin / "src/alysis_code").mkdir(parents=True)
    (origin / "src/alysis_code/config.py").write_text("VERSION = 1\n")
    frozen = tmp_path / "frozen"
    freeze_source(origin, frozen)
    return frozen


def test_stop_process_uses_owned_tree_cleanup_without_posix_groups(monkeypatch):
    from alysis_code.agent_runtimes import codex_cli

    process = object()
    stopped = []
    monkeypatch.delattr(live_eval.os, "killpg", raising=False)
    monkeypatch.setattr(codex_cli, "_terminate_process_tree", stopped.append)
    live_eval.stop_process(process)
    assert stopped == [process]


def plan_fixture(tmp_path: Path) -> Path:
    frozen = source_freeze(tmp_path)
    plan = tmp_path / "plan.json"
    screen_manifest(frozen, frozen, tmp_path / "campaign", plan)
    return plan


def test_screen_plan_has_twelve_predeclared_cells_and_no_equivalent_duplicate(tmp_path):
    plan = load_plan(plan_fixture(tmp_path))
    assert len(plan["attempts"]) == 12
    assert sum(a["direct"] for a in plan["attempts"]) == 2
    assert not any(
        a["model"] == "gpt-5.6-terra" and a["variant"] == "profiled" for a in plan["attempts"]
    )
    assert all(a["interface"] == "chat" for a in plan["attempts"])
    assert all(v["source_manifest_sha256"] for v in plan["variants"])


def test_multiturn_cannot_be_flattened_into_run(tmp_path):
    path = plan_fixture(tmp_path)
    plan = json.loads(path.read_text())
    plan["attempts"][0]["interface"] = "run"
    dump(path, plan)
    with pytest.raises(ValueError, match="genuine chat"):
        load_plan(path)


def test_live_dispatch_requires_explicit_flag(tmp_path):
    path = plan_fixture(tmp_path)
    attempt = load_plan(path)["attempts"][0]["id"]
    with pytest.raises(ValueError, match="execute-live"):
        run_attempt(path, attempt, execute_live=False)
    assert not (tmp_path / "campaign").exists()


@pytest.mark.parametrize("interface", ["run", "chat"])
@pytest.mark.parametrize("direct", [False, True])
def test_dispatch_preserves_native_stream_default(tmp_path, monkeypatch, interface, direct):
    path = plan_fixture(tmp_path)
    plan = load_plan(path)
    attempt = plan["attempts"][0]
    attempt.update(interface=interface, direct=direct, task="hold-dirty-review")
    plan["attempts"] = [attempt]
    dump(path, plan)
    case = all_cases()[attempt["task"]]
    commands = []
    stopped = []

    class Input:
        def __init__(self):
            self.writes = []

        def write(self, text):
            self.writes.append(text)
            if text == "/exit\n":
                process.returncode = 0

        def flush(self):
            pass

        def close(self):
            pass

    class Process:
        returncode = 0 if interface == "run" else None
        stdin = Input()
        polls = 0

        def poll(self):
            self.polls += 1
            assert self.polls < 10, "The mocked single-turn process must finish promptly"
            return self.returncode

        def wait(self):
            return self.returncode

    process = Process()
    original_popen = live_eval.subprocess.Popen

    def launch(command, **kwargs):
        if len(command) < 2 or Path(command[1]).name != "subagent_eval_subscription.py":
            return original_popen(command, **kwargs)
        commands.append(command)
        assert kwargs["start_new_session"] is True
        return process

    def stop(candidate):
        assert candidate is process
        stopped.append(candidate)
        process.returncode = 130

    def unexpected_signal(*args):
        raise AssertionError("A fake evaluation process must never receive an OS signal")

    monkeypatch.setattr(live_eval.subprocess, "Popen", launch)
    monkeypatch.setattr(live_eval, "stop_process", stop)
    monkeypatch.setattr(live_eval.os, "killpg", unexpected_signal, raising=False)
    # Emulate only the process boundary protocol, never a provider response.
    monkeypatch.setattr(
        live_eval, "completed_turns", lambda directory: [{"payload": {"exit_code": 0}}]
    )
    monkeypatch.setattr(live_eval.time, "sleep", lambda seconds: None)

    result = run_attempt(path, attempt["id"], execute_live=True)
    assert result["returncode"] == 0
    assert result["turns_sent"] == 1
    assert len(result["acceptance"]) == 1
    assert result["acceptance"][0].get("after_process_exit", False) is (interface == "run")
    assert stopped == []
    assert len(commands) == 1
    command = commands[0]
    assert command[2] == interface
    assert "--stream" not in command and "--no-stream" not in command
    assert ("--no-subagents" in command) is direct
    if interface == "run":
        assert command[3:5] == [case.turns[0], "--no-deadline"]
        assert process.stdin.writes == []
    else:
        assert process.stdin.writes == [" ".join(case.turns[0].splitlines()) + "\n", "/exit\n"]
    directory = Path(plan["output_dir"]) / "attempts" / attempt["id"]
    assert json.loads((directory / "attempt.json").read_text())["command"] == command


@pytest.mark.parametrize("interface", ["run", "chat"])
def test_native_cli_stream_default_overrides_saved_false(tmp_path, monkeypatch, interface):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from alysis_code import cli as cli_mod
    from alysis_code import config

    cfg = config.AppConfig(model="test-model", stream=False)
    captured = []
    monkeypatch.setattr(config, "load_config", lambda: config.clone_cfg(cfg))
    monkeypatch.setattr(cli_mod, "load_config", lambda: config.clone_cfg(cfg))

    def capture(*, cfg, **kwargs):
        captured.append(cfg.stream)
        if interface == "run":
            return 0
        return SimpleNamespace(store=SimpleNamespace(session_id="offline"), close=lambda: None)

    monkeypatch.setattr(cli_mod, "run_agent", capture)
    monkeypatch.setattr(cli_mod, "create_session", capture)
    command = [
        interface,
        "--model",
        "test-model",
        "--api-key",
        "DUMMY",
        "--no-log",
        "--path",
        str(tmp_path),
    ]
    if interface == "run":
        command.append("Read the package.")
    result = CliRunner().invoke(
        cli_mod.app,
        command,
        input="exit\n",
        env={"ALYSIS_CONFIG_DIR": str(tmp_path), "ALYSIS_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.output
    assert captured == [True]
    assert cfg.stream is False


@pytest.mark.parametrize("exhaustion", ["deadline", "tokens", "final_input", "final_output", None])
def test_boundary_oracle_cannot_admit_followup_after_budget_exhaustion(
    tmp_path, monkeypatch, exhaustion
):
    path = plan_fixture(tmp_path)
    plan = load_plan(path)
    attempt = plan["attempts"][0]
    attempt["task"] = "dev-quote-chat"
    plan["attempts"] = [attempt]
    dump(path, plan)
    case = all_cases()[attempt["task"]]
    clock = [0.0]
    token_bound = [None]
    checks = []
    oracle_stages = []
    stopped = []

    class Input:
        def __init__(self):
            self.writes = []

        def write(self, text):
            self.writes.append(text)
            if text == "/exit\n":
                process.returncode = 0

        def flush(self):
            pass

        def close(self):
            pass

    class Process:
        returncode = None
        stdin = Input()
        polls = 0

        def poll(self):
            self.polls += 1
            assert self.polls < 10, "The mocked chat must finish the bounded sequence"
            return self.returncode

        def wait(self):
            return self.returncode

    process = Process()
    original_popen = live_eval.subprocess.Popen

    def launch(command, **kwargs):
        if len(command) < 2 or Path(command[1]).name != "subagent_eval_subscription.py":
            return original_popen(command, **kwargs)
        return process

    def stop(candidate):
        assert candidate is process
        stopped.append(candidate)
        process.returncode = 130

    def unexpected_signal(*args):
        raise AssertionError("A fake evaluation process must never receive an OS signal")

    def oracle(directory, python, stage):
        oracle_stages.append(stage)
        # Run the real frozen acceptance/preservation path. Only its elapsed
        # time and the observed token allowance are simulated for admission.
        checked = acceptance(directory, python, stage)
        if stage == 0:
            if exhaustion == "deadline":
                clock[0] = plan["bounds"]["attempt_timeout_s"]
            elif exhaustion == "tokens":
                token_bound[0] = "max_output_tokens"
        return checked

    def observe_bound(directory, remaining):
        checks.append(clock[0])
        return token_bound[0]

    def boundaries(directory):
        sent = sum(text != "/exit\n" for text in process.stdin.writes)
        return [{"payload": {"exit_code": 0}} for _ in range(sent)]

    monkeypatch.setattr(live_eval.subprocess, "Popen", launch)
    monkeypatch.setattr(live_eval, "stop_process", stop)
    monkeypatch.setattr(live_eval.os, "killpg", unexpected_signal, raising=False)
    monkeypatch.setattr(live_eval, "acceptance", oracle)
    monkeypatch.setattr(live_eval, "completed_turns", boundaries)
    monkeypatch.setattr(live_eval, "observed_token_bound", observe_bound)
    monkeypatch.setattr(live_eval.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(live_eval.time, "sleep", lambda seconds: None)
    if exhaustion in {"final_input", "final_output"}:
        # Simulate completed telemetry arriving with the last response, after
        # the last supervision check. No fabricated answer affects the oracle.
        usage = {"uncached_prompt_tokens": 0, "completion_tokens": 0}
        field, limit = (
            ("uncached_prompt_tokens", "max_uncached_input_tokens")
            if exhaustion == "final_input"
            else ("completion_tokens", "max_output_tokens")
        )
        usage[field] = plan["bounds"][limit] + 1
        monkeypatch.setattr(
            live_eval,
            "summarize_evidence",
            lambda _directory: {"known_usage": usage, "unknown_usage": False},
        )

    result = run_attempt(path, attempt["id"], execute_live=True)
    prompts = [" ".join(turn.splitlines()) + "\n" for turn in case.turns]
    if exhaustion in {"deadline", "tokens"}:
        assert process.stdin.writes == prompts[:1]
        assert result["turns_sent"] == 1
        assert oracle_stages == [0], "The checked first turn must not trigger another oracle"
        assert len(stopped) == 1
        assert result["returncode"] == 130
        if exhaustion == "deadline":
            assert result["timed_out"] is True
        else:
            assert result["observed_token_bound_reached"] == "max_output_tokens"
            assert checks == [0.0], "Check immediately, before the 5s periodic budget interval"
    else:
        assert process.stdin.writes == prompts + ["/exit\n"]
        assert result["turns_sent"] == 2
        assert oracle_stages == [0, 1]
        assert result["returncode"] == 0
        assert stopped == []
        if exhaustion in {"final_input", "final_output"}:
            assert result["final_observed_token_bounds_reached"] == [limit]
            assert not result.get("observed_token_bound_reached")
    assert len(result["acceptance"]) == len(oracle_stages)
    assert all(not checked.get("after_process_exit") for checked in result["acceptance"])


def test_frozen_source_mutation_is_rejected(tmp_path):
    source = source_freeze(tmp_path)
    verify_source(source)
    (source / "src/alysis_code/config.py").write_text("VERSION = 2\n")
    with pytest.raises(ValueError, match="source changed"):
        verify_source(source)


def test_replaced_source_and_self_consistent_manifest_still_fail_dispatch(tmp_path):
    path = plan_fixture(tmp_path)
    plan = load_plan(path)
    manifest = tmp_path / "frozen/evaluation-source.json"
    payload = json.loads(manifest.read_text())
    payload["created_at"] = "different freeze identity"
    dump(manifest, payload)
    verify_source(tmp_path / "frozen")
    with pytest.raises(ValueError, match="predeclared manifest hash"):
        run_attempt(path, plan["attempts"][0]["id"], execute_live=False)


def test_harness_hash_mismatch_stops_dispatch(tmp_path):
    path = plan_fixture(tmp_path)
    plan = load_plan(path)
    plan["harness_hashes"]["subagent_eval_subscription.py"] = "changed"
    dump(path, plan)
    with pytest.raises(ValueError, match="harness changed"):
        run_attempt(path, plan["attempts"][0]["id"], execute_live=False)


def test_redispatch_cannot_replace_a_completed_attempt(tmp_path):
    path = plan_fixture(tmp_path)
    plan = load_plan(path)
    attempt = plan["attempts"][0]["id"]
    directory = tmp_path / "campaign/attempts" / attempt
    directory.mkdir(parents=True)
    previous = {
        "attempt": attempt,
        "wall_seconds": 1,
        "evidence": {
            "unknown_usage": False,
            "known_usage": {"uncached_prompt_tokens": 10, "completion_tokens": 1},
        },
    }
    dump(directory / "result.json", previous)
    (directory / "console.txt").write_text("original evidence\n")
    with pytest.raises(FileExistsError):
        run_attempt(path, attempt, execute_live=True)
    assert json.loads((directory / "result.json").read_text()) == previous
    assert (directory / "console.txt").read_text() == "original evidence\n"
    assert not (tmp_path / "campaign/live-attempt.lock").exists()


def test_redispatch_cannot_replace_a_crashed_attempt(tmp_path):
    path = plan_fixture(tmp_path)
    plan = load_plan(path)
    attempt = plan["attempts"][0]["id"]
    directory = tmp_path / "campaign/attempts" / attempt
    directory.mkdir(parents=True)
    (directory / "console.txt").write_text("interrupted evidence\n")
    with pytest.raises(ValueError, match="no terminal result"):
        run_attempt(path, attempt, execute_live=True)
    assert not (directory / "result.json").exists()
    assert (directory / "console.txt").read_text() == "interrupted evidence\n"


@pytest.mark.parametrize("case_id", ["dev-quote-chat", "hold-stock-chat"])
def test_external_acceptance_rejects_unimplemented_fixture_and_preserves_dirty_state(
    tmp_path, case_id
):
    case = all_cases()[case_id]
    directory = tmp_path / "attempt"
    prepare_attempt(directory, case)
    result = acceptance(directory, sys.executable, 0)
    assert result["returncode"] != 0
    assert result["preservation"] == {
        "head_unchanged": True,
        "staged_state_unchanged": True,
        "dirty_files_unchanged": True,
        "readonly_files_unchanged": None,
    }
    assert not (directory / "workspace/acceptance.py").exists()


def test_acceptance_tampering_is_rejected_before_running(tmp_path):
    directory = tmp_path / "attempt"
    prepare_attempt(directory, all_cases()["dev-quote-chat"])
    (directory / "acceptance.py").write_text("raise SystemExit(0)\n")
    with pytest.raises(ValueError, match="acceptance was altered"):
        acceptance(directory, sys.executable, 0)


def test_readonly_content_mutation_is_reported_separately(tmp_path):
    directory = tmp_path / "attempt"
    prepare_attempt(directory, all_cases()["dev-request-log"])
    (directory / "workspace/relayboard/router.py").write_text("changed\n")
    result = acceptance(directory, sys.executable, 0)
    assert result["returncode"] is None
    assert result["preservation"]["readonly_files_unchanged"] is False


def test_all_frozen_fixture_python_and_oracles_compile():
    for case in all_cases().values():
        assert len(case.turns) == len(case.acceptance_stages)
        for relative, source in (case.files | case.dirty_files).items():
            if relative.endswith(".py"):
                compile(source, relative, "exec")
        if case.acceptance:
            compile(case.acceptance, case.id + "-acceptance.py", "exec")


def write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def evidence_fixture(tmp_path: Path, *, incomplete_usage: bool = False) -> Path:
    directory = tmp_path / "evidence"
    usage = {"prompt_tokens": 100, "cached_prompt_tokens": 60, "completion_tokens": 20}
    if incomplete_usage:
        del usage["cached_prompt_tokens"]
    write_records(
        directory / "data/sessions/root.jsonl",
        [
            {"type": "session_start", "payload": {"subagent_depth": 0}},
            {"type": "llm_usage", "payload": {**usage, "requested_model": "gpt-5.6-luna"}},
            {
                "type": "final",
                "payload": {"content": "Human-reviewed result", "private_context": "do not copy"},
            },
            {
                "type": "verify_run",
                "payload": {
                    "commands": ["python3 -m unittest"],
                    "all_passed": True,
                    "private_context": "do not copy",
                },
            },
        ],
    )
    write_records(
        directory / "data/sessions/child.jsonl",
        [
            {"type": "session_start", "payload": {"subagent_depth": 1}},
            {"type": "llm_usage", "payload": {**usage, "requested_model": "gpt-5.6-luna"}},
        ],
    )
    write_records(
        directory / "data/sessions/root/diagnostics/provider_telemetry.jsonl",
        [{"model": "gpt-5.6-luna", "usage": usage}],
    )
    write_records(
        directory / "diagnostics.jsonl",
        [
            {
                "event_type": "turn_finished",
                "session_id": "root",
                "payload": {
                    "exit_code": 0,
                    "failure_category": "transport",
                    "private_context": "do not copy",
                },
            }
        ],
    )
    return directory


def test_evidence_derives_uncached_and_optional_reasoning_does_not_pause(tmp_path):
    result = summarize_evidence(evidence_fixture(tmp_path))
    assert result["unknown_usage"] is False
    assert result["usage_calls"] == 1  # Child replay is already in the root log.
    assert result["known_usage"]["uncached_prompt_tokens"] == 40
    assert result["missing_usage_fields"]["reasoning_tokens"] == 1
    assert "private_context" not in json.dumps(result)


def test_missing_cost_breakdown_remains_unknown(tmp_path):
    result = summarize_evidence(evidence_fixture(tmp_path, incomplete_usage=True))
    assert result["unknown_usage"] is True
    assert result["missing_usage_fields"]["uncached_prompt_tokens"] == 1


def test_live_observed_token_threshold_counts_provider_once(tmp_path):
    directory = evidence_fixture(tmp_path)
    assert (
        observed_token_bound(directory, {"max_uncached_input_tokens": 39, "max_output_tokens": 100})
        == "max_uncached_input_tokens"
    )
    assert (
        observed_token_bound(
            directory, {"max_uncached_input_tokens": 100, "max_output_tokens": 100}
        )
        is None
    )


@pytest.mark.parametrize("missing_attempt", [False, True])
def test_retry_attempt_usage_replaces_logical_usage_once(tmp_path, missing_attempt):
    directory = evidence_fixture(tmp_path)
    path = directory / "data/sessions/root/diagnostics/provider_telemetry.jsonl"
    record = json.loads(path.read_text())
    record.update(
        attempt_count=2,
        attempt_usage_totals={
            "prompt_tokens": 100 if missing_attempt else 180,
            "cached_prompt_tokens": 60 if missing_attempt else 100,
            "input_tokens_uncached": 40 if missing_attempt else 80,
            "completion_tokens": 20 if missing_attempt else 27,
        },
        attempt_usage_missing_counts={
            field: int(missing_attempt)
            for field in (
                "prompt_tokens",
                "cached_prompt_tokens",
                "input_tokens_uncached",
                "completion_tokens",
            )
        },
    )
    write_records(path, [record])
    result = summarize_evidence(directory)
    assert result["known_usage_reconciles_with_provider"] is True
    assert result["unknown_usage"] is missing_attempt
    assert result["logical_response_usage"]["uncached_prompt_tokens"] == 40
    assert result["known_usage"]["uncached_prompt_tokens"] == (40 if missing_attempt else 80)
    assert result["additional_observed_attempt_usage"]["completion_tokens"] == (
        None if missing_attempt else 7
    )
    assert observed_token_bound(
        directory, {"max_uncached_input_tokens": 70, "max_output_tokens": 100}
    ) == (None if missing_attempt else "max_uncached_input_tokens")


def test_retry_totals_do_not_infer_uncached_from_different_observed_subsets(tmp_path):
    directory = evidence_fixture(tmp_path)
    path = directory / "data/sessions/root/diagnostics/provider_telemetry.jsonl"
    record = json.loads(path.read_text())
    record.update(
        attempt_count=2,
        attempt_usage_totals={
            "prompt_tokens": 200,
            "cached_prompt_tokens": 60,
            "completion_tokens": 20,
        },
        attempt_usage_missing_counts={
            "prompt_tokens": 0,
            "cached_prompt_tokens": 1,
            "completion_tokens": 1,
        },
    )
    write_records(path, [record])
    result = summarize_evidence(directory)
    assert result["unknown_usage"] is True
    assert result["known_usage"]["uncached_prompt_tokens"] == 0  # No known counter, not free usage.
    assert result["missing_usage_fields"]["uncached_prompt_tokens"] == 2
    assert result["additional_observed_attempt_usage"]["uncached_prompt_tokens"] is None


def test_child_provider_usage_is_counted_once_alongside_root_replay(tmp_path):
    directory = evidence_fixture(tmp_path)
    root = directory / "data/sessions/root.jsonl"
    events = [json.loads(line) for line in root.read_text().splitlines()]
    events.append(events[1])  # A completed child's logical usage replayed once.
    write_records(root, events)
    provider = json.loads(
        (directory / "data/sessions/root/diagnostics/provider_telemetry.jsonl").read_text()
    )
    write_records(
        directory / "data/sessions/child/diagnostics/provider_telemetry.jsonl", [provider]
    )
    result = summarize_evidence(directory)
    assert result["unknown_usage"] is False
    assert result["known_usage"]["uncached_prompt_tokens"] == 80
    assert result["known_usage"]["completion_tokens"] == 40
    assert result["usage_calls"] == 2


@pytest.mark.parametrize("unknown,missing", [(True, False), (False, True)])
def test_unknown_or_unfinished_attempt_pauses_batch(tmp_path, unknown, missing):
    directory = tmp_path / "attempts/earlier"
    directory.mkdir(parents=True)
    if not missing:
        dump(directory / "result.json", {"evidence": {"unknown_usage": unknown}})
    with pytest.raises(ValueError, match="paused"):
        check_budget({"output_dir": str(tmp_path), "bounds": DEFAULT_BOUNDS})


def test_batch_wall_reserves_whole_attempt(tmp_path):
    directory = tmp_path / "attempts/earlier"
    directory.mkdir(parents=True)
    dump(
        directory / "result.json",
        {
            "wall_seconds": 10200,
            "evidence": {
                "unknown_usage": False,
                "known_usage": {"uncached_prompt_tokens": 10, "completion_tokens": 1},
            },
        },
    )
    with pytest.raises(ValueError, match="cannot accommodate"):
        check_budget({"output_dir": str(tmp_path), "bounds": DEFAULT_BOUNDS})


def test_batch_wall_admission_includes_oracle_and_stop_grace(tmp_path):
    allowance = live_eval.POST_ATTEMPT_WALL_ALLOWANCE_S
    assert allowance == (
        live_eval.ACCEPTANCE_TIMEOUT_S + sum(wait for _, wait in live_eval.PROCESS_STOP_STAGES) + 5
    )
    bounds = dict(DEFAULT_BOUNDS)
    # The previous 30-second reserve admitted this insufficient wall budget.
    bounds["batch_wall_s"] = bounds["attempt_timeout_s"] + allowance - 1
    with pytest.raises(ValueError, match="cannot accommodate"):
        check_budget({"output_dir": str(tmp_path), "bounds": bounds})
    bounds["batch_wall_s"] += 1
    assert (
        check_budget({"output_dir": str(tmp_path), "bounds": bounds})["batch_wall_s"]
        == bounds["batch_wall_s"]
    )


def test_real_method_observer_keeps_result_exception_and_root_only_boundary(tmp_path):
    path = tmp_path / "turn-boundaries.jsonl"
    path.touch()

    class Session:
        subagent_depth = 0

        def run_turn(self, callback):
            assert len(completed_turns(tmp_path)) == (0 if callback == "return" else 1)
            if callback == "return":
                return 0
            raise RuntimeError("original failure")

    observe_root_turns(Session, path)
    session = Session()
    assert session.run_turn("return") == 0
    with pytest.raises(RuntimeError, match="original failure"):
        session.run_turn("raise")
    boundaries = completed_turns(tmp_path)
    assert [r["payload"]["reason"] for r in boundaries] == ["run_turn_returned", "run_turn_raised"]
    assert boundaries[1]["payload"]["exception_type"] == "RuntimeError"
    assert "original failure" not in path.read_text()
