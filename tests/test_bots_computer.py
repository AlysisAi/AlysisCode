from __future__ import annotations

import io
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.computer import ComputerViews, LinuxScreenCapture


def png(color="blue"):
    result = io.BytesIO()
    Image.new("RGB", (16, 12), color).save(result, format="PNG")
    return result.getvalue()


def test_frames_require_bot_owner_and_binding(tmp_path):
    views = ComputerViews()
    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    app = create_app(settings, computer_views=views)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Viewer", "purpose": "Test"}).json()
        other = client.post("/v1/bots", json={"name": "Other", "purpose": "Test"}).json()
        path = f"/v1/bots/{bot['id']}/computer"
        assert client.get(path).json() == {"state": "not_running"}
        views.bind("another-owner", bot["id"], capture=png, alive=lambda: True)
        assert client.get(path).json() == {"state": "not_running"}
        generation = views.bind(settings.owner, bot["id"], capture=png, alive=lambda: True)
        response = client.get(path)
        assert response.headers["Cache-Control"] == "no-store"
        frame = response.json()
        assert frame["state"] == "live" and frame["session_id"] == generation
        assert frame["width"] == 16 and frame["height"] == 12
        assert frame["image"].startswith("data:image/jpeg;base64,")
        assert client.get(f"/v1/bots/{other['id']}/computer").json() == {"state": "not_running"}
        assert client.get("/v1/bots/missing/computer").status_code == 404
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)(path).status_code == 405
        assert client.post(path + "/input", json={"key": "a"}).status_code == 404
        client.headers.clear()
        assert client.get(path).status_code == 401
        views.revoke(settings.owner, bot["id"], generation)
        assert views.frame(settings.owner, bot["id"]) == {"state": "not_running"}


def test_revocation_during_capture_withholds_old_screen():
    views = ComputerViews()
    entered, release = threading.Event(), threading.Event()

    def capture():
        entered.set()
        assert release.wait(2)
        return png()

    old = views.bind("owner", "bot", capture=capture, alive=lambda: True)
    result = []
    thread = threading.Thread(target=lambda: result.append(views.frame("owner", "bot")))
    thread.start()
    assert entered.wait(2)
    try:
        assert views.frame("owner", "bot") == {"state": "connecting"}
        views.revoke("owner", "bot", old)
        new = views.bind("owner", "bot", capture=lambda: png("red"), alive=lambda: True)
        views.revoke("owner", "bot", old)
        assert views.frame("owner", "bot")["session_id"] == new
    finally:
        release.set()
        thread.join(2)
    assert result == [{"state": "disconnected"}]


def test_control_route_never_echoes_malformed_private_input(tmp_path, monkeypatch):
    from starlette.requests import ClientDisconnect, Request

    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    views = ComputerViews()
    app = create_app(settings, computer_views=views)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Viewer", "purpose": "Test"}).json()
        inputs = []
        session = views.bind(
            settings.owner, bot["id"], capture=png, alive=lambda: True, human_input=inputs.append
        )
        path = f"/v1/bots/{bot['id']}/computer/control"
        identity = {"client_id": "a" * 32, "session_id": session}
        for body in (
            "synthetic-private-value",
            ["synthetic-private-value"],
            {"event": {"text": "synthetic-private-value"}},
        ):
            response = client.post(path, json=body)
            assert response.status_code == 422 and "synthetic-private-value" not in response.text
        response = client.post(path, content='{"text":"synthetic-private-value"')
        assert response.status_code == 422 and "synthetic-private-value" not in response.text
        assert (
            client.post(path, json={**identity, "action": "take"}).json()["state"] == "controlling"
        )
        assert (
            client.post(
                path,
                json={
                    **identity,
                    "action": "input",
                    "sequence": 1,
                    "event": {"kind": "text", "text": "synthetic-private-value"},
                },
            ).status_code
            == 200
        )
        assert inputs == [{"kind": "text", "text": "synthetic-private-value"}]
        assert (
            client.post(path, json={**identity, "action": "resume"}).json()["state"] == "returning"
        )
        with app.state.runtime._computer_open_lock:
            workers = list(app.state.runtime._computer_openers.values())
        for worker in workers:
            worker.join(3)

        async def disconnected(_request):
            raise ClientDisconnect()

        with monkeypatch.context() as patch:
            patch.setattr(Request, "json", disconnected)
            response = client.post(path, json={**identity, "action": "take"})
        assert response.status_code == 400
        assert inputs == [{"kind": "text", "text": "synthetic-private-value"}, {"kind": "release"}]
        client.headers.clear()
        assert client.post(path, json={**identity, "action": "take"}).status_code == 401


