"""SIWC contract and signed identity checks, using only disposable local fixtures."""

import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from alysis_code.mcp.token_store import KEY_SOURCE_FILESYSTEM, OAUTH_KEY_SOURCE_ENV
from alysis_code.provider_auth import openai_codex as auth
from alysis_code.provider_auth.base import ProviderAuthError
from alysis_code.provider_auth.store import (
    ProviderTokenRecord,
    delete_provider_token,
    load_provider_token,
    save_provider_token,
    subscription_registration,
)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv(OAUTH_KEY_SOURCE_ENV, KEY_SOURCE_FILESYSTEM)


def record(*, subject="fixture-a", scopes=("chatgpt.tokens.use.direct", "resource.invoke")):
    return ProviderTokenRecord(
        access_token="fixture-access",
        refresh_token="fixture-refresh",
        expires_at=time.time() + 3600,
        account_id=subject,
        subject=subject,
        client_id="oaiapp_" + subject,
        id_token="fixture-id",
        scopes=scopes,
    )


def capture_login(monkeypatch, *, new_account=False):
    class Captured(Exception):
        pass

    class Server:
        server_address = ("127.0.0.1", 15455)

        def server_close(self):
            pass

    captured = []

    def capture(url):
        captured.append(url)
        raise Captured()

    monkeypatch.setattr(auth, "_bind_callback_server", Server)
    monkeypatch.setattr(auth, "open_url", capture)
    with pytest.raises(Captured):
        auth.OpenAICodexSubscriptionAuth()._browser_login(lambda _: None, new_account=new_account)
    return urlsplit(captured[0]), parse_qs(urlsplit(captured[0]).query)


def test_browser_login_uses_app_registration_and_plan_permission(monkeypatch):
    parsed, params = capture_login(monkeypatch)
    assert parsed.path == "/api/accounts/authorize"
    assert params["client_id"] == ["dynamic_agent_client"]
    assert params["agent_name_hint"] == ["Alysis Code"]
    assert params["redirect_uri"] == ["http://127.0.0.1:15455/auth/callback"]
    assert params["resource"] == ["https://api.openai.com/v1"]
    assert "chatgpt.tokens.use.direct" in params["scope"][0].split()
    assert params["ext_agent_host_id"][0].startswith("urn:uuid:")
    assert params["nonce"][0]
    assert params["code_challenge_method"] == ["S256"]
    assert len(params["code_challenge"][0]) == 43
    _, second = capture_login(monkeypatch)
    assert params["ext_agent_host_id"] == second["ext_agent_host_id"]
    assert params["state"] != second["state"]
    assert params["nonce"] != second["nonce"]


def test_reconnect_reuses_registration_but_switch_registers_new_account(monkeypatch):
    save_provider_token("openai-codex", record())
    _, params = capture_login(monkeypatch)
    assert params["client_id"] == ["oaiapp_fixture-a"]
    assert params["id_token_hint"] == ["fixture-id"]
    assert "agent_name_hint" not in params
    _, switched = capture_login(monkeypatch, new_account=True)
    assert switched["client_id"] == ["dynamic_agent_client"]
    assert "id_token_hint" not in switched
    assert switched["ext_agent_host_id"] == params["ext_agent_host_id"]


def test_sign_out_clears_tokens_and_retains_registration():
    old = record()
    save_provider_token("openai-codex", old)
    host = subscription_registration("openai-codex")["host_id"]
    delete_provider_token("openai-codex")
    assert load_provider_token("openai-codex") is None
    saved = subscription_registration("openai-codex")
    assert saved["client_id"] == old.client_id
    assert saved["subject"] == old.subject
    assert saved["host_id"] == host
    assert not {"access_token", "refresh_token", "id_token"} & saved.keys()


def test_failed_or_ungranted_switch_preserves_selected_account(monkeypatch):
    old = record()
    save_provider_token("openai-codex", old)
    adapter = auth.OpenAICodexSubscriptionAuth()

    def fail(*args, **kwargs):
        raise ProviderAuthError("declined")

    monkeypatch.setattr(adapter, "_browser_login", fail)
    with pytest.raises(ProviderAuthError, match="declined"):
        adapter.login("browser-new-account")
    assert load_provider_token("openai-codex") == old
    monkeypatch.setattr(
        adapter, "_browser_login", lambda *args, **kwargs: record(subject="b", scopes=())
    )
    with pytest.raises(ProviderAuthError, match="previous account"):
        adapter.login("browser-new-account")
    assert load_provider_token("openai-codex") == old
    assert subscription_registration("openai-codex")["client_id"] == old.client_id


