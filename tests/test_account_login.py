from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from alysis_code import account_login, alysis_cloud
from alysis_code.config import (
    load_config,
    load_persisted_profile_keys,
    resolve_api_key,
)
from alysis_code.profile_presets import get_preset, make_profile_from_preset

_GATEWAY_KEY = "slk_test-1111-2222-3333-4444"
_USER_CODE = "ABCD-EFGH"
_DEVICE_CODE = "device-code-secret-hex"


class _StubDeviceFlowServer:
    """Stands in for the `device-code` + `device-token` edge functions.

    Behaviour is driven by ``approve_after``: the Nth poll returns approved
    with the key; earlier polls return pending. ``terminal`` overrides every
    poll with a fixed terminal status (e.g. "denied", "expired").
    """

    def __init__(self, *, approve_after: int = 1, terminal: str | None = None) -> None:
        self.polls = 0
        self.code_requests: list[dict] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # noqa: A002
                return

            def _reply(self, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path.endswith("/device-code"):
                    outer.code_requests.append(body)
                    self._reply(
                        {
                            "device_code": _DEVICE_CODE,
                            "user_code": _USER_CODE,
                            "verification_url": "https://example.test/activate",
                            "verification_url_complete": (
                                f"https://example.test/activate?code={_USER_CODE}"
                            ),
                            # interval 0 keeps the tests fast.
                            "interval": 0,
                            "expires_in": 900,
                        }
                    )
                    return
                if self.path.endswith("/device-token"):
                    assert body.get("device_code") == _DEVICE_CODE
                    outer.polls += 1
                    if terminal is not None:
                        self._reply({"status": terminal})
                        return
                    if outer.polls >= approve_after:
                        self._reply({"status": "approved", "key": _GATEWAY_KEY})
                    else:
                        self._reply({"status": "pending"})
                    return
                self.send_error(404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", "https://cloud.example.invalid/api")
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _noop_browser(opened: list[str]):
    def _open(url: str) -> bool:
        opened.append(url)
        return True

    return _open


def test_alysis_preset_offers_current_free_models_without_paid_defaults() -> None:
    preset = get_preset("alysis")
    assert preset is not None
    assert preset.api_key_env is None
    assert preset.suggested_models[0] == "glm-5.3-flash"
    # The reviewed paid catalog grows without making its routes Free/offline defaults.
    free_models = (
        "glm-5.3-flash",
        "gpt-6-luna",
        "claude-haiku-5-5",
    )
    assert preset.suggested_models[:3] == free_models
    assert set(preset.suggested_models) == set(free_models) | set(preset.paid_models)
    assert {"gpt-6.1-terra", "mistral-large-4", "reflection-beam"} <= set(preset.paid_models)
    from alysis_code.profile_presets import model_options_for_preset

    assert [row[0] for row in model_options_for_preset(preset)] == [
        "glm-5.3-flash",
        "gpt-6-luna",
        "claude-haiku-5-5",
    ]
    assert set(preset.suggested_model_descriptions) == set(preset.suggested_models)
    profile = make_profile_from_preset(preset, name="alysis")
    assert profile.default_model == "glm-5.3-flash"
    # The old Xiaomi campaign no longer supplies hosted model aliases.
    from alysis_code.profile_presets import canonical_model_alias_for_preset

    for model in ("mimo", "mimo-v2.5-pro", "mimo-v2-flash", "mimo-v2.5"):
        assert canonical_model_alias_for_preset(preset, model) == model


@pytest.mark.parametrize(
    "legacy_model",
    [
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "deepseek-v4.1-flash-expires-on-0910",
    ],
)
def test_login_migrates_retired_hosted_models(tmp_path: Path, monkeypatch, legacy_model) -> None:
    from alysis_code.profiles import ProfileSpec, add_profile

    _config_env(tmp_path, monkeypatch)
    cfg = load_config()
    add_profile(
        cfg,
        ProfileSpec(
            name="alysis", base_url=alysis_cloud.gateway_base_url(), default_model=legacy_model
        ),
    )
    result = account_login._activate_alysis_profile(cfg, email="me@example.com")
    assert result.model == "glm-5.3-flash"
    assert load_config().model == "glm-5.3-flash"


def test_login_status_defaults_to_logged_out(tmp_path: Path, monkeypatch) -> None:
    _config_env(tmp_path, monkeypatch)
    cfg = load_config()
    status = account_login.login_status(cfg)
    assert status.logged_in is False
    assert status.active is False


def test_logout_when_not_logged_in_returns_false(tmp_path: Path, monkeypatch) -> None:
    _config_env(tmp_path, monkeypatch)
    cfg = load_config()
    assert account_login.logout(cfg) is False


def test_approval_url_pins_host_to_configured_site(monkeypatch) -> None:
    """The server never gets to choose which website login opens.

    A remote response with a different origin cannot redirect sign-in away
    from the configured product site.
    """
    monkeypatch.delenv("ALYSIS_SITE_URL", raising=False)
    site = alysis_cloud.site_url()

    supplied = f"https://example.com/activate?code={_USER_CODE}"
    assert (
        account_login._approval_url(supplied, user_code=_USER_CODE)
        == f"{site}/activate?code={_USER_CODE}"
    )

    # Any foreign origin is rewritten; path and query survive.
    assert (
        account_login._approval_url("https://evil.example/activate?code=XX", user_code=_USER_CODE)
        == f"{site}/activate?code=XX"
    )

    # Missing or unusable server values fall back to the local activate URL.
    for empty in ("", "   ".strip(), "://nonsense"):
        assert _USER_CODE in account_login._approval_url(empty, user_code=_USER_CODE)
        assert account_login._approval_url(empty, user_code=_USER_CODE).startswith(site)


def test_approval_url_follows_site_override(monkeypatch) -> None:
    """Staging/local deployments still steer the browser via ALYSIS_SITE_URL."""
    monkeypatch.setenv("ALYSIS_SITE_URL", "https://staging.example.test")
    assert (
        account_login._approval_url(
            f"https://alysiscode.com/activate?code={_USER_CODE}", user_code=_USER_CODE
        )
        == f"https://staging.example.test/activate?code={_USER_CODE}"
    )


def test_login_full_flow_wires_gateway_key_as_bearer(tmp_path: Path, monkeypatch) -> None:
    _config_env(tmp_path, monkeypatch)
    stub = _StubDeviceFlowServer(approve_after=3)  # a couple of pending polls first
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", f"{stub.base_url}/functions/v1")
    opened: list[str] = []
    try:
        cfg = load_config()
        result = account_login.login(cfg, browser_opener=_noop_browser(opened), timeout_s=30)

        # The browser was pointed at the approval page with the code prefilled,
        # on OUR site — not the "https://example.test" host the stub returned.
        assert opened and _USER_CODE in opened[0]
        assert opened[0].startswith(alysis_cloud.site_url())
        assert "example.test" not in opened[0]
        # The CLI kept polling until approval.
        assert stub.polls == 3

        # Result reflects an active alysis profile with the fresh-login default model.
        assert result.profile_name == "alysis"
        assert result.model == "gpt-6-luna"
        assert result.base_url.rstrip("/").endswith("/v1")

        # The gateway key is persisted as the alysis profile key.
        assert load_persisted_profile_keys()["alysis"] == _GATEWAY_KEY

        # Reloaded config has alysis active with the default model.
        reloaded = load_config()
        assert reloaded.extra_fields["active_profile"] == "alysis"
        assert reloaded.model == "gpt-6-luna"

        # The crucial wiring: resolve_api_key returns the gateway key as the
        # Bearer for the alysis profile (so requests hit the gateway authed).
        resolution = resolve_api_key(reloaded, profile_name="alysis")
        assert resolution.key == _GATEWAY_KEY

        status = account_login.login_status(reloaded)
        assert status.logged_in is True
        assert status.active is True
        assert status.key_preview is not None and status.key_preview.startswith("slk_")
    finally:
        stub.close()


def test_login_denied_persists_nothing(tmp_path: Path, monkeypatch) -> None:
    _config_env(tmp_path, monkeypatch)
    stub = _StubDeviceFlowServer(terminal="denied")
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", f"{stub.base_url}/functions/v1")
    try:
        with pytest.raises(account_login.AlysisLoginError, match="rejected"):
            account_login.login(load_config(), browser_opener=_noop_browser([]), timeout_s=10)
        assert "alysis" not in load_persisted_profile_keys()
    finally:
        stub.close()


def test_login_expired_code_raises(tmp_path: Path, monkeypatch) -> None:
    _config_env(tmp_path, monkeypatch)
    stub = _StubDeviceFlowServer(terminal="expired")
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", f"{stub.base_url}/functions/v1")
    try:
        with pytest.raises(account_login.AlysisLoginError, match="expired"):
            account_login.login(load_config(), browser_opener=_noop_browser([]), timeout_s=10)
        assert "alysis" not in load_persisted_profile_keys()
    finally:
        stub.close()


def test_login_then_logout_clears_key(tmp_path: Path, monkeypatch) -> None:
    _config_env(tmp_path, monkeypatch)
    stub = _StubDeviceFlowServer()
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", f"{stub.base_url}/functions/v1")
    try:
        cfg = load_config()
        account_login.login(cfg, browser_opener=_noop_browser([]), timeout_s=10)
        assert load_persisted_profile_keys().get("alysis") == _GATEWAY_KEY

        reloaded = load_config()
        assert account_login.logout(reloaded) is True
        assert "alysis" not in load_persisted_profile_keys()
    finally:
        stub.close()


def test_login_preserves_user_chosen_model_across_relogin(tmp_path: Path, monkeypatch) -> None:
    from dataclasses import replace

    from alysis_code.config import save_config
    from alysis_code.profiles import add_profile, get_profile

    _config_env(tmp_path, monkeypatch)
    stub = _StubDeviceFlowServer()
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", f"{stub.base_url}/functions/v1")
    try:
        # A fresh connect defaults to GPT-6 Luna (free credits).
        first = account_login.login(load_config(), browser_opener=_noop_browser([]), timeout_s=10)
        assert first.model == "gpt-6-luna"

        # Simulate the user picking another model in `/config`.
        cfg = load_config()
        add_profile(cfg, replace(get_profile(cfg, "alysis"), default_model="deepseek-v4-pro"))
        save_config(cfg)

        # Re-login must keep that choice instead of clobbering it back.
        second = account_login.login(load_config(), browser_opener=_noop_browser([]), timeout_s=10)
        assert second.model == "deepseek-v4-pro"
        assert load_config().model == "deepseek-v4-pro"
    finally:
        stub.close()


@pytest.mark.parametrize("existing_hosted_profile", [False, True])
def test_login_activates_native_execution_after_delegated_runtime(
    tmp_path: Path, monkeypatch, existing_hosted_profile: bool
) -> None:
    from dataclasses import replace

    from alysis_code.config import AgentRuntimeSettings, ExecutionConfig, save_config
    from alysis_code.profiles import add_profile, get_profile, set_active_profile

    _config_env(tmp_path, monkeypatch)
    stub = _StubDeviceFlowServer()
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", f"{stub.base_url}/functions/v1")
    gateway_url = f"{stub.base_url}/hosted/v1"
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", gateway_url)
    try:
        cfg = load_config()
        byok = replace(
            make_profile_from_preset(get_preset("openai"), name="my-api-provider"),
            default_model="gpt-4.1",
            reasoning_effort="low",
        )
        add_profile(cfg, byok)
        set_active_profile(cfg, byok.name)
        if existing_hosted_profile:
            add_profile(
                cfg,
                replace(
                    make_profile_from_preset(get_preset("alysis"), name="alysis"),
                    default_model="deepseek-v4-pro",
                    reasoning_effort="high",
                ),
            )
        cfg.execution = ExecutionConfig(backend="delegated", runtime="openai-codex")
        runtime = AgentRuntimeSettings(
            adapter="codex-cli",
            executable="codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            timeout_seconds=90,
        )
        cfg.agent_runtimes["openai-codex"] = runtime
        save_config(cfg)

        result = account_login.login(load_config(), browser_opener=_noop_browser([]), timeout_s=10)

        reloaded = load_config()
        assert reloaded.execution.backend == "native"
        assert reloaded.execution.runtime is None
        assert reloaded.extra_fields["active_profile"] == "alysis"
        assert reloaded.base_url == gateway_url
        assert reloaded.model == ("deepseek-v4-pro" if existing_hosted_profile else "gpt-6-luna")
        assert result.model == reloaded.model
        assert result.base_url == gateway_url
        assert get_profile(reloaded, byok.name) == byok
        assert reloaded.agent_runtimes["openai-codex"] == runtime
        assert resolve_api_key(reloaded, profile_name="alysis").key == _GATEWAY_KEY
        if existing_hosted_profile:
            assert get_profile(reloaded, "alysis").reasoning_effort == "high"
            assert reloaded.llm_reasoning_effort == "high"
    finally:
        stub.close()


@pytest.mark.parametrize("legacy_model", ["mimo", "mimo-v2.5-pro", "mimo-v2-flash", "mimo-v2.5"])
def test_login_does_not_redirect_old_xiaomi_campaign_model(
    tmp_path: Path, monkeypatch, legacy_model: str
) -> None:
    # An unsupported campaign model must not silently select another provider's model.
    from dataclasses import replace

    from alysis_code.config import save_config
    from alysis_code.profiles import add_profile, get_profile

    _config_env(tmp_path, monkeypatch)
    stub = _StubDeviceFlowServer()
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", f"{stub.base_url}/functions/v1")
    try:
        account_login.login(load_config(), browser_opener=_noop_browser([]), timeout_s=10)
        cfg = load_config()
        add_profile(cfg, replace(get_profile(cfg, "alysis"), default_model=legacy_model))
        save_config(cfg)

        again = account_login.login(load_config(), browser_opener=_noop_browser([]), timeout_s=10)
        assert again.model == legacy_model
        reloaded = load_config()
        assert reloaded.model == legacy_model
        assert reloaded.extra_fields["active_profile"] == "alysis"
        assert reloaded.base_url == alysis_cloud.gateway_base_url()
    finally:
        stub.close()


def test_fetch_trial_status_returns_none(tmp_path: Path, monkeypatch) -> None:
    # No CLI status endpoint yet: plan/credits live on the account page. The
    # None path is the documented contract callers degrade on.
    _config_env(tmp_path, monkeypatch)
    assert account_login.fetch_trial_status(load_config()) is None


def test_format_trial_status_line_expired() -> None:
    status = account_login.TrialStatus(
        plan="trial",
        email=None,
        trial_ends_at="2000-01-01T00:00:00+00:00",
        tokens_total=1000,
        tokens_used=1000,
        tokens_remaining=0,
    )
    line = account_login.format_trial_status_line(status)
    assert line is not None
    assert "expired" in line
    assert "1,000 / 1,000 tokens used" in line


def test_format_trial_status_line_empty_returns_none() -> None:
    status = account_login.TrialStatus(None, None, None, None, None, None)
    assert account_login.format_trial_status_line(status) is None


class _StubModelsServer:
    """Stands in for the gateway's GET /v1/models discovery route."""

    def __init__(self, model_ids: list[str], *, status_code: int = 200) -> None:
        body = json.dumps(
            {"object": "list", "data": [{"id": mid, "object": "model"} for mid in model_ids]}
        ).encode()

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                if not self.path.endswith("/models"):
                    self.send_error(404)
                    return
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def test_list_trial_models_parses_gateway_allowlist(tmp_path: Path, monkeypatch) -> None:
    _config_env(tmp_path, monkeypatch)
    stub = _StubModelsServer(["deepseek-flash", "deepseek-v4-pro"])
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", f"{stub.base_url}/v1")
    try:
        models = account_login.list_trial_models(load_config())
        assert models == ["deepseek-flash", "deepseek-v4-pro"]
    finally:
        stub.close()


def test_list_trial_models_empty_when_unreachable(tmp_path: Path, monkeypatch) -> None:
    _config_env(tmp_path, monkeypatch)
    stub = _StubModelsServer(["deepseek-flash"])
    base_url = stub.base_url
    stub.close()  # nothing is listening now -> connection refused
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", f"{base_url}/v1")
    assert account_login.list_trial_models(load_config()) == []
