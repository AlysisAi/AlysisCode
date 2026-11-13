import base64
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
from test_bots_cloud_desktop import Provider

from alysis_code.bots.cloud_browser import RemoteBrowser
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import CommandReceipt, ComputerSpec
from alysis_code.ide.managed_browser import BrowserError, BrowserStaleFrameError


class BrowserProvider(Provider):
    def run(self, ref, argv, *, timeout):
        if argv[-2] == "start":
            return super().run(ref, argv, timeout=timeout)
        request = json.loads(base64.b64decode(argv[-1]))
        self.actions.append(request)
        if argv[-3] == "screen":
            reply = {key: request[key] for key in ("id", "session")}
            reply["result"] = {"png": base64.b64encode(b"frame bytes").decode()}
            if self.mode == "wrong-screen":
                reply["session"] = "another-session"
            return CommandReceipt(0, json.dumps(reply), "")
        result = {"ready": True}
        reply = {key: request[key] for key in ("id", "session")}
        if request["operation"] == "browser_call":
            method = request["arguments"]["method"]
            result = {"state": "running", "command_generation": 8} if method == "status" else {}
        if self.mode == "stale":
            reply.update(error="untrusted details excluded", error_type="BrowserStaleFrameError")
        elif self.mode == "download":
            reply.update(
                error="untrusted details excluded", error_type="BrowserDownloadRequiredError"
            )
        else:
            reply["result"] = result
        return CommandReceipt(0, json.dumps(reply), "")


def test_both_surfaces_share_one_vm_and_last_release_removes_it(tmp_path):
    provider = BrowserProvider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    browser = factory.browser("bot", lambda: False)
    desktop = factory("bot", tmp_path, cancel=lambda: False)
    assert len(provider.allocations) == 1
    assert browser._surface.ref == desktop.ref
    assert browser.browser.status("bot", browser.session_id).command_generation == 8
    browser.finish()
    assert desktop.alive()
    assert not browser.alive()
    desktop.finish()
    assert provider.status(desktop.ref).state == "missing"
    assert not factory._instances
    browser.close()
    desktop.close()


@pytest.mark.parametrize("in_flight", [False, True])
def test_released_desktop_cannot_view_a_surviving_sibling(tmp_path, in_flight):
    from concurrent.futures import ThreadPoolExecutor

    from alysis_code.bots.desktop_linux import DesktopError

    provider = BrowserProvider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    browser = factory.browser("bot", lambda: False)
    desktop = factory("bot", tmp_path, cancel=lambda: False)
    entered, release = threading.Event(), threading.Event()

    def capture():
        entered.set()
        assert release.wait(2)
        return b"previously captured pixels"

    try:
        if in_flight:
            desktop._computer.capture = capture
            with ThreadPoolExecutor() as pool:
                pending = pool.submit(desktop.capture)
                assert entered.wait(1)
                desktop.finish()
                release.set()
                with pytest.raises(DesktopError, match="revoked"):
                    pending.result(2)
        else:
            desktop.finish()
            before = len(provider.actions)
            with pytest.raises(DesktopError, match="released"):
                desktop.capture()
            assert len(provider.actions) == before
        assert browser.alive() and not desktop.alive()
    finally:
        release.set()
        browser.close()
        desktop.close()


def test_remote_stale_frame_remains_typed_and_never_replays(tmp_path):
    provider = BrowserProvider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    browser = factory.browser("bot", lambda: False)
    provider.mode = "stale"
    before = len(provider.actions)
    with pytest.raises(BrowserStaleFrameError):
        browser.browser.click_visual_frame(
            "bot", browser.session_id, SimpleNamespace(frame_id="old"), 3, 4
        )
    assert len(provider.actions) == before + 1
    assert browser.alive()
    with pytest.raises(BrowserError, match="ownership"):
        browser.browser.status("other", browser.session_id)
    browser.close()


def test_cloud_browser_refuses_unlisted_rpc_methods(tmp_path):
    with pytest.raises(AttributeError):
        _ = RemoteBrowser(None).execute_arbitrary_code


def test_remote_attachment_receipt_keeps_computer_and_does_not_retry(tmp_path):
    from alysis_code.ide.managed_browser import BrowserDownloadRequiredError

    provider = BrowserProvider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    browser = factory.browser("bot", lambda: False)
    provider.mode = "download"
    before = len(provider.actions)
    try:
        with pytest.raises(BrowserDownloadRequiredError):
            browser.browser.navigate("bot", browser.session_id, "https://example.org/file")
        assert len(provider.actions) == before + 1 and browser.alive()
    finally:
        browser.close()


