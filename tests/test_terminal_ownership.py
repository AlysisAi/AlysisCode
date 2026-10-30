from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import alysis_code.terminal_ownership as ownership_mod
from alysis_code.terminal_ownership import TerminalOwnershipLedger


def _tracked_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    child_pid: int = 4242,
) -> tuple[TerminalOwnershipLedger, Path, dict[str, object]]:
    owner_pid = os.getpid()

    def token(pid: int) -> str | None:
        return {owner_pid: "owner-token", child_pid: "child-token"}.get(pid)

    monkeypatch.setattr(ownership_mod, "_pid_start_token", token)
    ledger = TerminalOwnershipLedger(tmp_path)
    handle = ledger.track(
        child_pid=child_pid,
        termination_mode="direct",
        posix_pgid=None,
    )
    assert handle is not None
    payload = json.loads(handle.record_path.read_text(encoding="utf-8"))
    return ledger, handle.record_path, payload


def test_track_and_release_write_a_strict_private_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger, record, payload = _tracked_record(monkeypatch, tmp_path)

    assert payload["owner_pid"] == os.getpid()
    assert payload["child_pid"] == 4242
    assert payload["owner_start_token"] == "owner-token"
    assert payload["child_start_token"] == "child-token"
    if os.name != "nt":
        assert record.stat().st_mode & 0o777 == 0o600

    ledger.release(ownership_mod.TerminalOwnershipHandle(record))
    assert not record.exists()


def test_scavenger_preserves_records_owned_by_a_live_exact_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger, record, _payload = _tracked_record(monkeypatch, tmp_path)
    monkeypatch.setattr(ownership_mod, "_process_state", lambda _pid, _token: "matching")
    monkeypatch.setattr(
        ownership_mod,
        "_terminate_owned_process",
        lambda _payload: pytest.fail("live owner must not be terminated"),
    )

    results = ledger.scavenge_stale()

    assert [result.action for result in results] == ["preserved"]
    assert record.exists()


def test_scavenger_terminates_only_exact_child_of_dead_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger, record, payload = _tracked_record(monkeypatch, tmp_path)
    states = {
        int(payload["owner_pid"]): "dead",
        int(payload["child_pid"]): "matching",
    }
    monkeypatch.setattr(
        ownership_mod,
        "_process_state",
        lambda pid, _token: states[int(pid)],
    )
    terminated: list[int] = []
    monkeypatch.setattr(
        ownership_mod,
        "_terminate_owned_process",
        lambda candidate: terminated.append(int(candidate["child_pid"])) or True,
    )

    results = ledger.scavenge_stale()

    assert terminated == [4242]
    assert [result.action for result in results] == ["terminated"]
    assert not record.exists()


def test_scavenger_keeps_owned_record_after_transient_termination_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger, record, payload = _tracked_record(monkeypatch, tmp_path)
    states = {
        int(payload["owner_pid"]): "dead",
        int(payload["child_pid"]): "matching",
    }
    monkeypatch.setattr(
        ownership_mod,
        "_process_state",
        lambda pid, _token: states[int(pid)],
    )
    outcomes = iter((False, True))
    monkeypatch.setattr(
        ownership_mod,
        "_terminate_owned_process",
        lambda _payload: next(outcomes),
    )

    first_results = ledger.scavenge_stale()

    assert [result.action for result in first_results] == ["refused"]
    assert record.exists()

    retry_results = ledger.scavenge_stale()

    assert [result.action for result in retry_results] == ["terminated"]
    assert not record.exists()


def test_scavenger_accepts_confirmed_exit_between_dispatch_and_signal(monkeypatch, tmp_path):
    ledger, record, _ = _tracked_record(monkeypatch, tmp_path)
    states = iter(("dead", "matching", "dead"))
    monkeypatch.setattr(ownership_mod, "_process_state", lambda *_: next(states))
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("exited process must not be signalled"))
    results = ledger.scavenge_stale()
    assert [result.action for result in results] == ["terminated"]
    assert not record.exists()


@pytest.mark.parametrize(
    "exists_after,expected", [(False, "dead"), (True, "unknown"), (None, "unknown")]
)
def test_identity_query_race_requires_confirmed_absence(monkeypatch, exists_after, expected):
    existence = iter((True, exists_after))
    monkeypatch.setattr(ownership_mod, "_pid_exists", lambda _: next(existence))
    monkeypatch.setattr(ownership_mod, "_pid_start_token", lambda _: None)
    assert ownership_mod._process_state(4242, "creation-token") == expected


