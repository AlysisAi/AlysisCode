from __future__ import annotations

import base64
import socket
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop_rfb import RfbSessions, validate


def packet(action="open", sequence=0, data=b"", client="a" * 32):
    return {
        "action": action,
        "sequence": sequence,
        "client_id": client,
        "data": base64.b64encode(data).decode(),
    }


@pytest.mark.parametrize(
    "change",
    [
        {"sequence": True},
        {"sequence": -1},
        {"client_id": "../display"},
        {"action": "input"},
        {"data": "%%%"},
        {"data": "A" * 43696},
        {"display": ":0"},
        {"data": "eA=="},
    ],
)
def test_stream_validation_is_bounded(change):
    with pytest.raises(ValueError):
        validate({**packet(), **change})


def test_private_stream_orders_bytes_and_never_opens_a_listener(monkeypatch):
    from alysis_code.bots import desktop_rfb

    actual_popen = subprocess.Popen

    def spawn(argv, **kwargs):
        assert argv[0] == "/usr/bin/x11vnc"
        assert all(
            flag in argv
            for flag in ("-inetd", "-noshm", "-viewonly", "-nosel", "-noclipboard", "-norc")
        )
        assert kwargs["stdin"] is kwargs["stdout"]
        assert kwargs["stdin"].family == socket.AF_UNIX
        # Real duplex pipe/process I/O; no X server is required for this transport test.
        import sys

        return actual_popen(
            [
                sys.executable,
                "-u",
                "-c",
                "import os; os.write(1,b'RFB 003.008\\n'); "
                "exec('while True:\\n data=os.read(0,32768)\\n if not data: break\\n os.write(1,data)')",
            ],
            **kwargs,
        )

    monkeypatch.setattr(desktop_rfb.subprocess, "Popen", spawn)
    streams = RfbSessions(":7", {"PATH": "/usr/bin:/bin"})
    try:
        first = streams.exchange(packet())
        greeting = base64.b64decode(first["data"])
        seq = 0
        while len(greeting) < 12 and seq < 5:
            seq += 1
            greeting += base64.b64decode(streams.exchange(packet("exchange", seq))["data"])
        assert greeting == b"RFB 003.008\n"
        seq += 1
        response = streams.exchange(packet("exchange", seq, b"\0binary\xff"))
        assert base64.b64decode(response["data"]) == b"\0binary\xff"
        process = streams.sessions["a" * 32][1]
        with pytest.raises(ValueError, match="sequence"):
            streams.exchange(packet("exchange", seq, b"must-not-replay"))
        assert process.poll() is not None
        assert not streams.sessions
        with pytest.raises(ValueError, match="expired"):
            streams.exchange(packet("exchange", seq + 1))
    finally:
        streams.close()


def test_stream_is_owned_and_revoked_during_read():
    views = ComputerViews()
    entered, finish = threading.Event(), threading.Event()

    def read(args):
        entered.set()
        assert finish.wait(2)
        return {"data": "cHJpdmF0ZQ==", "sequence": 0}

    generation = views.bind("owner", "owl", capture=lambda: b"", alive=lambda: True, rfb=read)
    for owner, bot, session in [
        ("other", "owl", generation),
        ("owner", "other", generation),
        ("owner", "owl", "b" * 32),
    ]:
        with pytest.raises(ValueError):
            views.live_display(owner, bot, session, packet())
    result = []

    def run():
        try:
            result.append(views.live_display("owner", "owl", generation, packet()))
        except ValueError:
            result.append("revoked")

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(2)
    views.revoke("owner", "owl", generation)
    finish.set()
    worker.join(2)
    assert result == ["revoked"]


def test_live_routes_authenticate_and_status_does_not_capture(tmp_path):
    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    views = ComputerViews()
    with TestClient(create_app(settings, computer_views=views)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Viewer", "purpose": "Test"}).json()["id"]

        def forbidden_capture():
            raise AssertionError("Status must not capture the desktop")

        generation = views.bind(
            settings.owner,
            bot,
            capture=forbidden_capture,
            alive=lambda: True,
            rfb=lambda args: {"sequence": args["sequence"], "data": ""},
        )
        path = f"/v1/bots/{bot}/computer"
        status = client.get(path + "/status")
        assert status.status_code == 200
        assert status.json()["live_display"] is True
        body = {"session_id": generation, "stream": packet()}
        assert client.post(path + "/live", json=body).json() == {"sequence": 0, "data": ""}
        assert client.post(path + "/live", json={**body, "session_id": "b" * 32}).status_code == 409
        malformed = client.post(
            path + "/live", json={**body, "stream": {"private": "private input"}}
        )
        assert malformed.status_code == 409 and "private input" not in malformed.text
        client.headers.clear()
        assert client.post(path + "/live", json=body).status_code == 401
        assert client.get(path + "/status").status_code == 401
