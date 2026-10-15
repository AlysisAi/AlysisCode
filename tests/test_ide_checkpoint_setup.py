from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.ide import stdio_bridge
from alysis_code.ide.change_ledger import ChangeLedgerError


def _session(tmp_path: Path, ledger: object = None) -> SimpleNamespace:
    warnings: list[str] = []
    return SimpleNamespace(
        root=tmp_path,
        session_id="checkpoint-setup",
        change_ledger=ledger,
        surface=SimpleNamespace(emit_warning=warnings.append),
        warnings=warnings,
    )


@pytest.mark.parametrize("existing", [False, True])
def test_checkpoint_setup_failure_detaches_ledger_and_next_turn_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    class FailedLedger:
        def ensure_baseline(self, session_id: str) -> None:
            raise ChangeLedgerError("storage unavailable")

    failed = FailedLedger()
    session = _session(tmp_path, failed if existing else None)
    monkeypatch.setattr(stdio_bridge, "ChangeLedger", lambda root: failed)

    assert stdio_bridge._prepare_change_ledger_with_deadline(session) is False
    assert session.change_ledger is None
    assert session.warnings == [
        "Restore snapshots are unavailable; actions that may change files are paused: storage unavailable"
    ]

    baselined: list[str] = []
    healthy = SimpleNamespace(ensure_baseline=baselined.append)
    monkeypatch.setattr(stdio_bridge, "ChangeLedger", lambda root: healthy)

    assert stdio_bridge._prepare_change_ledger_with_deadline(session) is True
    assert session.change_ledger is healthy
    assert baselined == [session.session_id]


def test_checkpoint_setup_reuses_a_healthy_existing_ledger(tmp_path: Path) -> None:
    baselined: list[str] = []
    ledger = SimpleNamespace(ensure_baseline=baselined.append)
    session = _session(tmp_path, ledger)

    assert stdio_bridge._prepare_change_ledger_with_deadline(session) is True
    assert session.change_ledger is ledger
    assert baselined == [session.session_id]
    assert session.warnings == []


@pytest.mark.parametrize("existing", [False, True])
def test_timed_out_checkpoint_setup_is_adopted_only_by_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    release = threading.Event()
    finished = threading.Event()

    class SlowLedger:
        def ensure_baseline(self, session_id: str) -> None:
            try:
                assert release.wait(timeout=5)
            finally:
                finished.set()

    ledger = SlowLedger()
    session = _session(tmp_path, ledger if existing else None)
    monkeypatch.setattr(stdio_bridge, "ChangeLedger", lambda root: ledger)
    try:
        assert (
            stdio_bridge._prepare_change_ledger_with_deadline(session, deadline_seconds=0.01)
            is False
        )
        assert session.change_ledger is None
        assert len(session.warnings) == 1
        assert "Actions that may change files are paused" in session.warnings[0]
        pending = session.checkpoint_setup
        assert (
            stdio_bridge._prepare_change_ledger_with_deadline(session, deadline_seconds=0) is False
        )
        assert session.checkpoint_setup is pending
    finally:
        release.set()
        assert finished.wait(timeout=5)
    assert session.change_ledger is None
    assert stdio_bridge._prepare_change_ledger_with_deadline(session) is True
    assert session.change_ledger is ledger
    assert session.checkpoint_setup is None


