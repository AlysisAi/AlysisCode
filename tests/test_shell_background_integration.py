from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent_loop import AgentRuntimeError, create_session
from alysis_code.background_runner import BackgroundProcessSpawn
from alysis_code.config import AppConfig, ConfigError
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.surface import ApprovalDecision, ApprovalRequest, NoopSurface


class FakeBackgroundPopen:
    def __init__(
        self,
        *,
        stdout_text: str = "",
        stderr_text: str = "",
        auto_exit: bool = False,
    ) -> None:
        self.pid = 0
        self.returncode: int | None = 0 if auto_exit else None
        self._exited = threading.Event()
        if auto_exit:
            self._exited.set()
        stdout_r, stdout_w = os.pipe()
        stderr_r, stderr_w = os.pipe()
        os.write(stdout_w, stdout_text.encode("utf-8"))
        os.write(stderr_w, stderr_text.encode("utf-8"))
        os.close(stdout_w)
        os.close(stderr_w)
        self.stdout = os.fdopen(stdout_r, "rb", buffering=0)
        self.stderr = os.fdopen(stderr_r, "rb", buffering=0)
        self.terminate_calls = 0
        self.kill_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        if timeout is None:
            self._exited.wait()
        elif not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(cmd="fake-background", timeout=timeout)
        return self.returncode if self.returncode is not None else 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.returncode is None:
            self.returncode = -15
        self._exited.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self._exited.set()


class FakeBackgroundRunner:
    def __init__(
        self,
        *,
        stdout_text: str = "",
        stderr_text: str = "",
        auto_exit: bool = False,
        order: list[str] | None = None,
    ) -> None:
        self.stdout_text = stdout_text
        self.stderr_text = stderr_text
        self.auto_exit = auto_exit
        self.order = order
        self.calls: list[dict[str, object]] = []
        self.popens: list[FakeBackgroundPopen] = []
        self.cleanup_calls = 0

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        if self.order is not None:
            self.order.append("spawn")
        self.calls.append({"root": root, "cwd": cwd, "cmd": cmd})
        popen = FakeBackgroundPopen(
            stdout_text=self.stdout_text,
            stderr_text=self.stderr_text,
            auto_exit=self.auto_exit,
        )
        self.popens.append(popen)
        return BackgroundProcessSpawn(
            popen=popen,  # type: ignore[arg-type]
            cleanup=self._cleanup,
            started_argv=(cmd,),
            termination_mode="direct",
        )

    def _cleanup(self) -> None:
        self.cleanup_calls += 1


class FakeShellRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append({"root": root, "cwd": cwd, "cmd": cmd, "timeout_s": timeout_s})
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="sync ok\n",
            stderr="",
        )


class RecordingSurface(NoopSurface):
    host_managed_approvals = True

    def __init__(self, *, allow: bool = True, order: list[str] | None = None) -> None:
        self.allow = allow
        self.order = order
        self.requests: list[ApprovalRequest] = []
        self.warnings: list[str] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        if self.order is not None:
            self.order.append("approval")
        self.requests.append(request)
        return ApprovalDecision(allow=self.allow)

    def on_warning(self, warning: str) -> None:
        self.warnings.append(warning)


class RecordingMcpManager:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.closed = False

    def close(self) -> None:
        self.order.append("mcp_close")
        self.closed = True


def _cfg(*, background_max_concurrent: int | None = None) -> AppConfig:
    cfg = AppConfig(model="test-model")
    shell_sandbox: dict[str, object] = {"mode": "strict", "backend": "auto"}
    if background_max_concurrent is not None:
        shell_sandbox["background_max_concurrent"] = background_max_concurrent
    cfg.extra_fields = {"shell_sandbox": shell_sandbox}
    return cfg


def _session_with_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: FakeBackgroundRunner,
    *,
    cfg: AppConfig | None = None,
    mode: str = "auto",
    yes: bool = True,
    non_interactive: bool = True,
    surface: RecordingSurface | None = None,
    execution_deadline: ExecutionDeadline | None = None,
):
    def fake_build_background_shell_runner_from_settings(*_args: object, **_kwargs: object):
        return runner

    monkeypatch.setattr(
        agent_loop_mod,
        "build_background_shell_runner_from_settings",
        fake_build_background_shell_runner_from_settings,
    )
    return create_session(
        cfg=cfg or _cfg(),
        root=tmp_path,
        mode=mode,
        yes=yes,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=non_interactive,
        surface=surface if surface is not None else RecordingSurface(allow=True),
        execution_deadline=execution_deadline,
    )


def _wait_for_process_exit(session: Any, process_id: str) -> None:
    assert session.terminal_manager is not None
    process = session.terminal_manager._processes[process_id]
    assert process.wait_for_exit(1.0)


