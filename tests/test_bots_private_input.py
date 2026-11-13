import base64
import io
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_bots_cloud_desktop import Provider

from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import (
    CommandReceipt,
    ComputerSpec,
    ComputeUncertain,
    validate_command,
)
from alysis_code.bots.desktop_linux import ComputerActionUncertain, DesktopError
from alysis_code.bots.private_input import PrivateInput, seal_input


def test_max_unicode_text_fits_the_private_helpers_bounded_stdin(monkeypatch):
    from alysis_code.bots import human_input

    received = []
    event = {"kind": "text", "text": "🔒" * 8000}
    human_input.validate(event)

    def start(argv, **_kwargs):
        assert event["text"] not in str(argv)
        return SimpleNamespace(
            stdin=SimpleNamespace(write=received.append, close=lambda: None),
            stdout=io.BytesIO(b"consumed\n"),
        )

    monkeypatch.setattr(human_input.subprocess, "Popen", start)
    monkeypatch.setattr(human_input.select, "select", lambda readers, *_: (readers, [], []))
    assert human_input.HumanInput(":77")(event) == {"dispatched": True}
    assert len(received) == 1 and len(received[0]) < 65537
    assert json.loads(received[0]) == event


def test_private_input_roundtrip_rejects_replay_and_cross_session():
    session, request = uuid4().hex, uuid4().hex
    receiver = PrivateInput(session)
    event = {"surface": "browser", "event": {"kind": "text", "text": "Καλημέρα 東京"}}
    envelope = seal_input(receiver.public_key, session, request, 1, event)
    assert "Καλημέρα" not in json.dumps(envelope, ensure_ascii=False)
    with pytest.raises(ValueError, match="Private computer input rejected"):
        receiver.open(uuid4().hex, envelope)
    other = PrivateInput(uuid4().hex)
    with pytest.raises(ValueError, match="Private computer input rejected"):
        other.open(request, envelope)
    assert receiver.open(request, envelope) == event
    with pytest.raises(ValueError, match="Private computer input rejected"):
        receiver.open(request, envelope)


@pytest.mark.parametrize("change", ["ciphertext", "nonce", "sequence", "plaintext"])
def test_tampering_never_consumes_input_or_echoes_private_content(change):
    session, request = uuid4().hex, uuid4().hex
    receiver = PrivateInput(session)
    event = {"surface": "desktop", "event": {"kind": "text", "text": "private-canary"}}
    original = seal_input(receiver.public_key, session, request, 1, event)
    altered = dict(original)
    if change == "sequence":
        altered[change] = 2
    elif change == "plaintext":
        altered = event
    else:
        raw = bytearray(base64.b64decode(altered[change]))
        raw[-1] ^= 1
        altered[change] = base64.b64encode(raw).decode()
    with pytest.raises(ValueError) as error:
        receiver.open(request, altered)
    assert "canary" not in str(error.value)
    assert receiver.open(request, original) == event


class InputProvider(Provider):
    def __init__(self):
        super().__init__()
        self.receiver = None
        self.received = []

    def run(self, ref, argv, *, timeout):
        if argv[-2] == "start":
            self.receiver = PrivateInput(ref.identity.allocation_id)
            return super().run(ref, argv, timeout=timeout)
        request = json.loads(base64.b64decode(argv[-1]))
        validate_command(argv, timeout)
        self.actions.append(argv)
        result = {}
        if request["operation"] == "human_input_key":
            result = {"public_key": self.receiver.public_key}
        if request["operation"] == "human_input":
            self.received.append(self.receiver.open(request["id"], request["arguments"]))
            if self.mode == "lost-reply":
                raise ComputeUncertain("Lost input reply")
            result = {"dispatched": True}
        return CommandReceipt(
            0,
            json.dumps({"id": request["id"], "session": request["session"], "result": result}),
            "",
        )


@pytest.mark.parametrize("surface", ["browser", "desktop"])
def test_cloud_input_is_encrypted_before_operator_command_and_released_surface_is_fenced(
    tmp_path, surface
):
    provider = InputProvider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, browser_enabled=True
    )
    browser = factory.browser("bot", lambda: False)
    desktop = factory("bot", tmp_path, cancel=lambda: False)
    selected = browser if surface == "browser" else desktop
    private_text = "🔒" * 8000  # Largest admitted text, including four-byte Unicode.
    try:
        assert selected.human_input({"kind": "text", "text": private_text}) == {"dispatched": True}
        assert provider.received == [
            {"surface": surface, "event": {"kind": "text", "text": private_text}}
        ]
        assert private_text not in json.dumps(provider.actions, ensure_ascii=False)
        wire = json.loads(base64.b64decode(provider.actions[-1][-1]))
        assert "event" not in wire["arguments"]
        assert private_text not in json.dumps(wire, ensure_ascii=False)
        selected.finish()
        count = len(provider.actions)
        with pytest.raises(DesktopError, match="released"):
            selected.human_input({"kind": "click", "x": 1, "y": 1, "button": 1})
        assert len(provider.actions) == count
    finally:
        browser.close()
        desktop.close()


def test_lost_input_receipt_retires_computer_and_never_repeats_click(tmp_path):
    provider = InputProvider()
    factory = CloudDesktopFactory("owner", provider, ComputerSpec("image"), tmp_path)
    desktop = factory("bot", tmp_path, cancel=lambda: False)
    provider.mode = "lost-reply"
    with pytest.raises(ComputerActionUncertain):
        desktop.human_input({"kind": "click", "x": 1, "y": 1, "button": 1})
    assert len(provider.received) == 1
    assert provider.status(desktop.ref).state == "missing"
    with pytest.raises(DesktopError):
        desktop.human_input({"kind": "click", "x": 1, "y": 1, "button": 1})
    assert len(provider.received) == 1
    desktop.close()


def test_rejected_encoding_does_not_poison_the_next_private_input(tmp_path):
    provider = InputProvider()
    factory = CloudDesktopFactory("owner", provider, ComputerSpec("image"), tmp_path)
    desktop = factory("bot", tmp_path, cancel=lambda: False)
    try:
        with pytest.raises(ValueError, match="transport allowance"):
            desktop.human_input({"kind": "text", "text": "🔒" * 9000})
        assert provider.received == []
        assert desktop.human_input({"kind": "release"}) == {"dispatched": True}
        assert provider.received == [{"surface": "desktop", "event": {"kind": "release"}}]
    finally:
        desktop.close()