def test_identity_only_login_is_disabled_and_can_request_consent(monkeypatch):
    adapter = auth.OpenAICodexSubscriptionAuth()
    monkeypatch.setattr(adapter, "_browser_login", lambda *args, **kwargs: record(scopes=()))
    status = adapter.login()
    assert not status.connected and status.verified
    assert "plan usage is not enabled" in status.detail
    _, params = capture_login(monkeypatch)
    assert params["prompt"] == ["consent"]


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@pytest.fixture
def signed_identity():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()
    key = {
        "kid": "fixture-key",
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "n": b64(numbers.n.to_bytes(256, "big")),
        "e": b64(numbers.e.to_bytes(3, "big")),
    }

    def sign(**patch):
        claims = {
            "iss": auth._ISSUER,
            "aud": "oaiapp_fixture",
            "sub": "fixture-subject",
            "nonce": "fixture-nonce",
            "exp": time.time() + 3600,
            "iat": time.time(),
            **patch,
        }
        head = b64(json.dumps({"alg": "RS256", "kid": "fixture-key"}).encode())
        body = b64(json.dumps(claims).encode())
        signature = private.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{body}.{b64(signature)}"

    return sign, key


def exchange_adapter(token, key, requests):
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200, json={"issuer": auth._ISSUER, "jwks_uri": auth._ISSUER + "/fixture-jwks"}
            )
        if request.url.path == "/fixture-jwks":
            return httpx.Response(200, json={"keys": [key]})
        assert str(request.url) == auth._TOKEN_URL
        return httpx.Response(
            200,
            json={
                "access_token": "fixture-access",
                "refresh_token": "fixture-refresh",
                "id_token": token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid resource.invoke chatgpt.tokens.use.direct",
            },
        )

    return auth.OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler))


def exchange(adapter, **kwargs):
    return adapter._exchange_code(
        code="fixture-code",
        verifier="fixture-verifier",
        redirect_uri="http://127.0.0.1:15455/auth/callback",
        client_id="oaiapp_fixture",
        nonce="fixture-nonce",
        **kwargs,
    )


def test_code_exchange_verifies_signature_before_activating(signed_identity):
    sign, key = signed_identity
    requests = []
    result = exchange(exchange_adapter(sign(), key, requests))
    assert result.subject == "fixture-subject"
    assert result.client_id == "oaiapp_fixture"
    form = parse_qs(requests[0].content.decode())
    assert form["client_id"] == ["oaiapp_fixture"]
    assert form["resource"] == [auth._RESOURCE]
    assert form["redirect_uri"] == ["http://127.0.0.1:15455/auth/callback"]
    assert form["code_verifier"] == ["fixture-verifier"]
    assert "client_secret" not in form
    assert load_provider_token("openai-codex") is None  # activation belongs to login()


@pytest.mark.parametrize(
    "patch",
    [
        {"nonce": "wrong"},
        {"iss": "https://evil.test"},
        {"aud": "another-client"},
        {"exp": 1},
        {"sub": ""},
        {"nbf": "future"},
        {"exp": float("nan")},
        {"aud": ["oaiapp_fixture", "other"], "azp": "other"},
    ],
)
def test_wrong_identity_claims_are_rejected(signed_identity, patch):
    sign, key = signed_identity
    # Full-suite execution can begin hours after collection. Construct this
    # future claim when the test runs, so it cannot become valid while queued.
    if patch.get("nbf") == "future":
        patch = {**patch, "nbf": time.time() + 3600}
    with pytest.raises(ProviderAuthError, match="identity could not be verified"):
        exchange(exchange_adapter(sign(**patch), key, []))


def test_tampered_signature_and_different_reconnect_account_are_rejected(signed_identity):
    sign, key = signed_identity
    token = sign()
    parts = token.split(".")
    parts[-1] = b64(b"wrong" * 51)
    with pytest.raises(ProviderAuthError, match="identity could not be verified"):
        exchange(exchange_adapter(".".join(parts), key, []))
    with pytest.raises(ProviderAuthError, match="Switch account"):
        exchange(exchange_adapter(token, key, []), expected_subject="old-subject")


