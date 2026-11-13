from __future__ import annotations

import base64
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code import account_login
from alysis_code.cli import app
from alysis_code.cli_impl.commands.root import _whoami_payload
from alysis_code.config import AppConfig

STATUS = json.loads(
    (Path(__file__).parent / "fixtures/account-status.json").read_text(encoding="utf-8")
)
OWNER = "6f1c1f8e-2b7a-4a51-9a51-3f0f2a4f5c11"
PHOTO = "0d6b2b8a-4c35-4d3e-8f0e-7d1c2b3a4f56"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x07" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x03" * 64


@pytest.fixture
def endpoint(monkeypatch):
    state = {"status": 200, "body": None, "requests": [], "account": copy.deepcopy(STATUS)}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, body):
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "/credential-leak")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):  # noqa: N802
            state["requests"].append(("GET", self.path, self.headers.get("Authorization"), None))
            self._reply(state["account"])

        def do_POST(self):  # noqa: N802
            sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(("POST", self.path, self.headers.get("Authorization"), sent))
            self._reply(
                state["body"]
                if state["body"] is not None
                else {
                    "schema_version": 1,
                    "display_name": sent.get("display_name", "Alex"),
                    "avatar_url": (
                        f"{state['base']}/storage/v1/object/public/avatars/{OWNER}/{PHOTO}.webp"
                        if sent.get("photo")
                        else None
                    ),
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("ALYSIS_SUPABASE_URL", state["base"])
    monkeypatch.setattr(
        account_login,
        "load_persisted_profile_keys",
        lambda: {"alysis": "slk_account_profile_fixture", "other": "other_provider_fixture"},
    )
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_a_new_name_is_sent_with_the_saved_alysis_key_only(endpoint):
    profile = account_login.update_account_profile(display_name="  Maria Papadopoulou ")
    assert profile == account_login.AccountProfile("Maria Papadopoulou", None)
    assert endpoint["requests"] == [
        (
            "POST",
            "/functions/v1/account-profile",
            "Bearer slk_account_profile_fixture",
            {"display_name": "Maria Papadopoulou"},
        )
    ]


def test_a_photo_is_sent_as_base64_and_its_account_link_comes_back(endpoint):
    profile = account_login.update_account_profile(photo=WEBP)
    assert profile.avatar_url == (
        f"{endpoint['base']}/storage/v1/object/public/avatars/{OWNER}/{PHOTO}.webp"
    )
    assert endpoint["requests"][0][3] == {"photo": base64.b64encode(WEBP).decode()}
    account_login.update_account_profile(display_name="Maria", remove_photo=True)
    assert endpoint["requests"][1][3] == {"display_name": "Maria", "photo": None}


def test_a_photo_link_outside_the_account_photos_is_never_passed_on(endpoint):
    for link in (
        "https://elsewhere.example/storage/v1/object/public/avatars/a/b.webp",
        f"{endpoint['base']}/storage/v1/object/public/other/{OWNER}/{PHOTO}.webp",
        f"{endpoint['base']}/storage/v1/object/public/avatars/{OWNER}/../{PHOTO}.webp",
        f"{endpoint['base']}/storage/v1/object/public/avatars/{OWNER}/{PHOTO}.svg",
        42,
    ):
        endpoint["body"] = {"schema_version": 1, "display_name": "Alex", "avatar_url": link}
        assert account_login.update_account_profile(display_name="Alex").avatar_url is None
        endpoint["account"]["avatar_url"] = link
        status = account_login.fetch_trial_status(AppConfig())
        assert status is not None and status.avatar_url is None


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": "x" * 61},
        {"display_name": "a\x07b"},
        {"photo": b""},
        {"photo": b"<svg/>"},
        {"photo": b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 262_200},
        {"photo": PNG, "remove_photo": True},
    ],
)
def test_bad_changes_are_refused_before_anything_is_sent(endpoint, change):
    with pytest.raises(account_login.AccountProfileError) as error:
        account_login.update_account_profile(**change)
    assert error.value.reason == "invalid"
    assert endpoint["requests"] == []


def test_signed_out_sends_nothing(endpoint, monkeypatch):
    monkeypatch.setattr(account_login, "load_persisted_profile_keys", lambda: {})
    with pytest.raises(account_login.AccountProfileError) as error:
        account_login.update_account_profile(display_name="Alex")
    assert error.value.reason == "signed_out"
    assert endpoint["requests"] == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, "invalid"),
        (401, "invalid_key"),
        (404, "unsupported"),
        (429, "rate_limited"),
        (500, "unavailable"),
        (302, "unavailable"),
    ],
)
def test_refusals_have_stable_reasons_and_redirects_never_carry_the_key(endpoint, status, reason):
    endpoint["status"] = status
    with pytest.raises(account_login.AccountProfileError) as error:
        account_login.update_account_profile(display_name="Alex")
    assert error.value.reason == reason
    assert "slk_" not in str(error.value)
    assert [request[1] for request in endpoint["requests"]] == ["/functions/v1/account-profile"]
    assert account_login.load_persisted_profile_keys()["alysis"] == "slk_account_profile_fixture"


def test_unknown_answers_are_not_taken_as_saved(endpoint):
    endpoint["body"] = {"schema_version": 2, "display_name": "Alex"}
    with pytest.raises(account_login.AccountProfileError) as error:
        account_login.update_account_profile(display_name="Alex")
    assert error.value.reason == "unavailable"


def test_whoami_json_carries_the_account_photo(endpoint, monkeypatch):
    monkeypatch.setattr(
        account_login,
        "login_status",
        lambda cfg: account_login.LoginStatus(
            True, "alysis", "https://example.test/v1", False, "x"
        ),
    )
    link = f"{endpoint['base']}/storage/v1/object/public/avatars/{OWNER}/{PHOTO}.png"
    endpoint["account"]["avatar_url"] = link
    assert _whoami_payload(AppConfig())["avatar_url"] == link
    endpoint["account"].pop("avatar_url")
    assert _whoami_payload(AppConfig())["avatar_url"] is None


def test_cli_saves_name_and_photo_and_reports_json(endpoint, tmp_path):
    picture = tmp_path / "me.png"
    picture.write_bytes(PNG)
    result = CliRunner().invoke(
        app, ["account-profile", "--name", "Maria", "--photo", str(picture), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "display_name": "Maria",
        "avatar_url": f"{endpoint['base']}/storage/v1/object/public/avatars/{OWNER}/{PHOTO}.webp",
    }
    assert endpoint["requests"][0][3] == {
        "display_name": "Maria",
        "photo": base64.b64encode(PNG).decode(),
    }
    removed = CliRunner().invoke(app, ["account-profile", "--remove-photo", "--json"])
    assert json.loads(removed.stdout)["avatar_url"] is None


def test_cli_failures_exit_non_zero_with_a_reason(endpoint, tmp_path):
    missing = CliRunner().invoke(
        app, ["account-profile", "--photo", str(tmp_path / "missing.png"), "--json"]
    )
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["error"] == "invalid"
    endpoint["status"] = 429
    limited = CliRunner().invoke(app, ["account-profile", "--name", "Maria", "--json"])
    assert limited.exit_code == 1
    assert json.loads(limited.stdout) == {
        "ok": False,
        "error": "rate_limited",
        "detail": "Too many changes in a short time. Wait a minute and try again.",
    }
    assert "slk_" not in limited.stdout
