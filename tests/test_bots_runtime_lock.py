"""Portable ownership admission; Windows API fixtures are not Windows qualification."""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from alysis_code.bots import runtime as module
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store


def test_service_import_does_not_require_a_unix_lock_module():
    program = """import builtins
original=builtins.__import__
def importing(name,globals=None,locals=None,fromlist=(),level=0):
    if name=='fcntl' and globals and globals.get('__name__')=='alysis_code.bots.runtime':
        raise ModuleNotFoundError('Unix file locking is unavailable')
    return original(name,globals,locals,fromlist,level)
builtins.__import__=importing
import alysis_code.bots.app
"""
    result = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("refused", [False, True])
def test_windows_admission_is_nonblocking_and_handle_lives_until_workers_stop(
    tmp_path, monkeypatch, refused
):
    calls = []

    def locking(fd, mode, length):
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        calls.append((fd, mode, length))
        if refused:
            raise OSError("Fixture competing owner")

    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"), raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_NBLCK=2))
    runtime = BotRuntime(Store(tmp_path / "bots.sqlite3"), "owner", lambda _: None)
    try:
        if refused:
            with pytest.raises(RuntimeError, match="already owns"):
                runtime.start()
            assert runtime._lease is None and not runtime.is_running
        else:
            runtime.start()
            assert runtime.is_running
            assert not runtime._lease.closed
    finally:
        runtime.stop()
    assert len(calls) == 1 and calls[0][1:] == (2, 1)
    with pytest.raises(OSError):
        os.fstat(calls[0][0])
