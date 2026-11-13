import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from alysis_code.bots.computer_stream import ComputerStreams, StreamUnavailable, validate_url


def connect(client, hub, link, token=None):
    return client.websocket_connect(
        "/internal/computer-stream/" + link.identity,
        headers={"Authorization": "Bearer " + (token or link.token)},
    )


def message(link, operation="screen"):
    return {"id": uuid4().hex, "session": link.session, "operation": operation, "arguments": {}}


def reply(request, **result):
    return {"id": request["id"], "session": request["session"], "result": result}


@pytest.fixture
def channel():
    hub = ComputerStreams("wss://controller.example/internal/computer-stream")
    app = FastAPI()
    app.mount("/internal/computer-stream", hub)
    owned = threading.Event()
    owned.set()
    link = hub.create(uuid4().hex, owned.is_set)
    with TestClient(app) as client:
        yield client, hub, link, owned
    hub.close()


@pytest.mark.parametrize(
    "url",
    [
        "ws://controller.example/internal/computer-stream",
        "wss://key@controller.example/internal/computer-stream",
        "wss://controller.example/another-route",
        "wss://controller.example/internal/computer-stream?credential=secret",
    ],
)
def test_only_the_explicit_verified_controller_endpoint_is_accepted(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_capability_is_single_use_and_does_not_grant_another_allocation(channel):
    client, hub, link, _ = channel
    token = link.token
    with pytest.raises(WebSocketDisconnect):
        with connect(client, hub, link, "wrong-capability"):
            pass
    other = hub.create(uuid4().hex, lambda: True)
    with pytest.raises(WebSocketDisconnect):
        with connect(client, hub, other, token):
            pass
    with connect(client, hub, link, token):
        link.wait(1)
        with pytest.raises(WebSocketDisconnect):
            with connect(client, hub, link, token):
                pass
    assert link.closed and not link.token
    with pytest.raises(WebSocketDisconnect):
        with connect(client, hub, link, token):
            pass


def test_independent_screen_and_input_receipts_do_not_block_each_other(channel):
    client, hub, link, _ = channel
    with connect(client, hub, link) as ws, ThreadPoolExecutor() as pool:
        link.wait(1)
        screen, event = message(link), message(link, "human_input")
        frame = pool.submit(link.request, screen, timeout=2)
        gesture = pool.submit(link.request, event, timeout=2)
        actual = [ws.receive_json(), ws.receive_json()]
        assert {m["id"] for m in actual} == {screen["id"], event["id"]}
        ws.send_json(reply(event, applied=True))
        assert gesture.result(1)["result"] == {"applied": True}
        assert not frame.done()
        ws.send_json(reply(screen, png=base64.b64encode(b"captured pixels").decode()))
        assert base64.b64decode(frame.result(1)["result"]["png"]) == b"captured pixels"


def test_disconnect_fails_an_admitted_input_without_replaying_it(channel):
    client, hub, link, _ = channel
    with ThreadPoolExecutor() as pool:
        with connect(client, hub, link) as ws:
            link.wait(1)
            event = message(link, "human_input")
            pending = pool.submit(link.request, event, timeout=2)
            assert ws.receive_json() == event
        with pytest.raises(StreamUnavailable):
            pending.result(1)
        with pytest.raises(StreamUnavailable):
            link.request(event, timeout=1)
    assert link.identity not in hub.links


def test_worker_epoch_loss_rejects_dispatch_and_late_receipts(channel):
    client, hub, link, owned = channel
    with connect(client, hub, link) as ws, ThreadPoolExecutor() as pool:
        link.wait(1)
        event = message(link, "human_input")
        pending = pool.submit(link.request, event, timeout=2)
        assert ws.receive_json() == event
        owned.clear()
        ws.send_json(reply(event, applied=True))
        with pytest.raises(StreamUnavailable):
            pending.result(1)
        with pytest.raises(StreamUnavailable):
            link.request(message(link), timeout=1)
    assert link.closed


def test_expired_worker_cannot_complete_a_guest_handshake(channel):
    client, hub, link, owned = channel
    owned.clear()
    with pytest.raises(WebSocketDisconnect):
        with connect(client, hub, link):
            pass
    assert link.closed and link.identity not in hub.links


def test_canary_is_scoped_before_bootstrapping_any_unrelated_computer(tmp_path):
    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import ComputerSpec

    selected, other = uuid4().hex, uuid4().hex
    streams = ComputerStreams("wss://controller.example/internal/computer-stream", bots=[selected])
    factory = CloudDesktopFactory(
        "owner", Provider(), ComputerSpec("image"), tmp_path, computer_streams=streams
    )
    first = factory(selected, tmp_path, cancel=lambda: False)
    second = factory(other, tmp_path, cancel=lambda: False)
    try:
        assert first._streams is streams
        assert second._streams is None
        assert not streams.links  # No unapproved helper/connection started.
    finally:
        first.close()
        second.close()


def test_cross_session_reply_closes_channel_and_never_becomes_success(channel):
    client, hub, link, _ = channel
    with connect(client, hub, link) as ws, ThreadPoolExecutor() as pool:
        link.wait(1)
        pending = pool.submit(link.request, message(link), timeout=2)
        request = ws.receive_json()
        ws.send_json({**reply(request, png="AA=="), "session": uuid4().hex})
        with pytest.raises(StreamUnavailable):
            pending.result(1)


def test_timeout_consumes_link_and_bounds_unacknowledged_work(channel):
    client, hub, link, _ = channel
    with connect(client, hub, link) as ws, ThreadPoolExecutor() as pool:
        link.wait(1)
        first = pool.submit(link.request, message(link), timeout=0.2)
        second = pool.submit(link.request, message(link, "human_input"), timeout=0.2)
        ws.receive_json()
        ws.receive_json()
        with pytest.raises(StreamUnavailable, match="busy"):
            link.request(message(link), timeout=1)
        for future in (first, second):
            with pytest.raises(StreamUnavailable):
                future.result(1)
        assert link.closed


def test_cloud_stream_input_keeps_existing_encryption_and_never_replays_on_loss(tmp_path):
    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import ComputerSpec
    from alysis_code.bots.desktop_linux import ComputerActionUncertain, DesktopError
    from alysis_code.bots.private_input import PrivateInput

    provider = Provider()
    factory = CloudDesktopFactory("owner", provider, ComputerSpec("image"), tmp_path)
    surface = factory("bot", tmp_path, cancel=lambda: False)
    computer = surface._computer
    private = PrivateInput(computer.identity.allocation_id)
    computer._input_key = private.public_key
    received = []

    class Link:
        closed = False

        def request(self, request, *, timeout):
            received.append(private.open(request["id"], request["arguments"]))
            assert "synthetic typed text" not in json.dumps(request)
            raise StreamUnavailable("Lost after applying input")

        def close(self):
            self.closed = True

    computer._stream = Link()
    baseline_commands = len(provider.actions)
    try:
        with pytest.raises(ComputerActionUncertain):
            surface.human_input({"kind": "text", "text": "synthetic typed text"})
        with pytest.raises(DesktopError, match="disconnected"):
            surface.human_input({"kind": "text", "text": "synthetic typed text"})
        assert len(received) == 1
        assert surface.alive() and provider.status(surface.ref).state == "running"
        assert not surface.input_ready()
        assert len(provider.actions) == baseline_commands
    finally:
        surface.close()


def test_an_uncertain_input_reset_preserves_the_owned_computer(tmp_path, monkeypatch):
    from dataclasses import replace

    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import ComputerSpec, ComputeUncertain
    from alysis_code.bots.desktop_linux import DesktopError

    provider = Provider()
    factory = CloudDesktopFactory("owner", provider, ComputerSpec("image"), tmp_path)
    surface = factory("bot", tmp_path, cancel=lambda: False)
    computer = surface._computer
    computer.spec = replace(computer.spec, suspend_mode="Memory")

    class ClosedLink:
        closed = True

        def close(self):
            pass

    computer._stream = ClosedLink()
    run = provider.run

    def lost_reset(*args, **kwargs):
        raise ComputeUncertain("Receipt lost after key reset")

    monkeypatch.setattr(provider, "run", lost_reset)
    try:
        with pytest.raises(DesktopError, match="reset was not confirmed"):
            surface.prepare_input()
        assert surface.alive() and provider.status(surface.ref).state == "running"
        assert not surface.input_ready()
    finally:
        monkeypatch.setattr(provider, "run", run)
        surface.close()


def test_guest_bootstrap_decrypts_only_in_memory_and_removes_its_private_socket(
    tmp_path, monkeypatch
):
    from alysis_code.bots import computer_stream_worker as worker
    from alysis_code.bots.private_input import seal_input

    # Short macOS AF_UNIX path; no real credential or private key is written.
    channel_id, session = uuid4().hex, uuid4().hex
    path = "/tmp/alysis-test-" + channel_id[:12] + ".sock"
    monkeypatch.setattr(worker, "address", lambda _: path)
    observed = []

    async def connected(actual_session, config):
        observed.append((actual_session, config.copy()))

    monkeypatch.setattr(worker, "relay", connected)
    with ThreadPoolExecutor() as pool:
        serving = pool.submit(worker.serve, session, channel_id)
        deadline = time.monotonic() + 2
        while True:
            try:
                key = worker.local(channel_id, {"action": "key"})
                break
            except (OSError, TimeoutError):
                assert time.monotonic() < deadline
                time.sleep(0.01)
        config = {
            "url": "wss://controller.example/internal/computer-stream/" + channel_id,
            "token": "synthetic-ephemeral-capability",
            "certificate": None,
        }
        identity = uuid4().hex
        envelope = seal_input(
            key["public_key"], session, identity, 1, {"surface": "desktop", "event": config}
        )
        request = {"id": identity, "envelope": envelope}
        assert config["token"] not in json.dumps(request)
        assert worker.local(channel_id, request) == {"configured": True}
        serving.result(2)
        assert observed == [(session, config)]
        from pathlib import Path

        assert not Path(path).exists()


def test_guest_relay_keeps_screen_independent_and_forwards_only_encrypted_input(monkeypatch):
    import asyncio
    import ssl
    from contextlib import asynccontextmanager

    from alysis_code.bots import computer_stream_worker as worker
    from alysis_code.bots.private_input import PrivateInput, seal_input

    session, identity = uuid4().hex, uuid4().hex
    private = PrivateInput(session)
    event = {"kind": "text", "text": "synthetic-private-Ελληνικά"}
    gesture = {
        "id": identity,
        "session": session,
        "operation": "human_input",
        "arguments": seal_input(
            private.public_key, session, identity, 1, {"surface": "browser", "event": event}
        ),
    }
    screen = {"id": uuid4().hex, "session": session, "operation": "screen", "arguments": {}}
    input_arrived, screen_entered = threading.Event(), threading.Event()
    observed, sent, handshakes = [], [], []

    class Socket:
        async def __aiter__(self):
            yield json.dumps(screen)
            assert await asyncio.to_thread(screen_entered.wait, 1)
            yield json.dumps(gesture)

        async def send(self, wire):
            sent.append(json.loads(wire))

        async def close(self, **kwargs):
            pass

    @asynccontextmanager
    async def connect(url, **options):
        assert options["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert options["ssl"].check_hostname is True
        assert options["proxy"] is None and options["max_queue"] == 2
        assert options["additional_headers"] == {"Authorization": "Bearer synthetic-capability"}
        handshakes.append(url)
        yield Socket()

    def exchange(actual_session, request, *, screen=False, timeout):
        assert actual_session == session
        if screen:
            assert set(request) == {"id", "session"}
            screen_entered.set()
            assert input_arrived.wait(1), "Input must not wait behind a slow frame"
            return reply(request, png="cGl4ZWxz")
        assert "synthetic-private" not in json.dumps(request)
        observed.append(private.open(request["id"], request["arguments"]))
        input_arrived.set()
        return reply(request, applied=True)

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    monkeypatch.setattr(worker, "exchange", exchange)
    config = {
        "url": "wss://controller.example/internal/computer-stream/" + uuid4().hex,
        "token": "synthetic-capability",
        "certificate": None,
    }
    asyncio.run(worker.relay(session, config))
    assert config == {} and len(handshakes) == 1
    assert observed == [{"surface": "browser", "event": event}]
    assert {r["id"] for r in sent} == {screen["id"], gesture["id"]}


def test_slow_stream_setup_never_blocks_the_first_screen_or_publishes_an_unready_link(tmp_path):
    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import CommandReceipt, ComputeError, ComputerSpec

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    provider = Provider()
    hub = ComputerStreams("wss://controller.example/internal/computer-stream")
    run = provider.run

    def upload(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(3)
            raise ComputeError("Setup transport failed")
        finally:
            finished.set()

    def screen(ref, argv, *, timeout):
        if argv[-3] == "screen":
            request = json.loads(base64.b64decode(argv[-1]))
            return CommandReceipt(
                0, json.dumps(reply(request, png=base64.b64encode(b"pixels").decode())), ""
            )
        return run(ref, argv, timeout=timeout)

    provider.upload, provider.run = upload, screen
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, computer_streams=hub
    )
    surface = factory("bot", tmp_path, cancel=lambda: False)
    computer = surface._computer
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(surface.capture)
            assert entered.wait(1)
            assert first.result(1) == b"pixels"
            assert computer._stream is None and computer._stream_connecting
            assert surface.input_ready()
            # Taking control may use the existing authenticated RPC while the
            # optional link is still being installed; it does not wait for it.
            pool.submit(surface.prepare_input).result(1)
        pending = computer._pending_stream
        assert pending and not pending.closed
        surface.close()
        assert pending.closed and not hub.links
    finally:
        release.set()
        assert finished.wait(1)
        surface.close()
        hub.close()
