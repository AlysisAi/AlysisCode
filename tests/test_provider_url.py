"""How a provider base URL is shown in pickers and summaries.

Two things must never reach the screen: credentials embedded in a base URL as
userinfo, and the hosted gateway's Supabase project host.
"""

from __future__ import annotations

import pytest

from alysis_code.provider_url import (
    ALYSIS_GATEWAY_LABEL,
    display_endpoint,
    display_host,
    is_alysis_gateway_url,
    is_loopback_endpoint,
    known_provider_key_from_base_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "https://MODEL.localhost.:9137/v1",
        "http://127.0.0.1/v1",
        "http://127.42.9.7/v1",
        "http://127.255.255.254/v1",
        "http://[::1]:8123/v1",
        "http://[0:0:0:0:0:0:0:1]/v1",
        "http://[::ffff:127.42.0.9]/v1",
    ],
)
def test_loopback_endpoint_uses_address_identity(url: str) -> None:
    assert is_loopback_endpoint(url)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "localhost:11434/v1",
        "file://localhost/v1",
        "ftp://127.0.0.1/v1",
        "http://localhost:99999/v1",
        "http://[::1/v1",
        "http://0.0.0.0/v1",
        "http://[::]/v1",
        "http://192.168.1.2/v1",
        "http://169.254.169.254/v1",
        "http://[::ffff:192.168.1.2]/v1",
        "https://localhost.example.test/v1",
        "https://127.0.0.1.example.test/v1",
        "https://localhost@models.example.test/v1",
        "https://models.example.test/v1",
    ],
)
def test_remote_or_invalid_endpoint_is_not_loopback(url: str | None) -> None:
    assert not is_loopback_endpoint(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://chatgpt.com/backend-api/codex", "openai"),
        ("https://chatgpt.com/backend-api/codex/", "openai"),
        ("https://chatgpt.com/backend-api/codex/responses", "openai"),
        ("https://chatgpt.com/backend-api/other", None),
        ("https://chatgpt.com/backend-api/codex-other", None),
        ("https://chatgpt.com.example.org/backend-api/codex", None),
    ],
)
def test_chatgpt_subscription_endpoint_has_openai_provider_identity(url, expected):
    assert known_provider_key_from_base_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.openai.com/v1", "api.openai.com"),
        ("http://localhost:11434/v1", "localhost:11434"),
        ("https://team:s3cret@proxy.example.com:8443/v1", "proxy.example.com:8443"),
        ("https://sk-live-token@proxy.example.com/v1", "proxy.example.com"),
        ("http://[::1]:8000/v1", "[::1]:8000"),
        ("https://proxy.example.com:99999/v1", "proxy.example.com"),
        ("http://[::1/v1", ""),
        ("api.openai.com/v1", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_display_host_is_the_host_and_port_only(url: str | None, expected: str) -> None:
    assert display_host(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alysiscode.com/v1",
    ],
)
def test_hosted_gateway_is_named_instead_of_shown_by_host(url: str) -> None:
    assert is_alysis_gateway_url(url) is True
    assert display_endpoint(url) == ALYSIS_GATEWAY_LABEL
    # Naming it for display does not change what it is: it forwards to DeepSeek.
    assert known_provider_key_from_base_url(url) == "deepseek"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Another Supabase function, or the llm path on another host, is not the gateway.
        ("https://other.example.invalid/rest/v1", "other.example.invalid"),
        ("https://example.com/functions/v1/llm/v1", "example.com"),
        ("https://api.z.ai/api/coding/paas/v4", "api.z.ai"),
        ("https://team:s3cret@proxy.example.com/v1", "proxy.example.com"),
    ],
)
def test_other_endpoints_display_their_host(url: str, expected: str) -> None:
    assert is_alysis_gateway_url(url) is False
    assert display_endpoint(url) == expected


def test_configured_gateway_uses_product_label(monkeypatch):
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", "https://cloud.example.invalid/api")
    url = "https://cloud.example.invalid/api/llm/v1"
    assert display_endpoint(url) == ALYSIS_GATEWAY_LABEL
    assert known_provider_key_from_base_url(url) == "deepseek"


def test_legacy_gateway_shape_still_gets_product_label():
    # Synthetic domain parts exercise legacy classification without a deployment URL.
    host = ".".join(("synthetic", "supabase", "co"))
    assert display_endpoint(f"https://{host}/functions/v1/llm/v1") == ALYSIS_GATEWAY_LABEL


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen"),
        ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "qwen"),
        ("https://dashscope-us.aliyuncs.com/compatible-mode/v1", "qwen"),
        ("https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1", "qwen"),
        ("https://coding-intl.dashscope.aliyuncs.com/v1", "qwen"),
        ("https://DASHSCOPE.ALIYUNCS.COM./compatible-mode/v1", "qwen"),
        ("https://notdashscope.aliyuncs.com/v1", None),
        ("https://dashscope.aliyuncs.com.example.net/v1", None),
        ("https://dashscope.attacker.example/v1", None),
        ("https://example.net/dashscope/v1", None),
        # Current production endpoints: workspace, trial, and token-plan Model Studio.
        ("https://my-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "qwen"),
        ("https://trial.us-east-1.maas.aliyuncs.com/compatible-mode/v1", "qwen"),
        ("https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1", "qwen"),
        ("https://maas.aliyuncs.com/v1", None),
        ("https://evilmaas.aliyuncs.com/v1", None),
        ("https://my-workspace.cn-beijing.maas.aliyuncs.com.attacker.example/v1", None),
    ],
)
def test_dashscope_provider_detection_requires_owned_host(url: str, expected: str | None) -> None:
    assert known_provider_key_from_base_url(url) == expected
