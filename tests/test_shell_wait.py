from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent_loop import create_session
from alysis_code.background_runner import BackgroundProcessSpawn, HostBackgroundRunner
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.terminal_manager import TerminalManager


class _BackgroundFixtureSurface(NoopSurface):
    def request_approval(self, request):
        return ApprovalDecision(request.kind == "shell_background")


class _FakeBackgroundPopen:
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
        if stdout_text:
            os.write(stdout_w, stdout_text.encode("utf-8"))
        if stderr_text:
            os.write(stderr_w, stderr_text.encode("utf-8"))
        os.close(stdout_w)
        os.close(stderr_w)
        self.stdout = os.fdopen(stdout_r, "rb", buffering=0)
        self.stderr = os.fdopen(stderr_r, "rb", buffering=0)

    def wait(self, timeout: float | None = None) -> int:
        if timeout is None:
            self._exited.wait()
        elif not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(cmd="fake-background", timeout=timeout)
        return self.returncode if self.returncode is not None else 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = -15
        self._exited.set()

    def kill(self) -> None:
        self.returncode = -9
        self._exited.set()


class _FakeBackgroundRunner:
    def __init__(self, *, stdout_text: str = "", auto_exit: bool = False) -> None:
        self.stdout_text = stdout_text
        self.auto_exit = auto_exit

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        _ = root, cwd, env_overrides
        popen = _FakeBackgroundPopen(stdout_text=self.stdout_text, auto_exit=self.auto_exit)
        return BackgroundProcessSpawn(
            popen=popen,  # type: ignore[arg-type]
            cleanup=lambda: None,
            started_argv=(cmd,),
            termination_mode="direct",
        )


def _shell_join(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _python_cmd(code: str) -> str:
    return _shell_join([sys.executable, "-c", code])


def _cfg() -> AppConfig:
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {"shell_sandbox": {"mode": "strict", "backend": "auto"}}
    return cfg


def _session_with_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: _FakeBackgroundRunner,
    *,
    deadline: ExecutionDeadline | None = None,
    runtime_kind: RuntimeKind = RuntimeKind.ONE_SHOT,
):
    monkeypatch.setattr(
        agent_loop_mod,
        "build_background_shell_runner_from_settings",
        lambda *_args, **_kwargs: runner,
    )
    return create_session(
        cfg=_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=False,
        surface=_BackgroundFixtureSurface(),
        execution_deadline=deadline,
        runtime_kind=runtime_kind,
    )