def test_shell_background_starts_and_returns_process_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeBackgroundRunner()
    session = _session_with_runner(monkeypatch, tmp_path, runner)
    try:
        result = session.tools["shell_background"].run({"cmd": "echo hi"})

        assert result["process_id"]
        assert result["lifetime"] == "session"
        assert result["status"] == "running"
        assert result["lines"] == []
        assert "next_seq" in result
        assert runner.calls[0]["cmd"] == "echo hi"
    finally:
        session.close()


def test_shell_background_deadline_denial_is_warning_not_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0.0,
        deadline_monotonic=4.0,
        configured_duration_seconds=4.0,
        clock=lambda: 3.5,
    )
    runner = FakeBackgroundRunner()
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        runner,
        execution_deadline=deadline,
    )
    try:
        result = session.tools["shell_background"].run({"cmd": "echo hi"})

        assert result["process_id"]
        assert result["lifetime"] == "session"
        assert result["deadline_prevented_launch"] is False
        assert "error" not in result
        assert "deadline_warning" in result
        assert result["deadline_start_decision"]["allowed"] is False
        assert result["deadline_start_decision"]["reason"] == "finalization_disallows_operation"
        assert runner.calls[0]["cmd"] == "echo hi"
    finally:
        session.close()


def test_shell_background_blocked_in_readonly_mode(tmp_path: Path) -> None:
    session = create_session(
        cfg=_cfg(),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=True,
    )
    try:
        assert "shell_background" not in session.tools
    finally:
        session.close()


def test_shell_background_blocked_by_dangerous_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeBackgroundRunner()
    session = _session_with_runner(monkeypatch, tmp_path, runner)
    try:
        with pytest.raises(AgentRuntimeError, match="Blocked command"):
            session.tools["shell_background"].run({"cmd": "mkfs.ext4 /dev/sda"})
        assert runner.calls == []
    finally:
        session.close()


def test_shell_run_denylist_message_preserved_for_non_fullaccess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(monkeypatch, tmp_path, FakeBackgroundRunner())
    try:
        with pytest.raises(
            AgentRuntimeError,
            match="Blocked fullaccess shell command by denylist pattern",
        ):
            session.tools["shell_run"].run({"cmd": "mkfs.ext4 /dev/sda"})
    finally:
        session.close()


def test_shell_background_review_mode_requires_approval_for_sensitive_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    surface = RecordingSurface(allow=True, order=order)
    runner = FakeBackgroundRunner(order=order)
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        runner,
        mode="review",
        yes=False,
        non_interactive=False,
        surface=surface,
    )
    try:
        session.tools["shell_background"].run({"cmd": "git push origin main"})

        assert [request.kind for request in surface.requests] == ["shell_background"]
        assert order == ["approval", "spawn"]
    finally:
        session.close()


def test_shell_run_approval_kind_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    surface = RecordingSurface(allow=True)
    shell_runner = FakeShellRunner()

    def build_shell_from_settings(*_args: object, **_kwargs: object) -> FakeShellRunner:
        return shell_runner

    monkeypatch.setattr(
        agent_loop_mod,
        "build_shell_runner_from_settings",
        build_shell_from_settings,
    )
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        FakeBackgroundRunner(),
        mode="review",
        yes=False,
        non_interactive=False,
        surface=surface,
    )
    try:
        session.tools["shell_run"].run({"cmd": "make build"})

        assert [request.kind for request in surface.requests] == ["shell_run"]
        assert shell_runner.calls[0]["cmd"] == "make build"
    finally:
        session.close()


def test_shell_background_fullaccess_denylist_message_in_non_fullaccess_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeBackgroundRunner()
    session = _session_with_runner(monkeypatch, tmp_path, runner)
    try:
        with pytest.raises(AgentRuntimeError, match="Blocked command: denylist pattern"):
            session.tools["shell_background"].run({"cmd": "sudo ls"})
        assert runner.calls == []
    finally:
        session.close()


def test_shell_background_cwd_escape_translated_to_agent_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeBackgroundRunner()
    session = _session_with_runner(monkeypatch, tmp_path, runner)
    try:
        with pytest.raises(AgentRuntimeError, match="Path escapes root"):
            session.tools["shell_background"].run({"cmd": "echo hi", "cwd": "../outside"})
    finally:
        session.close()


def test_terminal_limit_translated_to_agent_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeBackgroundRunner()
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        runner,
        cfg=_cfg(background_max_concurrent=1),
    )
    try:
        session.tools["shell_background"].run({"cmd": "sleep 60"})
        with pytest.raises(AgentRuntimeError, match="Maximum background process count reached"):
            session.tools["shell_background"].run({"cmd": "sleep 60"})
    finally:
        session.close()