@pytest.mark.parametrize(
    "capture,alive",
    [
        (lambda: b"not an image", lambda: True),
        (lambda: b"x" * 4_000_001, lambda: True),
        (png, lambda: False),
    ],
)
def test_failed_capture_never_returns_a_previous_frame(capture, alive):
    views = ComputerViews()
    views.bind("owner", "bot", capture=capture, alive=alive)
    assert views.frame("owner", "bot") == {"state": "disconnected"}


def test_invalid_capture_and_duplicate_binding_are_rejected():
    for display in ("remote:0", ":0;command", "", ":0\n"):
        with pytest.raises(ValueError):
            LinuxScreenCapture(display)
    views = ComputerViews()
    views.bind("owner", "bot", capture=png, alive=lambda: True)
    with pytest.raises(ValueError):
        views.bind("owner", "bot", capture=png, alive=lambda: True)
    assert ComputerViews().frame("owner", "bot") == {"state": "not_running"}


def test_slow_capture_is_not_published_as_live(monkeypatch):
    ticks = iter([0, 6])
    monkeypatch.setattr("alysis_code.bots.computer.time.monotonic", lambda: next(ticks))
    views = ComputerViews()
    views.bind("owner", "bot", capture=png, alive=lambda: True)
    assert views.frame("owner", "bot") == {"state": "disconnected"}


def test_combined_surfaces_share_authority_and_reject_a_capture_after_switch():
    views = ComputerViews()
    views.claim("owner", "bot", "task", "browser")
    views.claim("owner", "bot", "task", "desktop")
    views.release_claim("owner", "bot", "task", "browser")
    with pytest.raises(ValueError, match="another task"):
        views.claim("owner", "bot", "other-task", "browser")
    entered, release = threading.Event(), threading.Event()

    def capture():
        entered.set()
        assert release.wait(2)
        return png()

    browser = views.bind("owner", "bot", surface="browser", capture=capture, alive=lambda: True)
    desktop = views.bind("owner", "bot", surface="desktop", capture=png, alive=lambda: True)
    views.activate("owner", "bot", browser)
    result = []
    thread = threading.Thread(target=lambda: result.append(views.frame("owner", "bot")))
    thread.start()
    assert entered.wait(2)
    try:
        views.activate("owner", "bot", desktop)
        assert views.frame("owner", "bot")["session_id"] == desktop
    finally:
        release.set()
        thread.join(2)
    assert result == [{"state": "disconnected"}]
    views.revoke("owner", "bot", browser)
    assert views.frame("owner", "bot")["session_id"] == desktop
    views.release_task("owner", "task", "desktop")
    views.claim("owner", "bot", "other-task", "browser")


def test_viewer_removes_embedded_image_metadata_before_sending_frames():
    import base64

    from PIL import PngImagePlugin

    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", "private screen metadata")
    source = io.BytesIO()
    Image.new("RGB", (80, 40), "blue").save(source, format="PNG", pnginfo=metadata)
    views = ComputerViews()
    views.bind("owner", "bot", capture=source.getvalue, alive=lambda: True)
    frame = views.frame("owner", "bot")
    with Image.open(io.BytesIO(base64.b64decode(frame["image"].split(",", 1)[1]))) as image:
        assert image.size == (80, 40) and "comment" not in image.info


@pytest.mark.parametrize("mounted", [False, True])
def test_frame_compression_is_negotiated_and_scoped_away_from_other_api_responses(
    tmp_path, mounted
):
    import random
    from contextlib import ExitStack

    from starlette.applications import Starlette
    from starlette.routing import Mount

    views = ComputerViews()
    settings = Settings(
        tmp_path / "db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    app = create_app(settings, computer_views=views)
    image = Image.frombytes("RGB", (200, 100), random.Random(17).randbytes(200 * 100 * 3))
    pixels = io.BytesIO()
    image.save(pixels, format="PNG")
    with TestClient(app) as client, ExitStack() as clients:
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Frame", "purpose": "Compression"}).json()["id"]
        views.bind(settings.owner, bot, capture=pixels.getvalue, alive=lambda: True)
        path = f"/v1/bots/{bot}/computer"
        if mounted:
            client = clients.enter_context(
                TestClient(Starlette(routes=[Mount("/accounts/test", app=app)]))
            )
            client.headers["Authorization"] = "Bearer " + settings.token
            path = "/accounts/test" + path
        raw = client.get(path, headers={"Accept-Encoding": "identity"})
        compressed = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in raw.headers
        assert compressed.headers["content-encoding"] == "gzip"
        assert int(compressed.headers["content-length"]) < len(compressed.content)
        assert compressed.headers["cache-control"] == "no-store"
        assert compressed.json()["image"] == raw.json()["image"]
        assert "accept-encoding" in compressed.headers["vary"].lower()
        status = client.get(("/accounts/test" if mounted else "") + "/v1/status")
        assert status.status_code == 200 and "content-encoding" not in status.headers
        client.headers.pop("Authorization")
        assert client.get(path).status_code == 401