def test_terminal_manager_wait_returns_on_new_output(tmp_path: Path) -> None:
    manager = TerminalManager(runner=HostBackgroundRunner(), settings=ShellSandboxSettings())
    process_id = manager.start(
        cmd=_python_cmd("import time; time.sleep(0.1); print('ready', flush=True); time.sleep(1)"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        snapshot, timed_out = manager.wait_for_output(
            process_id,
            since=0,
            timeout_s=2.0,
            until="output_available",
        )

        assert timed_out is False
        assert [line.text.rstrip() for line in snapshot.lines] == ["ready"]
    finally:
        manager.shutdown_all(kill_timeout_s=0.2)


def test_terminal_manager_wait_returns_when_process_exits(tmp_path: Path) -> None:
    manager = TerminalManager(runner=HostBackgroundRunner(), settings=ShellSandboxSettings())
    process_id = manager.start(
        cmd=_python_cmd("import time; time.sleep(0.05)"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        snapshot, timed_out = manager.wait_for_output(
            process_id,
            since=0,
            timeout_s=2.0,
            until="process_exited",
        )

        assert timed_out is False
        assert snapshot.status == "exited"
    finally:
        manager.shutdown_all(kill_timeout_s=0.2)


def test_terminal_manager_wait_quiet_process_times_out_cleanly(tmp_path: Path) -> None:
    manager = TerminalManager(runner=HostBackgroundRunner(), settings=ShellSandboxSettings())
    process_id = manager.start(
        cmd=_python_cmd("import time; time.sleep(2)"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        snapshot, timed_out = manager.wait_for_output(
            process_id,
            since=0,
            timeout_s=0.05,
            until="output_available",
        )

        assert timed_out is True
        assert snapshot.lines == ()
        assert snapshot.status == "running"
    finally:
        manager.shutdown_all(kill_timeout_s=0.2)


def test_shell_wait_tool_returns_output_and_preserves_cursor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        _FakeBackgroundRunner(stdout_text="ready\n", auto_exit=False),
    )
    try:
        started = session.tools["shell_background"].run({"cmd": "fake"})
        result = session.tools["shell_wait"].run(
            {
                "process_id": started["process_id"],
                "since": 0,
                "until": "output_available",
                "wait_seconds": 1,
            }
        )

        assert result["waited"] is True
        assert result["timed_out"] is False
        assert [line["text"] for line in result["lines"]] == ["ready\n"]
        assert result["next_seq"] >= 1
        quiet = session.tools["shell_wait"].run(
            {"process_id": started["process_id"], "wait_seconds": 0.02}
        )
        assert quiet["lines"] == []
        assert quiet["timed_out"] is True
        assert quiet["meaningful_progress"] is False
        assert quiet["wake_reason"] == "interval_elapsed"
        assert quiet["since"] == result["next_seq"]
        replay = session.tools["shell_wait"].run(
            {"process_id": started["process_id"], "since": 0, "wait_seconds": 0}
        )
        assert [line["text"] for line in replay["lines"]] == ["ready\n"]
    finally:
        session.close()


def test_shell_wait_unknown_process_id_returns_recovery_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(monkeypatch, tmp_path, _FakeBackgroundRunner(auto_exit=False))
    try:
        started = session.tools["shell_background"].run({"cmd": "fake"})
        result = session.tools["shell_wait"].run(
            {
                "process_id": "missing",
                "since": 0,
                "until": "either",
                "wait_seconds": 1,
            }
        )

        assert result["status"] == "unknown_process_id"
        assert result["unknown_process_id"] is True
        assert result["requested_process_id"] == "missing"
        assert result["known_process_ids"] == [started["process_id"]]
        assert result["known_processes"][0]["process_id"] == started["process_id"]
        assert result["waited"] is False
        assert result["wait_seconds_effective"] == 0.0
        assert result["recovery"]["recommended_tool"] == "shell_list"
    finally:
        session.close()


def test_shell_wait_is_available_in_interactive_chat_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        _FakeBackgroundRunner(stdout_text="chat-ready\n", auto_exit=False),
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        assert "shell_wait" in session.tools
        started = session.tools["shell_background"].run({"cmd": "fake"})
        result = session.tools["shell_wait"].run(
            {
                "process_id": started["process_id"],
                "since": 0,
                "until": "output_available",
                "wait_seconds": 1,
            }
        )

        assert result["waited"] is True
        assert result["timed_out"] is False
        assert [line["text"] for line in result["lines"]] == ["chat-ready\n"]
    finally:
        session.close()


def test_shell_wait_deadline_clamps_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=10.0,
        deadline_monotonic=11.0,
        configured_duration_seconds=1.0,
        clock=lambda: 10.0,
    )
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        _FakeBackgroundRunner(auto_exit=False),
        deadline=deadline,
    )
    try:
        started = session.tools["shell_background"].run({"cmd": "fake"})
        result = session.tools["shell_wait"].run(
            {
                "process_id": started["process_id"],
                "since": 0,
                "until": "either",
                "wait_seconds": 30,
            }
        )

        assert result["timed_out"] is True
        assert result["wait_seconds_effective"] == 0.0
        assert result["deadline_clamped"] is True
    finally:
        session.close()


def test_shell_wait_accepts_long_wait_and_returns_early_on_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A large wait_seconds must be accepted (not rejected, and not silently
    # capped to the old 60s ceiling) and must still return the instant the
    # process exits, so a slow build costs a single wait rather than a poll
    # loop. auto_exit makes the fake report "exited" immediately, so a 600s
    # request returns at once and the call cannot actually block the test.
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        _FakeBackgroundRunner(stdout_text="done\n", auto_exit=True),
    )
    try:
        started = session.tools["shell_background"].run({"cmd": "fake"})
        result = session.tools["shell_wait"].run(
            {
                "process_id": started["process_id"],
                "since": 0,
                "until": "process_exited",
                "wait_seconds": 600,
            }
        )

        assert result["timed_out"] is False
        assert result["status"] == "exited"
        # No deadline in this session, so the full request survives coercion
        # (the old ceiling would have reported 60.0 here).
        assert result["wait_seconds_requested"] == 600
        assert result["wait_seconds_effective"] == 600.0
    finally:
        session.close()


def test_shell_wait_seconds_capped_at_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Above the ceiling the request is capped, not rejected: the tool stays
    # usable and the wait is simply bounded.
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        _FakeBackgroundRunner(stdout_text="done\n", auto_exit=True),
    )
    try:
        started = session.tools["shell_background"].run({"cmd": "fake"})
        result = session.tools["shell_wait"].run(
            {
                "process_id": started["process_id"],
                "since": 0,
                "until": "process_exited",
                "wait_seconds": 5000,
            }
        )

        assert result["wait_seconds_effective"] == 900.0
    finally:
        session.close()


def test_shell_output_existing_callers_remain_immediate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(
        monkeypatch,
        tmp_path,
        _FakeBackgroundRunner(stdout_text="one\n", auto_exit=False),
    )
    try:
        started = session.tools["shell_background"].run({"cmd": "fake"})
        # Let the fixture's reader publish its output before checking the
        # immediate snapshot API, which deliberately does not wait for bytes.
        assert session.terminal_manager is not None
        _, timed_out = session.terminal_manager.wait_for_output(
            started["process_id"], since=0, timeout_s=2.0, until="output_available"
        )
        assert timed_out is False
        result = session.tools["shell_output"].run({"process_id": started["process_id"]})

        assert "waited" not in result
        assert [line["text"] for line in result["lines"]] == ["one\n"]
    finally:
        session.close()


def test_repeated_empty_shell_output_polls_produce_wait_guidance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session_with_runner(monkeypatch, tmp_path, _FakeBackgroundRunner())
    try:
        started = session.tools["shell_background"].run({"cmd": "fake"})
        first = session.tools["shell_output"].run({"process_id": started["process_id"], "since": 0})
        second = session.tools["shell_output"].run(
            {"process_id": started["process_id"], "since": 0}
        )

        assert first["empty_poll_count"] == 1
        assert second["empty_poll_count"] == 2
        assert second["wait_guidance"]["recommended_tool"] == "shell_wait"
        assert second["wait_guidance"]["suggested_arguments"]["process_id"] == started["process_id"]
    finally:
        session.close()