def test_shell_background_start_config_error_translated_to_agent_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_build_background_shell_runner_from_settings(*_args: object, **_kwargs: object):
        raise ConfigError("strict mode is enabled, but no usable backend is available")

    monkeypatch.setattr(
        agent_loop_mod,
        "build_background_shell_runner_from_settings",
        fail_build_background_shell_runner_from_settings,
    )
    session = create_session(
        cfg=_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=True,
    )
    try:
        with pytest.raises(AgentRuntimeError, match="Failed to start background process"):
            session.tools["shell_background"].run({"cmd": "echo hi"})
    finally:
        session.close()


def test_shell_background_disabled_runner_error_translated_to_agent_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DisabledRunner:
        def start(
            self,
            *,
            root: Path,
            cwd: Path,
            cmd: str,
            env_overrides: dict[str, str] | None = None,
        ) -> BackgroundProcessSpawn:
            raise RuntimeError("background shell unavailable")

    def build_disabled_background_runner(*_args: object, **_kwargs: object) -> DisabledRunner:
        return DisabledRunner()

    monkeypatch.setattr(
        agent_loop_mod,
        "build_background_shell_runner_from_settings",
        build_disabled_background_runner,
    )
    session = create_session(
        cfg=_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=True,
    )
    try:
        with pytest.raises(AgentRuntimeError, match="Failed to start background process"):
            session.tools["shell_background"].run({"cmd": "echo hi"})
    finally:
        session.close()


def test_shell_output_reads_snapshot_with_since(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeBackgroundRunner(stdout_text="one\ntwo\n", auto_exit=True)
    session = _session_with_runner(monkeypatch, tmp_path, runner)
    try:
        started = session.tools["shell_background"].run({"cmd": "emit output"})
        process_id = str(started["process_id"])
        _wait_for_process_exit(session, process_id)

        first = session.tools["shell_output"].run({"process_id": process_id, "since": 0})
        second = session.tools["shell_output"].run(
            {"process_id": process_id, "since": first["next_seq"]}
        )

        assert [line["text"] for line in first["lines"]] == ["one\n", "two\n"]
        assert second["lines"] == []
    finally:
        session.close()


def test_shell_output_unknown_process_id_returns_recovery_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(monkeypatch, tmp_path, FakeBackgroundRunner())
    try:
        result = session.tools["shell_output"].run({"process_id": "missing"})

        assert result["status"] == "unknown_process_id"
        assert result["unknown_process_id"] is True
        assert result["requested_process_id"] == "missing"
        assert result["known_process_ids"] == []
        assert result["recovery"]["recommended_tool"] == "shell_list"
        assert "tool_call_id" in result["recovery"]["reason"]
        assert any(
            event["type"] == "bg_unknown_process"
            and event["payload"]["operation"] == "shell_output"
            and event["payload"]["process_id"] == "missing"
            for event in session.store.events_snapshot()
        )
    finally:
        session.close()


def test_shell_output_negative_since_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(monkeypatch, tmp_path, FakeBackgroundRunner())
    try:
        with pytest.raises(AgentRuntimeError, match="since must be non-negative"):
            session.tools["shell_output"].run({"process_id": "missing", "since": -1})
    finally:
        session.close()


def test_shell_kill_terminates_and_returns_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeBackgroundRunner()
    session = _session_with_runner(monkeypatch, tmp_path, runner)
    try:
        started = session.tools["shell_background"].run({"cmd": "sleep 60"})
        process_id = str(started["process_id"])

        killed = session.tools["shell_kill"].run({"process_id": process_id})

        assert killed["status"] == "killed"
        assert runner.popens[0].terminate_calls == 1
        assert any(
            event["type"] == "bg_kill"
            and event["payload"]["process_id"] == process_id
            and event["payload"]["status"] == "killed"
            for event in session.store.events_snapshot()
        )
    finally:
        session.close()


def test_shell_kill_unknown_process_id_raises_agent_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(monkeypatch, tmp_path, FakeBackgroundRunner())
    try:
        with pytest.raises(AgentRuntimeError, match="Unknown background process_id"):
            session.tools["shell_kill"].run({"process_id": "missing"})
    finally:
        session.close()


def test_shell_background_fullaccess_writes_shell_audit_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        FakeBackgroundRunner(),
        mode="fullaccess",
    )
    try:
        session.tools["shell_background"].run({"cmd": "python -m http.server 8000"})

        audit_events = [
            event
            for event in session.store.events_snapshot()
            if event["type"] == "fullaccess_shell"
        ]
        assert len(audit_events) == 1
        payload = audit_events[0]["payload"]
        assert set(payload) == {
            "event",
            "ts",
            "command",
            "cwd",
            "exit_code",
            "duration_ms",
            "mode",
        }
        assert payload["event"] == "fullaccess_shell"
        assert payload["command"] == "python -m http.server 8000"
        assert payload["cwd"] == str(tmp_path.resolve())
        assert payload["exit_code"] == -1
        assert payload["mode"] == "fullaccess"
        assert isinstance(payload["duration_ms"], int)
        assert payload["duration_ms"] >= 0
    finally:
        session.close()