def test_subscription_transport_uses_public_responses():
    assert auth.OpenAICodexSubscriptionAuth.base_url == "https://api.openai.com/v1"


def test_callback_rejects_wrong_state_before_issued_registration_exchange(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlencode

    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    statuses = []
    threads = []

    def browser(url):
        params = parse_qs(urlsplit(url).query)

        def callback():
            with httpx.Client(trust_env=False) as client:
                base = params["redirect_uri"][0]
                statuses.append(
                    client.get(
                        base
                        + "?"
                        + urlencode({"state": "wrong", "code": "bad", "client_id": "evil"})
                    ).status_code
                )
                statuses.append(
                    client.get(
                        base
                        + "?"
                        + urlencode(
                            {
                                "state": params["state"][0],
                                "code": "fixture-code",
                                "client_id": "oaiapp_fixture-a",
                            }
                        )
                    ).status_code
                )

        thread = threading.Thread(target=callback)
        threads.append(thread)
        thread.start()
        return True

    monkeypatch.setattr(auth, "_bind_callback_server", lambda: server)
    monkeypatch.setattr(auth, "open_url", browser)
    adapter = auth.OpenAICodexSubscriptionAuth()
    captured = []

    def exchange_fixture(**kwargs):
        captured.append(kwargs)
        return record()

    monkeypatch.setattr(adapter, "_exchange_code", exchange_fixture)
    try:
        result = adapter._browser_login(lambda _: None)
    finally:
        for thread in threads:
            thread.join(3)
    assert statuses == [400, 200]
    assert result.subject == "fixture-a"
    assert captured[0]["code"] == "fixture-code"
    assert captured[0]["client_id"] == "oaiapp_fixture-a"
    assert captured[0]["nonce"]


def test_catalog_empty_capabilities_override_static_fallback():
    save_provider_token("openai-codex", record())
    adapter = auth.OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "gpt-6-astra",
                            "visibility": "list",
                            "supported_reasoning_levels": [],
                        },
                        {"slug": "hidden", "visibility": "hide"},
                        {"slug": "gpt-6-sol", "visibility": "list"},
                    ]
                },
            )
        )
    )
    models = adapter.list_models()
    assert [model.id for model in models] == [
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    ]
    by_id = {model.id: model for model in models}
    assert not by_id["gpt-6-astra"].reasoning_efforts
    assert by_id["gpt-6-sol"].reasoning_efforts[0].id == "none"


def test_local_tool_history_retains_namespace_for_stateless_replay():
    payload = auth.OpenAICodexSubscriptionAuth().adapt_responses_payload(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "fixture-call",
                    "name": "read_file",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "fixture-call", "output": "fixture"},
            ]
        }
    )
    assert payload["input"][0]["namespace"] == "alysis"
    assert payload["input"][1]["call_id"] == "fixture-call"


def test_removed_plan_scope_cannot_reuse_previous_grant():
    old = record()
    save_provider_token("openai-codex", old)
    adapter = auth.OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "access_token": "next-fixture-access",
                    "refresh_token": "next-fixture-refresh",
                    "expires_in": 3600,
                    "scope": "",
                },
            )
        )
    )
    with pytest.raises(ProviderAuthError, match="permission was removed"):
        adapter._refresh(old, allow_unexpired_fallback=False)
    assert load_provider_token("openai-codex") == old


def test_successful_switch_keeps_credentials_bound_to_each_registration(monkeypatch):
    from alysis_code.mcp.token_store import load_token_payload_result
    from alysis_code.provider_auth.store import provider_token_store_path

    previous = record()
    replacement = record(subject="fixture-b")
    save_provider_token("openai-codex", previous)
    adapter = auth.OpenAICodexSubscriptionAuth()
    monkeypatch.setattr(adapter, "_browser_login", lambda *args, **kwargs: replacement)
    assert adapter.login("browser-new-account").connected
    assert load_provider_token("openai-codex") == replacement
    saved = load_token_payload_result(provider_token_store_path()).payload
    registrations = saved["$subscription_registrations"]["openai-codex"]["registrations"]
    assert ProviderTokenRecord.from_dict(registrations[previous.client_id]) == previous
    assert ProviderTokenRecord.from_dict(registrations[replacement.client_id]) == replacement
    delete_provider_token("openai-codex")
    assert load_provider_token("openai-codex") is None  # sign-out never selects another account
