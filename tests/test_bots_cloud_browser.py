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
from alysis_code.ide.managed_browser import (
    BrowserError,
    BrowserLimitError,
    BrowserStaleFrameError,
)


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
        if request["operation"] == "browser_start" and getattr(self, "features", None):
            result["features"] = self.features
        reply = {key: request[key] for key in ("id", "session")}
        if request["operation"] == "browser_call":
            method = request["arguments"]["method"]
            result = {"state": "running", "command_generation": 8} if method == "status" else {}
        if getattr(self, "error_reply", None) and request["operation"] == "browser_call":
            reply.update(self.error_reply)
        elif self.mode == "stale":
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


def test_the_browser_declares_its_operations_and_the_host_reaches_each_of_them(tmp_path):
    from alysis_code.bots.cloud_browser import METHODS, OPERATIONS
    from alysis_code.bots.cloud_browser_worker import FEATURES

    # This version's browser declares every operation beyond the first images'.
    assert set(OPERATIONS) <= set(FEATURES) and "site_data" in FEATURES
    provider = BrowserProvider()
    provider.features = list(FEATURES)
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    computer = factory.browser("bot", lambda: False)
    try:
        assert computer.browser_features == frozenset(FEATURES)
        for method in ("fill", "hover", "set_checked", "drag", "wait_for", "read_value"):
            assert method in METHODS
        computer.browser.fill(
            "bot", computer.session_id, None, "Αθηνά 雅典", expected_backend_node_id=7,
            frame_id="PAY-FRAME", expected_generation=8,
        )  # fmt: skip
        assert provider.actions[-1]["arguments"] == {
            "method": "fill",
            "args": [None, "Αθηνά 雅典"],
            "kwargs": {
                "expected_backend_node_id": 7,
                "frame_id": "PAY-FRAME",
                "expected_generation": 8,
            },
        }
        computer.browser.handle_dialog("bot", computer.session_id, accept=False)
        assert provider.actions[-1]["arguments"]["method"] == "handle_dialog"
    finally:
        computer.close()
    # An older image declares none of them.
    provider = BrowserProvider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path / "older", browser_enabled=True
    )
    older = factory.browser("bot", lambda: False)
    try:
        assert older.browser_features == frozenset()
    finally:
        older.close()


def test_a_page_dialog_crosses_the_bridge_with_its_text_and_never_its_traceback(tmp_path):
    from alysis_code.bots.cloud_desktop_worker import browser_error_reply
    from alysis_code.ide.managed_browser import BrowserDialogOpenError, BrowserTargetHiddenError

    # The computer's side: the dialog's type and text travel apart from the support details.
    reply = browser_error_reply(
        BrowserDialogOpenError("confirm", "Να φύγετε από τη σελίδα; 离开此页？"), fatal=False
    )
    assert reply["error_type"] == "BrowserDialogOpenError" and reply["fatal"] is False
    assert reply["dialog"] == {"type": "confirm", "message": "Να φύγετε από τη σελίδα; 离开此页？"}
    assert "error_detail" not in reply
    hidden = browser_error_reply(BrowserTargetHiddenError("hidden"), fatal=False)
    assert hidden["error_type"] == "BrowserTargetHiddenError" and "dialog" not in hidden
    provider = BrowserProvider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    computer = factory.browser("bot", lambda: False)
    try:
        provider.error_reply = {**reply, "error": "untrusted details excluded"}
        with pytest.raises(BrowserDialogOpenError) as shown:
            computer.browser.snapshot("bot", computer.session_id, kind="dom")
        assert shown.value.dialog_type == "confirm"
        assert shown.value.dialog_message == "Να φύγετε από τη σελίδα; 离开此页？"
        provider.error_reply = {**hidden, "error": "untrusted details excluded"}
        with pytest.raises(BrowserTargetHiddenError):
            computer.browser.hover("bot", computer.session_id, None, expected_backend_node_id=3)
        assert computer.alive()
    finally:
        computer.close()


def test_a_dialogs_frame_crosses_the_bridge_and_newer_operations_are_declared(tmp_path):
    from alysis_code.bots.cloud_browser_worker import FEATURES
    from alysis_code.bots.cloud_desktop_worker import browser_error_reply
    from alysis_code.ide.managed_browser import BrowserDialogOpenError

    # The computer's side: the frame that shows the dialog travels with its type and text.
    reply = browser_error_reply(
        BrowserDialogOpenError(
            "confirm", "Πληρωμή; 支付？", "https://pay.example.net/checkout", "PAY-FRAME"
        ),
        fatal=False,
    )
    assert reply["dialog"] == {
        "type": "confirm",
        "message": "Πληρωμή; 支付？",
        "url": "https://pay.example.net/checkout",
        "frame_id": "PAY-FRAME",
    }
    provider = BrowserProvider()
    provider.features = list(FEATURES)
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    computer = factory.browser("bot", lambda: False)
    try:
        for feature in ("frame_snapshots", "step_dialog", "dialog_frames"):
            assert feature in computer.browser_features
        provider.error_reply = {**reply, "error": "untrusted details excluded"}
        with pytest.raises(BrowserDialogOpenError) as shown:
            computer.browser.snapshot("bot", computer.session_id, kind="dom", frames=True)
        assert shown.value.dialog_url == "https://pay.example.net/checkout"
        assert shown.value.dialog_frame_id == "PAY-FRAME"
        assert provider.actions[-1]["arguments"]["kwargs"] == {"kind": "dom", "frames": True}
        # A frame id that is not one is dropped, never trusted as one.
        provider.error_reply = {
            **reply,
            "dialog": {**reply["dialog"], "frame_id": "../../not a frame"},
        }
        with pytest.raises(BrowserDialogOpenError) as shown:
            computer.browser.snapshot("bot", computer.session_id, kind="dom")
        assert shown.value.dialog_frame_id is None
    finally:
        computer.close()


def test_a_newer_browser_declares_its_page_report_and_the_host_reads_it_over_the_bridge(tmp_path):
    from alysis_code.bots.cloud_browser_worker import FEATURES

    assert "page_reported" in FEATURES
    provider = BrowserProvider()
    provider.features = list(FEATURES)
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    computer = factory.browser("bot", lambda: False)
    try:
        assert "page_reported" in computer.browser_features
        computer.browser.page_report("bot", computer.session_id)
        assert provider.actions[-1]["arguments"] == {
            "method": "page_report",
            "args": [],
            "kwargs": {},
        }
    finally:
        computer.close()


@pytest.mark.parametrize("kind", ["dom", "accessibility", "text"])
def test_a_page_read_too_large_to_receive_reaches_the_reader_as_a_page_limit(kind):
    from alysis_code.bots.desktop_linux import ComputerReplyTooLarge

    def refuse(*args, **kwargs):
        raise ComputerReplyTooLarge("too large to receive")

    executor = SimpleNamespace(
        owner="bot", session_id="session", _surface=SimpleNamespace(_request=refuse)
    )
    with pytest.raises(BrowserLimitError, match="browser_source"):
        RemoteBrowser(executor).snapshot("bot", "session", kind=kind)
