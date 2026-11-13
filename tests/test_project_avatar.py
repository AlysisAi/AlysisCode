from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from alysis_code import account_login
from alysis_code.cli import app


def _coin() -> dict[str, list[str]]:
    rows = [["."] * 32 for _ in range(32)]
    for y, line in enumerate([".dddd.", "d1111d", "d1221d", "d1221d", "d1111d", ".dddd."]):
        for x, char in enumerate(line):
            if char != ".":
                rows[22 + y][4 + x] = char
    return {"colors": ["#F7C948", "#c98a12"], "pixels": ["".join(row) for row in rows]}


@pytest.fixture
def endpoint(monkeypatch):
    state = {"status": 200, "body": None, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, self.headers.get("Authorization"), sent))
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "/credential-leak")
            self.end_headers()
            body = (
                state["body"]
                if state["body"] is not None
                else {"schema_version": 1, "item": _coin()}
            )
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setenv("ALYSIS_SUPABASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(
        account_login,
        "load_persisted_profile_keys",
        lambda: {"alysis": "slk_project_avatar_fixture", "other": "other_provider_fixture"},
    )
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_one_request_with_the_saved_alysis_key_returns_the_accessory(endpoint):
    item = account_login.generate_project_avatar(
        name="  Family\tbudget ", about="Bills and   savings"
    )
    assert item == {"colors": ["#f7c948", "#c98a12"], "pixels": _coin()["pixels"]}
    assert endpoint["requests"] == [
        (
            "/functions/v1/project-avatar",
            "Bearer slk_project_avatar_fixture",
            {"name": "Family budget", "about": "Bills and savings"},
        )
    ]


def test_an_empty_description_is_left_out(endpoint):
    account_login.generate_project_avatar(name="Travel")
    assert endpoint["requests"][0][2] == {"name": "Travel"}


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 81},
        {"name": "ok", "about": "x" * 501},
        {"name": "a\x07b"},
    ],
)
def test_bad_names_are_refused_before_anything_is_sent(endpoint, change):
    with pytest.raises(account_login.ProjectAvatarError) as error:
        account_login.generate_project_avatar(**change)
    assert error.value.reason == "invalid"
    assert endpoint["requests"] == []


def test_signed_out_sends_nothing(endpoint, monkeypatch):
    monkeypatch.setattr(account_login, "load_persisted_profile_keys", lambda: {})
    with pytest.raises(account_login.ProjectAvatarError) as error:
        account_login.generate_project_avatar(name="Travel")
    assert error.value.reason == "signed_out"
    assert endpoint["requests"] == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "invalid_key"),
        (404, "unsupported"),
        (422, "refused"),
        (429, "rate_limited"),
        (503, "unavailable"),
        (302, "unavailable"),
    ],
)
def test_server_answers_become_stable_reasons(endpoint, status, reason):
    endpoint["status"] = status
    endpoint["body"] = {"error": "x"}
    with pytest.raises(account_login.ProjectAvatarError) as error:
        account_login.generate_project_avatar(name="Travel")
    assert error.value.reason == reason


def _with(**change):
    item = _coin()
    item.update(change)
    return {"schema_version": 1, "item": item}


@pytest.mark.parametrize(
    "body",
    [
        {"schema_version": 2, "item": _coin()},
        _with(colors=[]),
        _with(colors=["gold"]),
        _with(colors=["#000000"] * 5),
        _with(pixels=_coin()["pixels"][1:]),
        _with(pixels=[*_coin()["pixels"][1:], "A" * 32]),
        _with(colors=["#f7c948"]),
        _with(pixels=["." * 32] * 32),
        _with(pixels=["1" * 32] * 32),
        {"schema_version": 1, "item": {**_coin(), "extra": True}},
    ],
)
def test_anything_but_an_accessory_layer_is_refused(endpoint, body):
    endpoint["body"] = body
    with pytest.raises(account_login.ProjectAvatarError) as error:
        account_login.generate_project_avatar(name="Travel")
    assert error.value.reason == "unavailable"


def test_the_command_answers_with_the_layer_in_json(endpoint):
    result = CliRunner().invoke(
        app, ["project-avatar", "--json", "--name=Travel planner", "--about=Trips"]
    )
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer["ok"] is True
    assert answer["item"]["pixels"] == _coin()["pixels"]


def test_the_command_reports_a_stable_reason(endpoint):
    endpoint["status"] = 429
    result = CliRunner().invoke(app, ["project-avatar", "--json", "--name=Travel"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "rate_limited"
