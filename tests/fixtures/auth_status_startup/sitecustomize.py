"""Offline child fixture: real CLI/status/store validation, scripted vault boundary."""

# ruff: noqa: E402 -- install the offline audit boundary before production imports.
import atexit
import copy
import json
import os
import signal
import sys
import time
from pathlib import Path


def deny_external(event, args):
    if event in {
        "socket.connect",
        "socket.getaddrinfo",
        "socket.bind",
        "subprocess.Popen",
        "os.system",
        "os.posix_spawn",
    }:
        events.append(["denied_external", event])
        raise OSError("offline auth-status fixture denies network and subprocesses")


sys.addaudithook(deny_external)
signal.alarm(45)
events = []
case = os.environ["W5_AUTH_CASE"]
audit_path = Path(os.environ["W5_AUTH_AUDIT"])


def receipt():
    audit_path.write_text(
        json.dumps(
            {
                "events": events,
                "modules": sorted(name for name in sys.modules if name.startswith("alysis_code")),
                "tls": {
                    key: os.environ.get(key)
                    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
                },
                "stdio_errors": [sys.stdout.errors, sys.stderr.errors],
            }
        )
    )


atexit.register(receipt)

import httpx

from alysis_code import config
from alysis_code.mcp import token_store
from alysis_code.provider_auth import openai_codex, registry, store

original_config = config.load_config


def config_read():
    events.append(["config_read"])
    return original_config()


config.load_config = config_read
original_keyring = token_store.keyring_availability


def keyring_read(*, probe_read=True):
    events.append(["keyring", probe_read])
    return original_keyring(probe_read=probe_read)


token_store.keyring_availability = keyring_read
record = {
    "access_token": "synthetic-access",
    "refresh_token": "synthetic-refresh",
    "expires_at": 4000000000.0,
    "account_id": "synthetic-account",
    "account_label": "fixture@example.test",
    "client_id": "synthetic-client",
    "subject": "synthetic-subject",
    "credential_generation": "cg_" + "1" * 32,
    "scopes": ["chatgpt.tokens.use.direct"],
}
if case in {"refresh_failure", "revoked", "timeout", "interrupt", "removed_during_refresh"}:
    record["expires_at"] = 1.0
reads = 0


def vault_read(path):
    global reads
    reads += 1
    events.append(["vault_read", path.name])
    if case == "block":
        audit_path.with_suffix(".ready").write_text("in vault read")
        while True:
            time.sleep(0.05)
    if case == "unreadable":
        raise ValueError("synthetic corrupt encrypted vault")
    if case == "missing" or (
        case in {"changed_between_calls", "removed_during_refresh"} and reads > 1
    ):
        payload = {}
    elif case == "malformed":
        payload = {"openai-codex": {"expires_at": "invalid"}}
    else:
        payload = {"openai-codex": copy.deepcopy(record)}
        if case == "revoked_between_calls" and reads > 1:
            payload["openai-codex"]["expires_at"] = 1.0
    return token_store.TokenPayloadLoadResult(payload=payload)


store.load_token_payload_result = vault_read


def unexpected_write(*args, **kwargs):
    raise AssertionError("these scenarios must not change the credential store")


store.save_token_payload = unexpected_write
store.migrate_legacy_token_payload = unexpected_write


def refresh(request):
    events.append(["refresh", request.method, request.url.path])
    assert request.url.path == "/api/accounts/oauth/token"
    if case == "timeout":
        raise httpx.ReadTimeout("scripted refresh timeout", request=request)
    if case == "interrupt":
        raise KeyboardInterrupt
    if case in {"revoked", "revoked_between_calls"}:
        return httpx.Response(400, json={"error": "invalid_grant"})
    assert case == "refresh_failure"
    return httpx.Response(503, json={"error": "temporarily_unavailable"})


registry.register_provider_auth(
    "openai-codex",
    lambda **kwargs: openai_codex.OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(refresh)
    ),
)

# Force only the dispatch choice for the full-CLI oracle. Everything from argv
# parsing through the production status/store path and process exit stays real.
if os.environ.get("W5_AUTH_FULL") == "1":
    from alysis_code.cli_impl import auth_status_entry

    auth_status_entry.is_auth_status_probe = lambda args: False