def test_tool_checkpoint_guard_serializes_setup_and_checks_again_until_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    session = _session(tmp_path)
    session.agent_session = SimpleNamespace(
        store=SimpleNamespace(events_snapshot=lambda: [], append=lambda *args: None)
    )
    session.active_job = stdio_bridge.BridgeJob("job-1", session.session_id, "now")
    guard = stdio_bridge._CheckpointToolDispatchGuard({"session": session})
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def prepare(owner: object) -> bool:
        calls.append(session.active_job.job_id)
        started.set()
        assert release.wait(timeout=5)
        return False

    monkeypatch.setattr(stdio_bridge, "_prepare_change_ledger_with_deadline", prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(guard.check_tool_call, "shell", {})
        try:
            assert started.wait(timeout=5)
            second = pool.submit(guard.check_tool_call, "mcp_tool", {})
            assert not first.done()
            assert not second.done()
        finally:
            release.set()
        for result in (first, second):
            with pytest.raises(ChangeLedgerError, match="checkpoint_unavailable"):
                result.result(timeout=5)
    # One at a time, never in parallel; the second call checks again after the first.
    assert calls == ["job-1", "job-1"]
    with pytest.raises(ChangeLedgerError, match="checkpoint_unavailable"):
        guard.check_tool_call("file_edit", {})
    # Not ready is not cached: the worker keeps running and may have finished since.
    assert calls == ["job-1", "job-1", "job-1"]
    session.active_job = stdio_bridge.BridgeJob("job-2", session.session_id, "now")
    with pytest.raises(ChangeLedgerError, match="checkpoint_unavailable"):
        guard.check_tool_call("file_edit", {})
    assert calls == ["job-1", "job-1", "job-1", "job-2"]


def test_tool_checkpoint_guard_honors_cancellation_before_tool_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    session.agent_session = SimpleNamespace(
        store=SimpleNamespace(events_snapshot=lambda: [], append=lambda *args: None)
    )
    session.active_job = stdio_bridge.BridgeJob("job-1", session.session_id, "now")
    guard = stdio_bridge._CheckpointToolDispatchGuard({"session": session})

    def prepare(owner: object) -> bool:
        session.active_job.cancellation_event.set()
        return True

    monkeypatch.setattr(stdio_bridge, "_prepare_change_ledger_with_deadline", prepare)
    with pytest.raises(stdio_bridge.BridgeCancellationError):
        guard.check_tool_call("shell", {})


def test_checkpoint_wait_responds_to_cancellation_and_retains_single_worker(tmp_path, monkeypatch):
    session = _session(tmp_path)
    session.active_job = stdio_bridge.BridgeJob("job", session.session_id, "now")
    started = threading.Event()
    release = threading.Event()

    class Ledger:
        def ensure_baseline(self, session_id):
            started.set()
            release.wait(timeout=5)

    monkeypatch.setattr(stdio_bridge, "ChangeLedger", lambda root: Ledger())
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(stdio_bridge._prepare_change_ledger_with_deadline, session)
        try:
            assert started.wait(timeout=2)
            session.active_job.cancellation_event.set()
            with pytest.raises(stdio_bridge.BridgeCancellationError):
                result.result(timeout=1)
            assert session.change_ledger is None
            assert session.checkpoint_setup is not None
        finally:
            release.set()


def test_file_review_skips_snapshots_but_first_possible_edit_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    session.agent_session = SimpleNamespace(
        store=SimpleNamespace(events_snapshot=lambda: [], append=lambda *args: None)
    )
    session.active_job = stdio_bridge.BridgeJob("review-job", session.session_id, "now")
    guard = stdio_bridge._CheckpointToolDispatchGuard({"session": session})
    prepared: list[str] = []
    monkeypatch.setattr(
        stdio_bridge,
        "_prepare_change_ledger_with_deadline",
        lambda owner: prepared.append(owner.active_job.job_id) or True,
    )
    for tool in (
        "git_status",
        "git_diff",
        "fs_list",
        "fs_read",
        "fs_read_lines",
        "session_artifact_read",
    ):
        guard.check_tool_call(tool, {})
    assert prepared == []
    guard.check_tool_call("fs_edit", {})
    assert prepared == ["review-job"]
    guard.check_tool_call("shell_run", {})
    assert prepared == ["review-job"]
    session.active_job = stdio_bridge.BridgeJob("next-job", session.session_id, "now")
    guard.check_tool_call("unknown_plugin_tool", {})
    assert prepared == ["review-job", "next-job"]
    session.active_job.cancellation_event.set()
    with pytest.raises(stdio_bridge.BridgeCancellationError):
        guard.check_tool_call("fs_read", {})


def _guarded_session(tmp_path: Path) -> SimpleNamespace:
    session = _session(tmp_path)
    session.agent_session = SimpleNamespace(
        store=SimpleNamespace(events_snapshot=lambda: [], append=lambda *args: None)
    )
    session.active_job = stdio_bridge.BridgeJob("job-1", session.session_id, "now")
    return session


@pytest.mark.parametrize(
    "tool_name",
    ["search_rg", "symbol_search", "repo_map", "git_history", "history_search", "fs_read"],
)
def test_read_only_tools_never_wait_for_restore_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    session = _guarded_session(tmp_path)
    guard = stdio_bridge._CheckpointToolDispatchGuard({"session": session})

    def prepare(owner: object) -> bool:
        raise AssertionError(f"{tool_name} waited for restore points")

    monkeypatch.setattr(stdio_bridge, "_prepare_change_ledger_with_deadline", prepare)
    guard.check_tool_call(tool_name, {})


def test_tools_that_can_change_files_are_never_exempt() -> None:
    from alysis_code.tools.registry import get_builtin_tool_metadata

    for name in stdio_bridge._CHECKPOINT_FREE_READ_TOOLS:
        spec = get_builtin_tool_metadata(name)
        if spec is None:
            continue
        assert not {"write", "shell", "subagent", "verify"} & set(spec.categories), name


def test_a_change_after_the_baseline_finishes_proceeds_in_the_same_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _guarded_session(tmp_path)
    guard = stdio_bridge._CheckpointToolDispatchGuard({"session": session})
    release = threading.Event()

    class Ledger:
        def ensure_baseline(self, session_id: str) -> None:
            assert release.wait(timeout=5)

    ledger = Ledger()
    monkeypatch.setattr(stdio_bridge, "ChangeLedger", lambda root: ledger)
    prepare = stdio_bridge._prepare_change_ledger_with_deadline
    monkeypatch.setattr(
        stdio_bridge,
        "_prepare_change_ledger_with_deadline",
        lambda owner: prepare(owner, deadline_seconds=0.05),
    )
    with pytest.raises(ChangeLedgerError, match="checkpoint_unavailable"):
        guard.check_tool_call("fs_write", {})
    worker_done = session.checkpoint_setup[0]
    release.set()
    assert worker_done.wait(timeout=5)
    guard.check_tool_call("fs_write", {})
    assert session.change_ledger is ledger


def test_setup_runs_one_background_worker_that_a_later_wait_adopts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    started = threading.Event()
    release = threading.Event()
    ledgers: list[object] = []

    class Ledger:
        def __init__(self, root: Path) -> None:
            ledgers.append(self)

        def ensure_baseline(self, session_id: str) -> None:
            started.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(stdio_bridge, "ChangeLedger", Ledger)
    done, _ = stdio_bridge._start_change_ledger_setup(session)
    assert started.wait(timeout=2)
    assert not done.is_set()
    assert stdio_bridge._start_change_ledger_setup(session)[0] is done  # one worker only
    release.set()
    assert stdio_bridge._prepare_change_ledger_with_deadline(session) is True
    assert session.change_ledger is ledgers[0]
    assert len(ledgers) == 1


def test_a_slow_baseline_is_shown_as_an_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    activities: list[tuple[str, str]] = []
    session = _session(tmp_path)
    session.surface.emit_activity = lambda event: activities.append((event.operation, event.status))

    class Ledger:
        def ensure_baseline(self, session_id: str) -> None:
            threading.Event().wait(0.3)

    monkeypatch.setattr(stdio_bridge, "ChangeLedger", lambda root: Ledger())
    monkeypatch.setattr(stdio_bridge, "_CHANGE_LEDGER_SETUP_NOTICE_SECONDS", 0.05)
    assert stdio_bridge._prepare_change_ledger_with_deadline(session) is True
    assert activities == [
        ("prepare_restore_points", "running"),
        ("prepare_restore_points", "succeeded"),
    ]


def test_a_quick_baseline_shows_no_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    activities: list[object] = []
    session = _session(tmp_path)
    session.surface.emit_activity = activities.append
    monkeypatch.setattr(
        stdio_bridge, "ChangeLedger", lambda root: SimpleNamespace(ensure_baseline=lambda s: None)
    )
    assert stdio_bridge._prepare_change_ledger_with_deadline(session) is True
    assert activities == []
