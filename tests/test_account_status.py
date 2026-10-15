from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from alysis_code import account_login
from alysis_code.cli_impl.commands.root import _whoami_payload
from alysis_code.config import AppConfig

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/account-status.json").read_text(encoding="utf-8")
)


@pytest.fixture
def endpoint(monkeypatch):
    state = {"body": copy.deepcopy(FIXTURE), "status": 200, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            state["requests"].append((self.path, self.headers.get("Authorization")))
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "/credential-leak")
            self.end_headers()
            self.wfile.write(json.dumps(state["body"]).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ALYSIS_SUPABASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(
        account_login,
        "load_persisted_profile_keys",
        lambda: {
            "alysis": "slk_account_status_fixture",
            "other": "other_provider_fixture",
        },
    )
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_live_billing_uses_saved_alysis_key_even_with_another_active_provider(endpoint):
    cfg = AppConfig()
    cfg.extra_fields["active_profile"] = "other"
    status = account_login.fetch_trial_status(cfg)
    assert status is not None
    assert status.email == FIXTURE["email"]
    assert status.billing == FIXTURE["billing"]
    assert status.tokens_used is None
    assert "38.0 credits left" in account_login.format_trial_status_line(status)
    assert endpoint["requests"] == [
        ("/functions/v1/account-status", "Bearer slk_account_status_fixture"),
    ]


def test_whoami_json_carries_billing_without_credentials(endpoint, monkeypatch):
    monkeypatch.setattr(
        account_login,
        "login_status",
        lambda cfg: account_login.LoginStatus(
            True,
            "alysis",
            "https://example.test/v1",
            False,
            "private_preview",
        ),
    )
    payload = _whoami_payload(AppConfig())
    assert payload["authenticated"] is True
    assert payload["billing_status"] == "available"
    assert payload["billing"] == FIXTURE["billing"]
    assert payload["display_name"] == "Alex"
    assert "tokens_used" not in payload
    assert "slk_" not in json.dumps(payload)
    assert "private_preview" not in json.dumps(payload)


@pytest.mark.parametrize("status", [401, 404, 429, 500, 302])
def test_failed_reads_and_redirects_do_not_change_signin_or_forward_credentials(endpoint, status):
    endpoint["status"] = status
    assert account_login.fetch_trial_status(AppConfig()) is None
    assert len(endpoint["requests"]) == 1
    assert endpoint["requests"][0][0] == "/functions/v1/account-status"
    assert account_login.load_persisted_profile_keys()["alysis"] == "slk_account_status_fixture"


@pytest.mark.parametrize(
    "patch",
    [
        {"balance_microusd": None},
        {"balance_microusd": True},
        {"cap_5h_microusd": -1},
        {"burn_7d_microusd": -1},
        {"free_allowance_microusd": 2**60},
        {"free_allowance_microusd": None},
        {"model_promotions": [{"model": "m", "label": "M", "multiplier": 0}]},
    ],
)
def test_malformed_usage_is_unavailable_never_an_invented_zero(endpoint, patch):
    endpoint["body"]["billing"].update(patch)
    assert account_login.fetch_trial_status(AppConfig()) is None


def test_missing_key_does_not_make_a_request(endpoint, monkeypatch):
    monkeypatch.setattr(account_login, "load_persisted_profile_keys", lambda: {})
    assert account_login.fetch_trial_status(AppConfig()) is None
    assert endpoint["requests"] == []


def test_bounded_response_and_unknown_contract(endpoint):
    endpoint["body"]["schema_version"] = 2
    assert account_login.fetch_trial_status(AppConfig()) is None
    endpoint["body"] = {"padding": "x" * 70000}
    assert account_login.fetch_trial_status(AppConfig()) is None


def test_unlimited_and_pre_activation_balances(endpoint):
    endpoint["body"]["billing"].update(
        unlimited_credits=True,
        free_allowance_microusd=None,
        cap_5h_microusd=None,
        cap_7d_microusd=None,
    )
    status = account_login.fetch_trial_status(AppConfig())
    assert "unlimited credits" in account_login.format_trial_status_line(status)
    endpoint["body"]["billing"].update(
        unlimited_credits=False,
        free_allowance_microusd=500000,
        free_granted_period=False,
        balance_microusd=0,
    )
    status = account_login.fetch_trial_status(AppConfig())
    assert "50.0 credits left" in account_login.format_trial_status_line(status)