def test_scavenger_keeps_record_when_child_identity_is_temporarily_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger, record, payload = _tracked_record(monkeypatch, tmp_path)
    states = {
        int(payload["owner_pid"]): "dead",
        int(payload["child_pid"]): "unknown",
    }
    monkeypatch.setattr(
        ownership_mod,
        "_process_state",
        lambda pid, _token: states[int(pid)],
    )
    monkeypatch.setattr(
        ownership_mod,
        "_terminate_owned_process",
        lambda _payload: pytest.fail("an unverifiable PID must never be signalled"),
    )

    results = ledger.scavenge_stale()

    assert [result.action for result in results] == ["refused"]
    assert record.exists()


def test_scavenger_refuses_recycled_child_pid_without_signalling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger, record, payload = _tracked_record(monkeypatch, tmp_path)
    states = {
        int(payload["owner_pid"]): "dead",
        int(payload["child_pid"]): "reused",
    }
    monkeypatch.setattr(
        ownership_mod,
        "_process_state",
        lambda pid, _token: states[int(pid)],
    )
    monkeypatch.setattr(
        ownership_mod,
        "_terminate_owned_process",
        lambda _payload: pytest.fail("recycled PID must never be signalled"),
    )

    results = ledger.scavenge_stale()

    assert [result.action for result in results] == ["refused"]
    assert not record.exists()


def test_windows_taskkill_nonzero_is_success_when_exact_identity_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    identity_checks: list[tuple[int, str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 128)

    def state(pid: int, token: str) -> ownership_mod._ProcessState:
        identity_checks.append((pid, token))
        return "dead"

    monkeypatch.setattr(ownership_mod.subprocess, "run", run)
    monkeypatch.setattr(ownership_mod, "_process_state", state)

    terminated = ownership_mod._terminate_windows_process_tree(4242, "windows:123")

    assert terminated is True
    assert commands == [["taskkill", "/PID", "4242", "/T", "/F"]]
    assert identity_checks == [(4242, "windows:123")]


def test_windows_taskkill_zero_does_not_consume_still_live_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_checks: list[tuple[int, str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0)

    def state(pid: int, token: str) -> ownership_mod._ProcessState:
        identity_checks.append((pid, token))
        return "matching"

    monkeypatch.setattr(ownership_mod.subprocess, "run", run)
    monkeypatch.setattr(ownership_mod, "_process_state", state)
    monkeypatch.setattr(ownership_mod, "_TERMINATION_CONFIRM_TIMEOUT_S", 0.0)

    terminated = ownership_mod._terminate_windows_process_tree(4242, "windows:123")

    assert terminated is False
    assert identity_checks == [(4242, "windows:123")]


def test_next_process_scavenges_background_tree_after_hard_owner_exit(tmp_path: Path) -> None:
    ownership_dir = tmp_path / "owners"
    child_code = "import time; time.sleep(60)"
    child_command = (
        subprocess.list2cmdline([sys.executable, "-c", child_code])
        if os.name == "nt"
        else shlex.join([sys.executable, "-c", child_code])
    )
    helper = "\n".join(
        (
            "import os, sys",
            "from pathlib import Path",
            "from alysis_code.background_runner import HostBackgroundRunner",
            "from alysis_code.sandbox_settings import ShellSandboxSettings",
            "from alysis_code.terminal_manager import TerminalManager",
            "from alysis_code.terminal_ownership import TerminalOwnershipLedger",
            "ledger = TerminalOwnershipLedger(Path(sys.argv[1]))",
            "manager = TerminalManager(runner=HostBackgroundRunner(), settings=ShellSandboxSettings(), ownership_ledger=ledger)",
            "manager.start(cmd=sys.argv[2], cwd=Path.cwd(), root=Path.cwd())",
            "os._exit(0)",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", helper, os.fspath(ownership_dir), child_command],
        check=False,
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    assert completed.returncode == 0, completed.stderr
    deadline = time.monotonic() + 5.0
    records: list[Path] = []
    while time.monotonic() < deadline:
        records = list(ownership_dir.glob("*.json"))
        if records:
            break
        time.sleep(0.02)
    assert len(records) == 1
    payload = json.loads(records[0].read_text(encoding="utf-8"))

    try:
        results = TerminalOwnershipLedger(ownership_dir).scavenge_stale()
        assert [result.action for result in results] == ["terminated"]
        assert not records[0].exists()
    finally:
        # If an assertion fails before scavenging, still use the exact recorded identity and never
        # leave the test child running on the developer machine.
        if records[0].exists():
            ownership_mod._terminate_owned_process(payload)
            records[0].unlink(missing_ok=True)


def test_default_ownership_dir_follows_isolated_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALYSIS_TERMINAL_OWNERSHIP_DIR", raising=False)
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))

    ledger = TerminalOwnershipLedger()

    assert ledger.state_dir == (tmp_path / "data" / "terminal-processes").resolve()
