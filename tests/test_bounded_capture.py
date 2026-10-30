from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from alysis_code.bounded_capture import run_capture
from alysis_code.command_text import decode_command_output


def test_capture_uses_engine_command_decoding_for_utf8_and_legacy_bytes() -> None:
    raw = "Δοκιμή\r\n".encode() + b"legacy:\x82\r\n"
    result = run_capture(
        [sys.executable, "-c", f"import os; os.write(1, {raw!r})"],
        env=dict(os.environ),
        timeout=5,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == decode_command_output(raw).replace("\r\n", "\n")


@pytest.mark.parametrize("text", [False, True])
def test_capture_is_noninteractive_and_preserves_exit_and_output(text: bool) -> None:
    result = run_capture(
        [
            sys.executable,
            "-c",
            "import os,sys; assert sys.stdin.read() == ''; "
            "os.write(1,b'first\\r\\nsecond\\rthird\\n'); "
            "os.write(2,b'error\\r\\n'); sys.exit(7)",
        ],
        env=dict(os.environ),
        timeout=5,
        text=text,
    )
    assert result.returncode == 7
    assert result.stdout == ("first\nsecond\nthird\n" if text else b"first\r\nsecond\rthird\n")
    assert result.stderr == ("error\n" if text else b"error\r\n")


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited-handle regression")
def test_timeout_returns_and_kills_descendant_holding_output(tmp_path: Path) -> None:
    import psutil

    pid_file = tmp_path / "owned-child.pid"
    command = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(8)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(8)"
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_capture(
                [sys.executable, "-c", command, str(pid_file)],
                env=dict(os.environ),
                timeout=1.5,
            )
        assert time.monotonic() - started < 5
        assert pid_file.exists(), "probe must reach the inherited-output child"
        child_pid = int(pid_file.read_text())
        assert not psutil.pid_exists(child_pid), "owned descendant survived timeout cleanup"
    finally:
        # A failing test must not leave its finite probe waiting in the background.
        if pid_file.exists():
            try:
                child = psutil.Process(int(pid_file.read_text()))
                if child.create_time() >= time.time() - (time.monotonic() - started) - 1:
                    child.kill()
                    child.wait(timeout=2)
            except psutil.NoSuchProcess:
                pass
