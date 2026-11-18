from __future__ import annotations

import io
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

import alysis_code.cli_impl.chat as chat_impl_mod
from alysis_code import cli as cli_mod
from alysis_code.background_runner import BackgroundProcessSpawn
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.terminal_manager import TerminalManager


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
    ) -> None:
        self.stdout_text = stdout_text
        self.stderr_text = stderr_text
        self.auto_exit = auto_exit
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


def _manager(runner: FakeBackgroundRunner) -> TerminalManager:
    return TerminalManager(runner=runner, settings=ShellSandboxSettings())


def _session(
    *,
    tmp_path: Path,
    manager: TerminalManager | None,
    mode: str = "auto",
) -> SimpleNamespace:
    return SimpleNamespace(root=tmp_path, mode=mode, terminal_manager=manager)


def _run_chat_command(session: Any, command: str) -> tuple[str | object, str]:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=80)
    result = chat_impl_mod._handle_chat_command_impl(
        cli_mod,
        input_text=command,
        root=Path(getattr(session, "root", Path("."))),
        session=session,
        pending_images=[],
        console=console,
    )
    return result, buffer.getvalue()


def _start_process(manager: TerminalManager, tmp_path: Path, cmd: str) -> str:
    return manager.start(cmd=cmd, cwd=tmp_path, root=tmp_path)


def _wait_for_exit(manager: TerminalManager, process_id: str) -> None:
    process = manager._processes[process_id]
    assert process.wait_for_exit(1.0)


def test_terminals_empty_session_prints_empty_message(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner())
    try:
        result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager), "/terminals"
        )

        assert result == "handled"
        assert "No background processes." in output
    finally:
        manager.shutdown_all()


def test_terminals_list_renders_table_for_active_processes(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner())
    try:
        process_id = _start_process(manager, tmp_path, "python -m http.server")

        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            "/terminals list",
        )

        assert "Background Terminals" in output
        assert process_id in output
        assert "python -m http.server" in output
        assert "running" in output
    finally:
        manager.shutdown_all()


def test_terminals_list_renders_command_preview_as_literal_text(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner())
    try:
        _start_process(manager, tmp_path, "echo '[red]literal[/red]'")

        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            "/terminals list",
        )

        assert "[red]literal[/red]" in output
    finally:
        manager.shutdown_all()


def test_terminals_list_includes_terminated_processes(tmp_path: Path) -> None:
    runner = FakeBackgroundRunner(auto_exit=True)
    manager = _manager(runner)
    try:
        process_id = _start_process(manager, tmp_path, "echo done")
        _wait_for_exit(manager, process_id)

        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            "/terminals list",
        )

        assert process_id in output
        assert "exited" in output
    finally:
        manager.shutdown_all()


def test_terminals_show_prints_snapshot_lines(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner(stdout_text="one\ntwo\n", auto_exit=True))
    try:
        process_id = _start_process(manager, tmp_path, "emit output")
        _wait_for_exit(manager, process_id)

        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            f"/terminals show {process_id}",
        )

        assert f"process_id: {process_id}" in output
        assert "status: exited" in output
        assert "stdout: one" in output
        assert "stdout: two" in output
    finally:
        manager.shutdown_all()


def test_terminals_show_renders_output_as_literal_text(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner(stdout_text="[red]ERR[/red]\n", auto_exit=True))
    try:
        process_id = _start_process(manager, tmp_path, "emit markup-like output")
        _wait_for_exit(manager, process_id)

        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            f"/terminals show {process_id}",
        )

        assert "stdout: [red]ERR[/red]" in output
    finally:
        manager.shutdown_all()


def test_terminals_show_unknown_process_id_prints_error_does_not_raise(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner())
    try:
        result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            "/terminals show missing",
        )

        assert result == "handled"
        assert "No such background process: missing" in output
    finally:
        manager.shutdown_all()


def test_terminals_show_truncates_at_display_cap(tmp_path: Path) -> None:
    stdout = "".join(f"line {idx}\n" for idx in range(250))
    manager = _manager(FakeBackgroundRunner(stdout_text=stdout, auto_exit=True))
    try:
        process_id = _start_process(manager, tmp_path, "emit lots")
        _wait_for_exit(manager, process_id)

        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            f"/terminals show {process_id}",
        )

        assert "… (older lines truncated)" in output
        assert "stdout: line 49" not in output
        assert "stdout: line 50" in output
        assert "stdout: line 249" in output
    finally:
        manager.shutdown_all()


def test_terminals_kill_terminates_running_process(tmp_path: Path) -> None:
    runner = FakeBackgroundRunner()
    manager = _manager(runner)
    try:
        process_id = _start_process(manager, tmp_path, "sleep 60")

        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            f"/terminals kill {process_id}",
        )

        assert f"Killed {process_id}" in output
        assert "status=killed" in output
        assert runner.popens[0].terminate_calls == 1
        assert manager.read(process_id).status == "killed"
    finally:
        manager.shutdown_all()


def test_terminals_kill_unknown_process_id_prints_error(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner())
    try:
        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            "/terminals kill missing",
        )

        assert "No such background process: missing" in output
    finally:
        manager.shutdown_all()


def test_terminals_kill_blocked_in_readonly_mode_with_message(tmp_path: Path) -> None:
    runner = FakeBackgroundRunner()
    manager = _manager(runner)
    try:
        process_id = _start_process(manager, tmp_path, "sleep 60")

        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager, mode="readonly"),
            f"/terminals kill {process_id}",
        )

        assert "Cannot kill processes in readonly mode." in output
        assert manager.read(process_id).status == "running"
        assert runner.popens[0].terminate_calls == 0
    finally:
        manager.shutdown_all()


def test_terminals_list_works_in_readonly_mode(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner())
    try:
        process_id = _start_process(manager, tmp_path, "sleep 60")

        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager, mode="readonly"),
            "/terminals list",
        )

        assert process_id in output
        assert "running" in output
    finally:
        manager.shutdown_all()


def test_terminals_help_prints_usage(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner())
    try:
        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            "/terminals help",
        )

        assert "Usage: /terminals" in output
        assert "/terminals show <process_id>" in output
        assert "/terminals kill <process_id>" in output
    finally:
        manager.shutdown_all()


def test_terminals_default_subcommand_is_list(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner())
    try:
        process_id = _start_process(manager, tmp_path, "sleep 60")

        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            "/terminals",
        )

        assert process_id in output
    finally:
        manager.shutdown_all()


def test_terminals_unknown_subcommand_prints_usage(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner())
    try:
        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            "/terminals nope",
        )

        assert "Usage: /terminals" in output
        assert "/terminals list" in output
    finally:
        manager.shutdown_all()


def test_terminals_when_session_has_no_terminal_manager_prints_unavailable(
    tmp_path: Path,
) -> None:
    result, output = _run_chat_command(
        _session(tmp_path=tmp_path, manager=None),
        "/terminals",
    )

    assert result == "handled"
    assert "Background terminals are unavailable in this session." in output


def test_terminals_kill_extra_args_rejected_with_usage(tmp_path: Path) -> None:
    manager = _manager(FakeBackgroundRunner())
    try:
        _result, output = _run_chat_command(
            _session(tmp_path=tmp_path, manager=manager),
            "/terminals kill abc def",
        )

        assert "Usage: /terminals kill <process_id>" in output
    finally:
        manager.shutdown_all()
