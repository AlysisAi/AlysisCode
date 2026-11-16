from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from alysis_code import account_login
from alysis_code.cli import app

WEEKLY = "6f1c1f8e-2b7a-4a51-9a51-3f0f2a4f5c11"
SHORT = "0d6b2b8a-4c35-4d3e-8f0e-7d1c2b3a4f56"
RESETS = [
    {"id": SHORT, "kind": "weekly", "label": None, "expires_at": "2026-11-23T21:59:00+00:00"},
    {
        "id": WEEKLY,
        "kind": "weekly",
        "label": "Owls launch thanks",
        "expires_at": "2026-11-30T21:59:00+00:00",
    },
]


@pytest.fixture
def endpoint(monkeypatch):
    state = {"status": 200, "body": None, "requests": []}

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
            self._reply(
                state["body"]
                if state["body"] is not None
                else {"schema_version": 1, "resets": RESETS}
            )

        def do_POST(self):  # noqa: N802
            sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(("POST", self.path, self.headers.get("Authorization"), sent))
            used = next((r for r in RESETS if r["id"] == sent.get("reset_id")), RESETS[0])
            self._reply(
                state["body"]
                if state["body"] is not None
                else {
                    "schema_version": 1,
                    "kind": used["kind"],
                    "used_at": "2026-11-16T10:00:00.123+00:00",
                    "resets": [r for r in RESETS if r is not used],
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setenv("ALYSIS_SUPABASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(
        account_login,
        "load_persisted_profile_keys",
        lambda: {"alysis": "slk_limit_reset_fixture", "other": "other_provider_fixture"},
    )
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_lists_resets_with_the_alysis_key_only(endpoint):
    resets = account_login.list_limit_resets()
    assert [(r.id, r.kind, r.label) for r in resets] == [
        (SHORT, "weekly", None),
        (WEEKLY, "weekly", "Owls launch thanks"),
    ]
    assert endpoint["requests"] == [
        ("GET", "/functions/v1/limit-reset", "Bearer slk_limit_reset_fixture", None)
    ]


def test_uses_one_reset_and_returns_what_is_left(endpoint):
    result = account_login.use_limit_reset(f"  {WEEKLY.upper()} ")
    assert (result.kind, result.used_at) == ("weekly", "2026-11-16T10:00:00.123+00:00")
    assert [r.id for r in result.resets] == [SHORT]
    assert endpoint["requests"][-1][3] == {"reset_id": WEEKLY}


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, {"error": "invalid_key"}, "invalid_key"),
        (404, {"error": "reset_unavailable"}, "unavailable"),
        (409, {"error": "reset_already_used"}, "already_used"),
        (410, {"error": "reset_expired"}, "expired"),
        (429, {"error": "rate_limited"}, "rate_limited"),
        # An account server without the endpoint answers its own 404.
        (404, {"code": "NOT_FOUND", "message": "Requested function was not found"}, "unsupported"),
        (503, {"error": "unavailable"}, "unavailable"),
        # The saved key is never sent on to a redirect target.
        (302, {}, "unavailable"),
    ],
)
def test_refusals_keep_stable_reasons(endpoint, status, body, reason):
    endpoint["status"], endpoint["body"] = status, body
    with pytest.raises(account_login.LimitResetError) as error:
        account_login.use_limit_reset(WEEKLY)
    assert error.value.reason == reason
    assert all(path == "/functions/v1/limit-reset" for _, path, _, _ in endpoint["requests"])


@pytest.mark.parametrize(
    "body",
    [
        {"schema_version": 2, "resets": RESETS},
        {"schema_version": 1, "resets": [{**RESETS[0], "kind": 5}]},
        {"schema_version": 1, "resets": [{**RESETS[0], "id": "not-a-reset"}]},
        {"schema_version": 1, "resets": [{**RESETS[0], "expires_at": "soon"}]},
        {"schema_version": 1},
    ],
)
def test_unexpected_answers_are_unavailable(endpoint, body):
    endpoint["body"] = body
    with pytest.raises(account_login.LimitResetError) as error:
        account_login.list_limit_resets()
    assert error.value.reason == "unavailable"


def test_kinds_this_engine_does_not_know_are_left_out(endpoint):
    endpoint["body"] = {
        "schema_version": 1,
        "resets": [{**RESETS[0], "kind": "monthly", "id": "not checked"}, RESETS[1]],
    }
    assert [r.id for r in account_login.list_limit_resets()] == [WEEKLY]


def test_bad_ids_and_signed_out_never_reach_the_server(endpoint, monkeypatch):
    for bad in ["", "nope", WEEKLY + "0", "../" + WEEKLY]:
        with pytest.raises(account_login.LimitResetError) as error:
            account_login.use_limit_reset(bad)
        assert error.value.reason == "invalid"
    monkeypatch.setattr(account_login, "load_persisted_profile_keys", lambda: {"other": "x"})
    with pytest.raises(account_login.LimitResetError) as error:
        account_login.list_limit_resets()
    assert error.value.reason == "signed_out"
    assert endpoint["requests"] == []


def test_cli_json_contract_for_the_desktop_app(endpoint):
    runner = CliRunner()
    listed = runner.invoke(app, ["limit-reset", "--json"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output) == {"ok": True, "resets": RESETS}

    used = runner.invoke(app, ["limit-reset", "--json", f"--use={WEEKLY}"])
    assert used.exit_code == 0, used.output
    assert json.loads(used.output) == {
        "ok": True,
        "kind": "weekly",
        "used_at": "2026-11-16T10:00:00.123+00:00",
        "resets": [RESETS[0]],
    }
    # --json never asks: the app shows its own confirmation first.
    assert [method for method, *_ in endpoint["requests"]] == ["GET", "POST"]

    endpoint["status"], endpoint["body"] = 409, {"error": "reset_already_used"}
    refused = runner.invoke(app, ["limit-reset", "--json", f"--use={WEEKLY}"])
    assert refused.exit_code == 1
    assert json.loads(refused.output) == {
        "ok": False,
        "error": "already_used",
        "detail": "This reset was already used.",
    }


def test_cli_asks_before_using_a_reset(endpoint):
    runner = CliRunner()
    shown = runner.invoke(app, ["limit-reset"])
    assert shown.exit_code == 0
    assert "Weekly reset" in shown.output and WEEKLY in shown.output

    declined = runner.invoke(app, ["limit-reset", f"--use={WEEKLY}"], input="n\n")
    assert declined.exit_code == 1
    assert "weekly and 5-hour limits go back to full" in declined.output
    assert [method for method, *_ in endpoint["requests"]] == ["GET", "GET"]

    accepted = runner.invoke(app, ["limit-reset", f"--use={WEEKLY}"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert endpoint["requests"][-1][0] == "POST"

    direct = runner.invoke(app, ["limit-reset", f"--use={SHORT}", "--yes"])
    assert direct.exit_code == 0
    assert "Your weekly and 5-hour limits go back to full now." in direct.output
    assert "5-hour reset" not in shown.output
