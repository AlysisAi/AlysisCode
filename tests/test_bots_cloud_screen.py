import base64
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from alysis_code.bots import cloud_desktop_worker as worker


@pytest.fixture
def screen_root(monkeypatch):
    # AF_UNIX paths have a small OS limit, including on the macOS development host.
    with TemporaryDirectory(prefix="alysis-screen-", dir="/tmp") as root:
        monkeypatch.setattr(worker, "root_for", lambda session: Path(root))
        yield


def test_screen_channel_is_read_only_owned_and_revokes_inflight_capture(screen_root):
    session = uuid4().hex
    server = worker.ScreenServer(session)
    request = {"id": uuid4().hex, "session": session}
    entered, release = threading.Event(), threading.Event()
    try:
        server.bind(lambda: b"actual capture")
        result = worker.exchange(session, request, screen=True)
        assert base64.b64decode(result["result"]["png"]) == b"actual capture"
        assert "error" in worker.exchange(session, {**request, "session": uuid4().hex}, screen=True)
        assert "error" in worker.exchange(session, {**request, "operation": "run"}, screen=True)

        def capture():
            entered.set()
            assert release.wait(2)
            return b"old screen"

        server.bind(capture)
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(worker.exchange, session, request, screen=True)
            assert entered.wait(1)
            server.bind(None)
            release.set()
            assert "error" in pending.result(2)
        assert "error" in worker.exchange(session, request, screen=True)
    finally:
        release.set()
        server.close()
    assert not server.thread.is_alive()


@pytest.mark.parametrize("same_receiver", [True, False])
def test_rebinding_the_same_bound_capture_preserves_only_that_screen(screen_root, same_receiver):
    entered, release = threading.Event(), threading.Event()

    class Display:
        def __eq__(self, other):
            return True  # Equal settings do not establish display ownership.

        def capture(self):
            entered.set()
            assert release.wait(2)
            return b"current display pixels"

    first, other = Display(), Display()
    session = uuid4().hex
    server = worker.ScreenServer(session)
    server.bind(first.capture)
    try:
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(
                worker.exchange, session, {"id": uuid4().hex, "session": session}, screen=True
            )
            assert entered.wait(1)
            server.bind((first if same_receiver else other).capture)
            release.set()
            result = pending.result(2)
            if same_receiver:
                assert base64.b64decode(result["result"]["png"]) == b"current display pixels"
            else:
                assert "error" in result
    finally:
        release.set()
        server.close()
