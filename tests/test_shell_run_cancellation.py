"""A Stop ends a ``shell_run`` command that is still running.

The turn hands its cancellation token to ``shell_run``, as it does to
``shell_wait``. The runner reads it while the command runs and, once it fires,
stops the command and what the command started, so the turn ends within the
1.5 s a desktop host gives a Stop to be confirmed, not when the command ends.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.tools_assembly import (
    _SHELL_CANCELLABLE_WAIT_TOOL_NAMES,
    _SHELL_CANCELLATION_TOKEN_ARG,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.process_reaping import (
    CommandCancelledError,
    ProcessGroupRegistry,
    run_in_tracked_process_group,
)
from alysis_code.sandbox_runner import HostShellRunner, LazyShellRunner
from alysis_code.tools.shell import CANCELLED_COMMAND_MESSAGE, ShellError, shell_run
from alysis_code.verify_gate import (
    CANCELLED_VERIFICATION_MESSAGE,
    VerifyError,
    run_task_verification,
)

# Without a stop these commands run this long.
_SLEEP_SECONDS = 30
# How long a stopped command may take to end, from the moment the check fires.
# Measured well under a second; generous for a loaded machine.
_STOPPED_WITHIN_SECONDS = 3.0


def _shell_join(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _python_cmd(code: str) -> str:
    return _shell_join([sys.executable, "-c", code])


# Prints a line, starts a child that sleeps, writes the child's pid to
# ``child.pid`` in its working folder, then sleeps itself.
_SLOW_WITH_CHILD = _python_cmd(
    "import pathlib, subprocess, sys, time; "
    "print('started', flush=True); "
    f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({_SLEEP_SECONDS})']); "
    "pathlib.Path('child.tmp').write_text(str(child.pid)); "
    "pathlib.Path('child.tmp').replace('child.pid'); "
    f"time.sleep({_SLEEP_SECONDS})"
)


class _CancelWhen:
    """A cancel check that fires once ``ready()`` is true, and notes when it fired."""

    def __init__(self, ready: Callable[[], bool]) -> None:
        self._ready = ready
        self.fired_at: float | None = None

    def __call__(self) -> bool:
        if self.fired_at is None and self._ready():
            self.fired_at = time.monotonic()
        return self.fired_at is not None


class _Token:
    """Stands in for the turn's cancellation token: only ``is_cancelled`` is read."""

    def __init__(self, check: _CancelWhen) -> None:
        self._check = check

    @property
    def is_cancelled(self) -> bool:
        return self._check()


def _child_started(folder: Path) -> Callable[[], bool]:
    return lambda: (folder / "child.pid").exists()


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return f'"{pid}"' in listed
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # A zombie has ended; it only waits for its parent to collect it.
        return "\nState:\tZ" not in Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return True


