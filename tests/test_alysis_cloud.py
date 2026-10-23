from __future__ import annotations

import pytest

from alysis_code import account_login
from alysis_code import alysis_cloud as cloud
from alysis_code.config import AppConfig
from alysis_code.feedback_delivery import prepare_feedback


@pytest.fixture(autouse=True)
def isolated_cloud_configuration(monkeypatch):
    for name in (
        "ALYSIS_CLOUD_API_URL",
        "ALYSIS_CLOUD_PUBLIC_KEY",
        "ALYSIS_SUPABASE_URL",
        "ALYSIS_GATEWAY_URL",
        "ALYSIS_ALLOW_INSECURE_URLS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_public_build_defaults_to_hosted_services_without_a_client_key():
    base = cloud.DEFAULT_CLOUD_API_URL
    assert base.startswith("https://")
    assert cloud.cloud_api_url() == base
    assert cloud.device_code_url() == f"{base}/device-code"
    assert cloud.device_token_url() == f"{base}/device-token"
    assert cloud.account_status_url() == f"{base}/account-status"
    assert cloud.feedback_url() == f"{base}/feedback"
    assert cloud.gateway_base_url() == f"{base}/llm/v1"
    assert cloud.models_url() == f"{base}/llm/v1/models"
    assert cloud.public_client_key() == ""
    assert account_login._function_headers() == {"Content-Type": "application/json"}


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_blank_deployment_overrides_keep_the_hosted_default(monkeypatch, value):
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", value)
    monkeypatch.setenv("ALYSIS_SUPABASE_URL", value)
    assert cloud.cloud_api_url() == cloud.DEFAULT_CLOUD_API_URL


def test_default_login_reaches_the_device_flow(monkeypatch, tmp_path):
    from alysis_code.config import load_config, load_persisted_profile_keys

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    calls = []
    opened = []

    def post(url, payload):
        calls.append(url)
        if url == cloud.device_code_url():
            assert payload["installation_id"]
            return {"device_code": "synthetic-device", "user_code": "TEST-CODE", "interval": 0}
        assert url == cloud.device_token_url()
        assert payload == {"device_code": "synthetic-device"}
        return {"status": "approved", "key": "slk_synthetic-onboarding"}

    monkeypatch.setattr(account_login, "_post_json", post)
    result = account_login.login(AppConfig(), browser_opener=lambda url: opened.append(url) or True)
    assert calls == [cloud.device_code_url(), cloud.device_token_url()]
    assert opened == [f"{cloud.activate_url()}?code=TEST-CODE"]
    assert result.base_url == cloud.gateway_base_url()
    assert load_persisted_profile_keys()["alysis"] == "slk_synthetic-onboarding"
    assert account_login.login_status(load_config()).logged_in


def test_configured_api_supplies_all_hosted_endpoints(monkeypatch):
    from alysis_code.profile_presets import get_preset, make_profile_from_preset

    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", "https://cloud.example.invalid/api/")
    base = "https://cloud.example.invalid/api"
    assert cloud.device_code_url() == f"{base}/device-code"
    assert cloud.device_token_url() == f"{base}/device-token"
    assert cloud.account_status_url() == f"{base}/account-status"
    assert cloud.feedback_url() == f"{base}/feedback"
    assert cloud.gateway_base_url() == f"{base}/llm/v1"
    assert cloud.models_url() == f"{base}/llm/v1/models"
    preset = get_preset("alysis")
    assert preset.base_url == ""
    assert make_profile_from_preset(preset).base_url == cloud.gateway_base_url()


def test_explicit_legacy_configuration_remains_supported(monkeypatch):
    monkeypatch.setenv("ALYSIS_SUPABASE_URL", "http://127.0.0.1:12345/")
    assert cloud.device_code_url() == "http://127.0.0.1:12345/functions/v1/device-code"
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", "https://cloud.example.invalid/api")
    assert cloud.device_code_url() == "https://cloud.example.invalid/api/device-code"


def test_gateway_override_is_independent_of_account_api(monkeypatch):
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", "https://gateway.example.invalid/v1")
    assert cloud.models_url() == "https://gateway.example.invalid/v1/models"
    assert cloud.device_code_url() == f"{cloud.DEFAULT_CLOUD_API_URL}/device-code"


def test_optional_public_routing_key_is_read_at_request_time(monkeypatch):
    monkeypatch.setenv("ALYSIS_CLOUD_PUBLIC_KEY", "synthetic-public-routing-key")
    assert account_login._function_headers() == {
        "Content-Type": "application/json",
        "apikey": "synthetic-public-routing-key",
        "Authorization": "Bearer synthetic-public-routing-key",
    }
    monkeypatch.delenv("ALYSIS_CLOUD_PUBLIC_KEY")
    assert "Authorization" not in account_login._function_headers()


def test_feedback_uses_hosted_endpoint_without_configuration(tmp_path):
    report = prepare_feedback(
        workspace_root=tmp_path, feedback_text="Synthetic report", include_diagnostics=False
    )
    assert report.endpoint == cloud.feedback_url()


def test_remote_cleartext_api_is_rejected(monkeypatch):
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", "http://cloud.example.invalid/api")
    with pytest.raises(cloud.AlysisCloudConfigError, match="https is required"):
        cloud.device_code_url()