def test_close_page_rpc_preserves_owner_and_observed_generation(tmp_path):
    provider = BrowserProvider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    computer = factory.browser("bot", lambda: False)
    try:
        before = len(provider.actions)
        with pytest.raises(BrowserError, match="ownership"):
            computer.browser.close_page("other", computer.session_id, "page", expected_generation=8)
        assert len(provider.actions) == before
        computer.browser.close_page("bot", computer.session_id, "page", expected_generation=8)
        assert provider.actions[-1]["arguments"] == {
            "method": "close_page",
            "args": ["page"],
            "kwargs": {"expected_generation": 8},
        }
        assert computer.alive()
    finally:
        computer.close()


def test_corrupt_browser_download_stream_does_not_publish_and_closes():
    from uuid import uuid4

    session = uuid4().hex
    closed = []
    payload = b"actual bytes"

    def chunks(*args, **kwargs):
        try:
            yield payload
        finally:
            closed.append(True)

    surface = SimpleNamespace(
        identity=SimpleNamespace(allocation_id=session),
        ref=None,
        provider=SimpleNamespace(download=chunks),
    )
    executor = SimpleNamespace(owner="bot", session_id=session, _surface=surface)
    browser = RemoteBrowser(executor)
    browser._rpc = lambda *args: {
        "path": f"/tmp/alysis-cloud-{session}/export-{uuid4().hex}",
        "bytes": len(payload),
        "sha256": hashlib.sha256(b"wrong").hexdigest(),
    }
    published = []

    def collect(data, size):
        value = b"".join(data)
        published.append(value)

    with pytest.raises(BrowserError, match="integrity"):
        browser.download("bot", session, "#link", max_bytes=100, collect=collect)
    assert not published
    assert closed == [True]


def test_failed_surface_stops_shared_vm_and_rejects_sibling_work(tmp_path):
    from alysis_code.bots.desktop_linux import DesktopError

    provider = BrowserProvider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    browser = factory.browser("bot", lambda: False)
    desktop = factory("bot", tmp_path, cancel=lambda: False)
    browser.close()
    assert provider.status(desktop.ref).state == "missing"
    assert not desktop.alive()
    with pytest.raises(DesktopError, match="closed"):
        desktop.call("bot", "run", {"command": "never dispatch"})
    desktop.close()
    assert not factory._instances


@pytest.mark.parametrize("surface", ["browser", "desktop"])
def test_capture_does_not_wait_for_action_lock_and_bad_receipt_never_closes_work(tmp_path, surface):
    from alysis_code.bots.desktop_linux import DesktopError

    provider = BrowserProvider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    browser = (
        factory.browser("bot", lambda: False)
        if surface == "browser"
        else factory("bot", tmp_path, cancel=lambda: False)
    )
    computer = browser._surface._computer if surface == "browser" else browser._computer
    entered, release = threading.Event(), threading.Event()

    def action():
        with computer._lock:
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=action)
    worker.start()
    try:
        assert entered.wait(1)
        assert browser.capture() == b"frame bytes"
        provider.mode = "wrong-screen"
        with pytest.raises(BrowserError if surface == "browser" else DesktopError, match="receipt"):
            browser.capture()
        assert browser.alive() and not provider.stopped.is_set()
    finally:
        release.set()
        worker.join(2)
        browser.close()


def test_screen_receipts_and_local_revocation_do_not_probe_management_status(tmp_path, monkeypatch):
    from alysis_code.bots.desktop_linux import DesktopError

    provider = BrowserProvider()
    factory = CloudDesktopFactory("owner", provider, ComputerSpec("image"), tmp_path)
    surface = factory("bot", tmp_path, cancel=lambda: False)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(provider, "status", lambda _: pytest.fail("Unexpected management probe"))
            assert surface.alive()
            assert surface.capture() == b"frame bytes"
            provider.mode = "wrong-screen"
            with pytest.raises(DesktopError, match="receipt"):
                surface.capture()
        surface._computer._stop.set()
        assert not surface.alive()
        with pytest.raises(DesktopError, match="closed"):
            surface.capture()
    finally:
        surface.close()


def test_browser_uses_provider_native_screen_stream_without_a_legacy_ingress_hub(tmp_path):
    class Stream:
        closed = False

        def request(self, message, *, timeout):
            assert message["operation"] == "screen" and message["arguments"] == {}
            return {
                "id": message["id"],
                "session": message["session"],
                "result": {
                    "mime": "image/jpeg",
                    "image": base64.b64encode(b"native screen pixels").decode(),
                },
            }

        def close(self):
            self.closed = True

    provider = BrowserProvider()
    provider.open_stream = lambda *args, **kwargs: pytest.fail("Stream is already connected")
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    browser = factory.browser("bot", lambda: False)
    computer = browser._surface._computer
    stream = Stream()
    try:
        assert computer._streams is None and computer._native_stream
        computer._stream, computer._stream_attempted = stream, True
        calls = len(provider.actions)
        assert browser.capture() == b"native screen pixels"
        assert len(provider.actions) == calls
    finally:
        browser.close()
    assert stream.closed
