from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from alysis_code.cli_run_deadline import finish_expired_managed_launch
from alysis_code.config import AppConfig, ConfigError
from alysis_code.managed_host_deadline import MANAGED_HOST_DEADLINE_UNIX_ENV


@pytest.fixture
def launch(tmp_path, monkeypatch):
    monkeypatch.setenv(MANAGED_HOST_DEADLINE_UNIX_ENV, str(time.time() - 1))
    monkeypatch.setenv("ALYSIS_TASK_OUTCOME_PATH", str(tmp_path / "host-outcome.json"))
    monkeypatch.delenv("ALYSIS_RUN_DEADLINE_SECONDS", raising=False)
    return dict(
        cfg=AppConfig(session_log_dir=str(tmp_path / "sessions")),
        root=tmp_path,
        require_run_deadline=True,
        one_shot_execution=True,
        run_deadline_seconds=10,
        no_log=False,
    )


def test_expired_launch_records_truthful_outcome_and_anchor(launch, tmp_path):
    assert finish_expired_managed_launch(**launch) == 0
    record = json.loads((tmp_path / "host-outcome.json").read_text())
    assert record["outcome"] == "deadline_exceeded"
    assert record["terminal"] is True
    assert record["verified_success"] is False
    assert record["material_paths"] == []
    logs = list((tmp_path / "sessions").glob("*.jsonl"))
    assert len(logs) == 1
    events = [json.loads(line) for line in logs[0].read_text().splitlines()]
    anchors = [e["payload"] for e in events if e["type"] == "managed_host_deadline_anchor"]
    assert len(anchors) == 1 and anchors[0]["status"] == "exhausted"
    assert anchors[0]["narrowed_by_seconds"] > 0
    assert [e["payload"] for e in events if e["type"] == "turn_outcome"] == [record]
    assert json.loads(logs[0].with_suffix(".outcome.json").read_text()) == record


@pytest.mark.parametrize("anchor", [None, "invalid", "nan", "inf", "-1", "0", "future"])
def test_other_anchor_states_keep_normal_runtime(launch, tmp_path, monkeypatch, anchor):
    if anchor is None:
        monkeypatch.delenv(MANAGED_HOST_DEADLINE_UNIX_ENV)
    else:
        monkeypatch.setenv(
            MANAGED_HOST_DEADLINE_UNIX_ENV,
            str(time.time() + 3600) if anchor == "future" else anchor,
        )
    assert finish_expired_managed_launch(**launch) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "changes",
    [
        {"require_run_deadline": False},
        {"one_shot_execution": False},
        {"no_run_deadline": True},
        {"execution_deadline": object()},
        {"run_deadline_seconds": None},
        {"session_id_override": "unadmitted"},
    ],
)
def test_unhandled_invocations_keep_normal_validation(launch, tmp_path, changes):
    assert finish_expired_managed_launch(**(launch | changes)) is None
    assert not list(tmp_path.iterdir())


def test_invalid_deadline_is_not_reported_as_expiration(launch, tmp_path):
    with pytest.raises(ConfigError):
        finish_expired_managed_launch(**(launch | {"run_deadline_seconds": -1}))
    assert not list(tmp_path.iterdir())


def test_no_log_does_not_create_session_artifacts(launch, tmp_path):
    assert finish_expired_managed_launch(**(launch | {"no_log": True})) == 0
    assert not (tmp_path / "sessions").exists()
    assert json.loads((tmp_path / "host-outcome.json").read_text())["verified_success"] is False


def test_log_admission_failure_does_not_emit_host_completion(launch, tmp_path):
    (tmp_path / "sessions").write_text("not a directory")
    with pytest.raises(ConfigError, match="Session log unavailable"):
        finish_expired_managed_launch(**launch)
    assert not (tmp_path / "host-outcome.json").exists()


def test_failed_log_write_does_not_suppress_host_deadline_receipt(launch, tmp_path, monkeypatch):
    from alysis_code.session_store import SessionStore

    def fail_append(*args, **kwargs):
        raise OSError("disk write failed after admission")

    monkeypatch.setattr(SessionStore, "append", fail_append)
    assert finish_expired_managed_launch(**launch) == 0
    record = json.loads((tmp_path / "host-outcome.json").read_text())
    assert record["outcome"] == "deadline_exceeded"
    assert record["verified_success"] is False


def test_named_session_uses_retained_capability(launch, tmp_path):
    from alysis_code.managed_session import prepare_managed_session_persistence

    persistence = prepare_managed_session_persistence(
        sessions_dir=tmp_path / "named", session_id="deadline-run"
    )
    persistence.seal()
    try:
        assert (
            finish_expired_managed_launch(
                **(
                    launch
                    | dict(
                        managed_session_persistence=persistence,
                        session_log_dir_override=persistence.sessions_dir,
                        session_id_override=persistence.session_id,
                    )
                )
            )
            == 0
        )
    finally:
        persistence.close()
    record = json.loads((tmp_path / "host-outcome.json").read_text())
    assert record["session_id"] == "deadline-run"
    assert (tmp_path / "named/deadline-run.jsonl").is_file()


def test_cli_boundary_finalizes_without_importing_agent_runtime(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env[MANAGED_HOST_DEADLINE_UNIX_ENV] = str(time.time() - 1)
    env["ALYSIS_TASK_OUTCOME_PATH"] = str(tmp_path / "outcome.json")
    code = f"""
import sys
from pathlib import Path
from alysis_code.config import AppConfig
from alysis_code.cli_impl.commands.cli_common import run_agent
assert 'alysis_code.agent_loop' not in sys.modules
assert run_agent(cfg=AppConfig(), root=Path({str(tmp_path)!r}),
                 require_run_deadline=True, one_shot_execution=True,
                 run_deadline_seconds=10, no_log=True) == 0
for name in ('alysis_code.agent_loop', 'alysis_code.agent.session',
             'alysis_code.agent.tools_assembly'):
    assert name not in sys.modules, name
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "outcome.json").read_text())["outcome"] == "deadline_exceeded"


def test_live_budget_forwards_original_invocation(launch, monkeypatch):
    from types import SimpleNamespace

    from alysis_code.cli_impl.commands import cli_common

    monkeypatch.setenv(MANAGED_HOST_DEADLINE_UNIX_ENV, str(time.time() + 3600))
    received = []

    def run_agent(**options):
        received.append(options)
        return 17

    monkeypatch.setattr(
        cli_common, "_agent_loop_module", lambda: SimpleNamespace(run_agent=run_agent)
    )
    assert cli_common.run_agent(**launch) == 17
    assert received == [launch]
