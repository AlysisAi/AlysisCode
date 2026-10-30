"""Noninteractive host probes whose Windows output cannot outlive the timeout."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence

from .command_text import command_text, decode_command_output


def _stop_owned_tree(process: subprocess.Popen[bytes]) -> None:
    import psutil

    # Inventory before killing the leader: Windows can otherwise orphan a Git
    # helper that still owns inherited output handles. psutil pins creation time
    # when checking process identity before kill; never kill by executable name.
    descendants: list[psutil.Process] = []
    try:
        descendants = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        pass
    for child in reversed(descendants):
        try:
            child.kill()
        except psutil.Error:
            pass
    try:
        process.kill()
    except OSError:
        pass
    # All waits are bounded, including cleanup. Regular output files need no
    # EOF from descendants we could not inspect or terminate.
    if descendants:
        psutil.wait_procs(descendants, timeout=0.5)
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def run_capture(
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    text: bool = False,
    timeout: float,
) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
    """Capture a finite, noninteractive probe; raise TimeoutExpired on timeout.

    Python's Windows PIPE timeout path kills the leader and then drains output
    with no timeout. An inherited pipe in a surviving descendant can block that
    drain forever. Regular temporary files let us read a snapshot without
    waiting for EOF. DEVNULL also keeps host probes off the IDE protocol stdin.
    """
    if os.name != "nt":
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            **command_text(text),
            env=env,
            timeout=timeout,
        )
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=env,
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            process.wait(timeout=timeout)
        except BaseException:
            _stop_owned_tree(process)
            raise
        # Read only the size observed when the leader exited. A surviving helper
        # cannot keep this read running by appending more output concurrently.
        out_size = os.fstat(stdout.fileno()).st_size
        err_size = os.fstat(stderr.fileno()).st_size
        stdout.seek(0)
        stderr.seek(0)
        out = stdout.read(out_size)
        err = stderr.read(err_size)
    if text:
        return subprocess.CompletedProcess(
            args,
            process.returncode,
            decode_command_output(out).replace("\r\n", "\n").replace("\r", "\n"),
            decode_command_output(err).replace("\r\n", "\n").replace("\r", "\n"),
        )
    return subprocess.CompletedProcess(args, process.returncode, out, err)
