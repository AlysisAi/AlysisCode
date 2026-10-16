from __future__ import annotations

import pytest

from alysis_code import account_login
from alysis_code import alysis_cloud as cloud
from alysis_code.config import AppConfig
from alysis_code.feedback_delivery import prepare_feedback
from alysis_code.feedback_report import FeedbackReportError


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


def test_public_build_has_no_deployment_defaults_and_login_sends_nothing(monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail("Unconfigured hosted login must not make a network request")

    monkeypatch.setattr(account_login.urllib.request, "urlopen", unexpected_request)
    assert cloud.cloud_api_url() == ""
    assert cloud.gateway_base_url() == ""
    assert cloud.public_client_key() == ""
    assert account_login._function_headers() == {"Content-Type": "application/json"}
    with pytest.raises(account_login.AlysisLoginError, match="ALYSIS_CLOUD_API_URL"):
        account_login.login(AppConfig())


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
    with pytest.raises(cloud.AlysisCloudConfigError, match="not configured"):
        cloud.device_code_url()


def test_optional_public_routing_key_is_read_at_request_time(monkeypatch):
    monkeypatch.setenv("ALYSIS_CLOUD_PUBLIC_KEY", "synthetic-public-routing-key")
    assert account_login._function_headers() == {
        "Content-Type": "application/json",
        "apikey": "synthetic-public-routing-key",
        "Authorization": "Bearer synthetic-public-routing-key",
    }
    monkeypatch.delenv("ALYSIS_CLOUD_PUBLIC_KEY")
    assert "Authorization" not in account_login._function_headers()


def test_missing_feedback_configuration_returns_actionable_error(tmp_path):
    with pytest.raises(FeedbackReportError, match="ALYSIS_CLOUD_API_URL"):
        prepare_feedback(
            workspace_root=tmp_path, feedback_text="Synthetic report", include_diagnostics=False
        )


def test_remote_cleartext_api_is_rejected(monkeypatch):
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", "http://cloud.example.invalid/api")
    with pytest.raises(cloud.AlysisCloudConfigError, match="https is required"):
        cloud.device_code_url()