def _ends_soon(pid: int, *, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _stopped_in_time(check: _CancelWhen) -> float:
    assert check.fired_at is not None, "the cancel check never fired"
    took = time.monotonic() - check.fired_at
    assert took < _STOPPED_WITHIN_SECONDS, f"the command took {took:.2f}s to stop"
    return took


@pytest.mark.parametrize("tracked", [True, False], ids=["registry", "no-registry"])
def test_a_cancel_stops_the_command_and_what_it_started(tmp_path: Path, tracked: bool) -> None:
    check = _CancelWhen(_child_started(tmp_path))
    with pytest.raises(CommandCancelledError) as caught:
        run_in_tracked_process_group(
            _SLOW_WITH_CHILD,
            shell=True,
            cwd=str(tmp_path),
            timeout=60,
            registry=ProcessGroupRegistry() if tracked else None,
            origin="test",
            cancel_check=check,
        )
    _stopped_in_time(check)
    assert "started" in str(caught.value.stdout)
    assert _ends_soon(int((tmp_path / "child.pid").read_text()))


def test_a_cancel_check_that_never_fires_changes_nothing(tmp_path: Path) -> None:
    completed = run_in_tracked_process_group(
        _python_cmd("print('ok')"),
        shell=True,
        cwd=str(tmp_path),
        timeout=30,
        registry=None,
        origin="test",
        cancel_check=lambda: False,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"


def test_the_timeout_still_applies_with_a_cancel_check(tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_in_tracked_process_group(
            _python_cmd(f"import time; time.sleep({_SLEEP_SECONDS})"),
            shell=True,
            cwd=str(tmp_path),
            timeout=1,
            registry=None,
            origin="test",
            cancel_check=lambda: False,
        )
    assert caught.value.timeout == 1
    assert time.monotonic() - started < 1 + _STOPPED_WITHIN_SECONDS


def test_shell_run_reports_a_cancelled_command_with_what_it_printed(tmp_path: Path) -> None:
    check = _CancelWhen(_child_started(tmp_path))
    with pytest.raises(ShellError) as caught:
        shell_run(
            root=tmp_path,
            cmd=_SLOW_WITH_CHILD,
            timeout_s=60,
            runner=HostShellRunner(),
            cancel_check=check,
        )
    _stopped_in_time(check)
    assert str(caught.value) == CANCELLED_COMMAND_MESSAGE
    payload = caught.value.result_payload
    assert payload is not None
    assert payload["cancelled"] is True
    assert payload["error"] == CANCELLED_COMMAND_MESSAGE
    assert payload["cmd"] == _SLOW_WITH_CHILD
    assert "started" in payload["stdout"]
    assert _ends_soon(int((tmp_path / "child.pid").read_text()))


class _RunnerWithoutCancelCheck:
    """A runner written before ``cancel_check``."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, *, root: Path, cwd: Path, cmd: str, timeout_s: int) -> Any:
        self.commands.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="old\n", stderr="")


def test_shell_run_keeps_working_with_a_runner_that_takes_no_cancel_check(tmp_path: Path) -> None:
    runner = _RunnerWithoutCancelCheck()
    result = shell_run(root=tmp_path, cmd="echo old", runner=runner, cancel_check=lambda: False)
    assert result["exit_code"] == 0
    assert result["stdout"] == "old\n"
    assert runner.commands == ["echo old"]


def test_the_lazy_runner_passes_the_cancel_check_on(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class _Runner:
        def run(
            self,
            *,
            root: Path,
            cwd: Path,
            cmd: str,
            timeout_s: int,
            cancel_check: Callable[[], bool] | None = None,
        ) -> Any:
            seen["cancel_check"] = cancel_check
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    def check() -> bool:
        return False

    LazyShellRunner(_Runner).run(
        root=tmp_path, cwd=tmp_path, cmd="x", timeout_s=5, cancel_check=check
    )
    assert seen["cancel_check"] is check


def test_verify_stops_a_running_check_and_reports_no_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    check = _CancelWhen(_child_started(tmp_path))
    with pytest.raises(VerifyError) as caught:
        run_task_verification(
            root=tmp_path,
            commands=[_SLOW_WITH_CHILD],
            artifact_path=tmp_path / "verify.txt",
            cfg=AppConfig(model=""),
            timeout_s=60,
            cancel_check=check,
        )
    _stopped_in_time(check)
    assert str(caught.value) == CANCELLED_VERIFICATION_MESSAGE
    assert _ends_soon(int((tmp_path / "child.pid").read_text()))


def test_verify_starts_no_further_check_after_a_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    commands = [
        _python_cmd("import pathlib; pathlib.Path('first-ran').write_text('x')"),
        _python_cmd("import pathlib; pathlib.Path('second-ran').write_text('x')"),
    ]
    with pytest.raises(VerifyError):
        run_task_verification(
            root=tmp_path,
            commands=commands,
            artifact_path=tmp_path / "verify.txt",
            cfg=AppConfig(model=""),
            timeout_s=60,
            cancel_check=lambda: (tmp_path / "first-ran").exists(),
        )
    assert (tmp_path / "first-ran").exists()
    assert not (tmp_path / "second-ran").exists()


def test_the_turn_hands_its_cancellation_to_shell_run(tmp_path: Path) -> None:
    # The turn gives the token to the tools in this set (``_run_tool_with_turn_cancellation``).
    assert {"shell_run", "verify_run"} <= _SHELL_CANCELLABLE_WAIT_TOOL_NAMES
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {"shell_sandbox": {"mode": "off"}}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        non_interactive=True,
    )
    try:
        check = _CancelWhen(_child_started(tmp_path))
        with pytest.raises(ShellError) as caught:
            session.tools["shell_run"].run(
                {"cmd": _SLOW_WITH_CHILD, _SHELL_CANCELLATION_TOKEN_ARG: _Token(check)}
            )
        _stopped_in_time(check)
        payload = caught.value.result_payload
        assert payload is not None and payload["cancelled"] is True
        assert _ends_soon(int((tmp_path / "child.pid").read_text()))
    finally:
        session.close()
