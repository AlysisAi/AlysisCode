"""How a provider base URL is shown in pickers and summaries.

Two things must never reach the screen: credentials embedded in a base URL as
userinfo, and the hosted gateway's Supabase project host.
"""

from __future__ import annotations

import pytest

from alysis_code import alysis_cloud
from alysis_code.profile_presets import PROFILE_PRESETS
from alysis_code.provider_url import (
    ALYSIS_GATEWAY_LABEL,
    display_endpoint,
    display_host,
    is_alysis_gateway_url,
    known_provider_key_from_base_url,
)

_ALYSIS_PRESET_URL = next(p for p in PROFILE_PRESETS if p.key == "alysis").base_url


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
        _ALYSIS_PRESET_URL,
        alysis_cloud.DEFAULT_PROXY_BASE_URL,
        "https://staging-ref.supabase.co/functions/v1/llm/v1",
        "https://api.alysiscode.com/v1",
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
        ("https://other.supabase.co/rest/v1", "other.supabase.co"),
        ("https://example.com/functions/v1/llm/v1", "example.com"),
        ("https://api.z.ai/api/coding/paas/v4", "api.z.ai"),
        ("https://team:s3cret@proxy.example.com/v1", "proxy.example.com"),
    ],
)
def test_other_endpoints_display_their_host(url: str, expected: str) -> None:
    assert is_alysis_gateway_url(url) is False
    assert display_endpoint(url) == expected