def test_shell_list_returns_summaries_for_active_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(monkeypatch, tmp_path, FakeBackgroundRunner())
    try:
        session.tools["shell_background"].run({"cmd": "sleep 1"})
        session.tools["shell_background"].run({"cmd": "sleep 2"})

        listed = session.tools["shell_list"].run({})

        assert len(listed["processes"]) == 2
        for process in listed["processes"]:
            assert process["process_id"]
            assert process["cmd"].startswith("sleep ")
            assert process["status"] == "running"
            assert isinstance(process["runtime_s"], float)
    finally:
        session.close()


def test_shell_and_background_runners_use_same_resolved_sandbox_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sync_settings: list[object] = []
    background_settings: list[object] = []
    shell_runner = FakeShellRunner()
    background_runner = FakeBackgroundRunner()

    def build_shell_from_settings(settings: object, *_args: object, **_kwargs: object):
        sync_settings.append(settings)
        return shell_runner

    def build_background_from_settings(settings: object, *_args: object, **_kwargs: object):
        background_settings.append(settings)
        return background_runner

    monkeypatch.setattr(
        agent_loop_mod,
        "build_shell_runner_from_settings",
        build_shell_from_settings,
    )
    monkeypatch.setattr(
        agent_loop_mod,
        "build_background_shell_runner_from_settings",
        build_background_from_settings,
    )
    session = create_session(
        cfg=_cfg(background_max_concurrent=3),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=True,
    )
    try:
        session.tools["shell_run"].run({"cmd": "echo sync"})
        session.tools["shell_background"].run({"cmd": "echo async"})

        assert len(sync_settings) == 1
        assert len(background_settings) == 1
        assert sync_settings[0] is background_settings[0]
        assert shell_runner.calls[0]["cmd"] == "echo sync"
        assert background_runner.calls[0]["cmd"] == "echo async"
    finally:
        session.close()


def test_shell_list_empty_when_no_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(monkeypatch, tmp_path, FakeBackgroundRunner())
    try:
        assert session.tools["shell_list"].run({}) == {"processes": []}
    finally:
        session.close()


def test_shell_list_blocked_in_readonly_mode(tmp_path: Path) -> None:
    session = create_session(
        cfg=_cfg(),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=True,
    )
    try:
        assert "shell_list" not in session.tools
    finally:
        session.close()


def test_session_close_shuts_down_background_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    runner = FakeBackgroundRunner()
    session = _session_with_runner(monkeypatch, tmp_path, runner)
    assert session.terminal_manager is not None
    original_shutdown_all = session.terminal_manager.shutdown_all

    def tracking_shutdown_all(*, kill_timeout_s: float | None = None) -> None:
        order.append("terminal_shutdown")
        original_shutdown_all(kill_timeout_s=kill_timeout_s)

    session.terminal_manager.shutdown_all = tracking_shutdown_all  # type: ignore[method-assign]
    session.mcp_manager = RecordingMcpManager(order)  # type: ignore[assignment]
    session.tools["shell_background"].run({"cmd": "sleep 1"})
    session.tools["shell_background"].run({"cmd": "sleep 2"})

    session.close()

    assert order == ["terminal_shutdown", "mcp_close"]


def test_session_close_swallows_terminal_shutdown_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    surface = RecordingSurface()
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        FakeBackgroundRunner(),
        surface=surface,
    )
    assert session.terminal_manager is not None

    def failing_shutdown_all(*, kill_timeout_s: float | None = None) -> None:
        raise RuntimeError("shutdown boom")

    session.terminal_manager.shutdown_all = failing_shutdown_all  # type: ignore[method-assign]
    mcp_manager = RecordingMcpManager(order)
    session.mcp_manager = mcp_manager  # type: ignore[assignment]

    session.close()

    assert mcp_manager.closed is True
    assert any("Terminal manager shutdown failed" in warning for warning in surface.warnings)
    assert any(
        event["type"] == "warning" and event["payload"]["warning"] == "terminal_shutdown_failed"
        for event in session.store.events_snapshot()
    )


def test_strict_mode_defers_background_runner_construction_until_first_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    call_count = 0
    runner = FakeBackgroundRunner()

    def fake_build_background_shell_runner_from_settings(*_args: object, **_kwargs: object):
        nonlocal call_count
        call_count += 1
        return runner

    monkeypatch.setattr(
        agent_loop_mod,
        "build_background_shell_runner_from_settings",
        fake_build_background_shell_runner_from_settings,
    )
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=True,
    )
    try:
        assert call_count == 0
        session.tools["shell_background"].run({"cmd": "echo hi"})
        assert call_count == 1
    finally:
        session.close()
