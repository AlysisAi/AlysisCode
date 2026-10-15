from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.alysis_cloud import DEFAULT_PROXY_BASE_URL
from alysis_code.config import AppConfig
from alysis_code.llm.openai_responses import (
    WebSearchCitation,
    WebSearchResponse,
    WebSearchSource,
)
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.tools import web_search as web_search_module
from alysis_code.tools.web_search import (
    _OPENROUTER_WEB_MIN_TIMEOUT_S,
    WebSearchError,
    _is_openrouter_base_url,
    resolve_web_search_runtime,
    resolve_web_search_runtime_status,
    web_search,
)
from alysis_code.tools.web_search_ddgs import DdgsSearchError


@pytest.fixture(autouse=True)
def _clear_generic_web_search_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_API_KEY", raising=False)


def _public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def _configured_cfg(
    *,
    mode: str = "auto",
    base_url: str = "https://api.openai.com/v1",
    web_search_base_url: str | None = None,
) -> AppConfig:
    return AppConfig(
        model="main-model",
        base_url=base_url,
        web_search_mode=mode,
        web_search_base_url=web_search_base_url,
    )


def test_web_search_returns_structured_openai_result_and_dedupes_sources() -> None:
    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["init"] = kwargs

        def web_search(
            self,
            *,
            query: str,
            allowed_domains: list[str] | None = None,
            external_web_access: bool | None = None,
            include_source_details: bool = True,
        ) -> WebSearchResponse:
            captured["query"] = query
            captured["allowed_domains"] = allowed_domains
            captured["external_web_access"] = external_web_access
            captured["include_source_details"] = include_source_details
            return WebSearchResponse(
                answer="Read the official docs.",
                citations=[
                    WebSearchCitation(
                        title="Docs",
                        url="https://docs.example.com/start",
                        start_index=0,
                        end_index=12,
                    )
                ],
                sources=[
                    WebSearchSource(url="https://docs.example.com/start", title="Docs"),
                    WebSearchSource(url="https://docs.example.com/start", title="Docs duplicate"),
                    WebSearchSource(
                        url="https://changelog.example.com/release", title="Release Notes"
                    ),
                ],
                queries=["docs example"],
                raw={"id": "resp"},
                response_id="resp_1",
                model="search-model",
            )

    result = web_search(
        query="docs example",
        cfg=_configured_cfg(),
        api_key="main-key",
        allowed_domains=["docs.example.com"],
        max_sources=8,
        external_web_access=False,
        client_factory=_FakeClient,
    )

    assert captured["query"] == "docs example"
    assert captured["allowed_domains"] == ["docs.example.com"]
    assert captured["external_web_access"] is False
    assert captured["include_source_details"] is True
    assert result["query"] == "docs example"
    assert result["answer"] == "Read the official docs."
    assert result["backend"] == "openai_responses"
    assert result["protocol"] == "openai_compat"
    assert result["chat_protocol"] == "openai_compat"
    assert result["search_protocol"] == "openai_responses"
    assert result["backend_adapter"] == "openai_responses"
    assert result["provider_hosted_search"] is True
    assert result["external_search_provider"] is None
    assert result["citation_count"] == 1
    assert result["source_count"] == 2
    assert result["model"] == "search-model"
    assert result["response_id"] == "resp_1"
    assert result["sources_truncated"] is False
    assert result["sources"] == [
        {"url": "https://docs.example.com/start", "title": "Docs"},
        {"url": "https://changelog.example.com/release", "title": "Release Notes"},
    ]
    assert result["citations"] == [
        {
            "title": "Docs",
            "url": "https://docs.example.com/start",
            "start_index": 0,
            "end_index": 12,
        }
    ]


def test_subscription_auth_makes_native_responses_web_search_ready() -> None:
    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def web_search(self, **_kwargs: object) -> WebSearchResponse:
            return WebSearchResponse(
                answer="Subscription search result",
                citations=[],
                sources=[],
                queries=["subscription query"],
                raw={"id": "resp_search"},
                response_id="resp_search",
                model="gpt-5.4",
            )

    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
        auth_provider="openai-codex",
        default_model="gpt-5.4",
        reasoning_effort="high",
        web_search_adapter="openai_responses",
    )
    cfg = AppConfig(
        model=profile.default_model,
        base_url=profile.base_url,
        web_search_mode="native",
        web_search_adapter="openai_responses",
    )
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)

    status = resolve_web_search_runtime_status(cfg=cfg, api_key="")
    result = web_search(
        query="subscription query",
        cfg=cfg,
        api_key="",
        client_factory=_FakeClient,
        session_id="session-subscription",
    )

    assert status.registration_ready is True
    assert status.api_key_available is True
    assert result["answer"] == "Subscription search result"
    assert captured["provider_auth"].provider_id == "openai-codex"
    assert captured["session_id"] == "session-subscription"


def test_web_search_auto_status_prefers_openai_when_conservatively_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    status = resolve_web_search_runtime_status(cfg=_configured_cfg(), api_key="main-key")
    runtime = resolve_web_search_runtime(cfg=_configured_cfg(), api_key="main-key", strict=True)

    assert status.registration_ready is True
    assert status.provider == "openai_responses"
    assert status.availability_label == "available"
    assert runtime is not None
    assert runtime.provider == "openai_responses"


def test_web_search_auto_status_falls_back_to_tavily_when_openai_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    cfg = _configured_cfg(base_url="https://example-proxy.invalid/v1")
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")
    runtime = resolve_web_search_runtime(cfg=cfg, api_key="main-key", strict=True)

    assert status.registration_ready is True
    assert status.provider == "tavily"
    assert status.base_url is None
    assert status.model is None
    assert runtime is not None
    assert runtime.provider == "tavily"


def test_web_search_auto_status_prefers_native_when_external_is_also_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    status = resolve_web_search_runtime_status(cfg=_configured_cfg(), api_key="main-key")
    runtime = resolve_web_search_runtime(cfg=_configured_cfg(), api_key="main-key", strict=True)

    assert status.registration_ready is True
    assert status.provider == "openai_responses"
    assert runtime is not None
    assert runtime.provider == "openai_responses"


def test_web_search_native_status_never_selects_tavily_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    cfg = _configured_cfg(mode="native", base_url="https://example-proxy.invalid/v1")
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")

    assert status.mode == "native"
    assert status.registration_ready is False
    assert status.provider is None
    assert status.availability_label == "native-unavailable"
    assert any("Native mode never falls back to Tavily" in note for note in status.notes)
    assert not any("TAVILY_API_KEY" in note for note in status.notes)

    with pytest.raises(
        WebSearchError,
        match="web_search is not available in native mode.*Native mode never falls back to Tavily",
    ):
        resolve_web_search_runtime(cfg=cfg, api_key="main-key", strict=True)


def test_web_search_external_status_selects_tavily_and_ignores_ready_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    cfg = _configured_cfg(mode="external")
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")
    runtime = resolve_web_search_runtime(cfg=cfg, api_key="main-key", strict=True)

    assert status.mode == "external"
    assert status.registration_ready is True
    assert status.provider == "tavily"
    assert runtime is not None
    assert runtime.provider == "tavily"


def test_web_search_auto_status_uses_dashscope_chat_for_qwen_coding_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    cfg = AppConfig(
        model="qwen3.5-plus",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        web_search_mode="auto",
    )
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")
    runtime = resolve_web_search_runtime(cfg=cfg, api_key="main-key", strict=True)

    assert status.registration_ready is True
    assert status.provider == "dashscope_chat"
    assert status.base_url == "https://coding-intl.dashscope.aliyuncs.com/v1"
    assert status.model == "qwen3.5-plus"
    assert runtime is not None
    assert runtime.provider == "dashscope_chat"


def test_web_search_auto_status_uses_dashscope_chat_for_qwen_us_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    cfg = AppConfig(
        model="qwen3.5-plus",
        base_url="https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        web_search_mode="auto",
    )
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")
    runtime = resolve_web_search_runtime(cfg=cfg, api_key="main-key", strict=True)

    assert status.registration_ready is True
    assert status.provider == "dashscope_chat"
    assert status.base_url == "https://dashscope-us.aliyuncs.com/compatible-mode/v1"
    assert status.model == "qwen3.5-plus"
    assert runtime is not None
    assert runtime.provider == "dashscope_chat"


def test_web_search_auto_status_supports_qwen37_responses_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    cfg = AppConfig(
        model="qwen3.7-plus",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        web_search_mode="auto",
    )
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")
    runtime = resolve_web_search_runtime(cfg=cfg, api_key="main-key", strict=True)

    assert status.registration_ready is True
    assert status.provider == "dashscope_chat"
    assert status.model == "qwen3.7-plus"
    assert runtime is not None
    assert runtime.provider == "dashscope_chat"


@pytest.mark.parametrize(
    ("base_url", "model", "expected_provider"),
    [
        ("https://api.moonshot.cn/v1", "kimi-k2.6", "moonshot_kimi"),
        ("https://api.moonshot.ai/v1", "kimi-k3", "moonshot_kimi"),
        ("https://open.bigmodel.cn/api/paas/v4/", "glm-4.6", "zhipu_web_search"),
        (
            "https://ark.cn-beijing.volces.com/api/v3",
            "doubao-seed-1-6-250615",
            "volcengine_web_search",
        ),
        ("https://api.minimax.io/v1", "MiniMax-M2.7", "minimax_coding_plan"),
    ],
)
def test_web_search_auto_status_uses_native_chinese_provider_adapters(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    model: str,
    expected_provider: str,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    cfg = AppConfig(model=model, base_url=base_url, web_search_mode="auto")
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")
    runtime = resolve_web_search_runtime(cfg=cfg, api_key="main-key", strict=True)

    assert status.registration_ready is True
    assert status.provider == expected_provider
    assert runtime is not None
    assert runtime.provider == expected_provider


def test_web_search_auto_status_uses_cohere_hosted_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    cfg = AppConfig(
        model="command-a-plus-05-2026",
        base_url="https://api.cohere.ai/compatibility/v1",
        web_search_mode="auto",
    )
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="cohere-key")
    runtime = resolve_web_search_runtime(cfg=cfg, api_key="cohere-key", strict=True)

    assert status.registration_ready is True
    assert status.provider == "cohere_web_search"
    assert runtime is not None
    assert runtime.provider == "cohere_web_search"


def test_is_openrouter_base_url_no_longer_matches_alysis_proxy() -> None:
    # The Alysis Code hosted proxy forwarded to OpenRouter only during the retired
    # MiMo (Xiaomi) trial; it now forwards to DeepSeek, which has no native web
    # search — so the proxy must NOT classify as an OpenRouter base_url anymore.
    assert _is_openrouter_base_url("https://api.alysiscode.com/v1") is False
    assert _is_openrouter_base_url(DEFAULT_PROXY_BASE_URL) is False
    assert _is_openrouter_base_url("https://openrouter.ai/api/v1") is True
    assert _is_openrouter_base_url("https://example.supabase.co/rest/v1") is False
    assert _is_openrouter_base_url("https://example.supabase.co/") is False
    assert _is_openrouter_base_url("https://api.openai.com/v1") is False
    assert _is_openrouter_base_url(None) is False


def test_web_search_no_longer_treats_alysis_proxy_as_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The hosted proxy now forwards to DeepSeek (no native web search), so the
    # MiMo-era OpenRouter routing must not activate for the proxy base_url.
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_ADAPTER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_BASE_URL", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    cfg = AppConfig(
        model="deepseek-v4-flash",
        base_url=DEFAULT_PROXY_BASE_URL,
        web_search_mode="auto",
    )
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="slk_test-key")

    assert status.provider != "openrouter_web"


def test_web_search_openrouter_floors_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A short web_search_timeout_s (e.g. 20s) starves the OpenRouter web round-trip,
    # so the openrouter_web adapter floors its per-attempt budget. An explicitly
    # higher timeout is preserved untouched.
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_ADAPTER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_TIMEOUT_S", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    floored = resolve_web_search_runtime(
        cfg=AppConfig(
            model="qwen-max",
            base_url="https://openrouter.ai/api/v1",
            web_search_mode="auto",
            web_search_adapter="openrouter_web",
            web_search_timeout_s=20.0,
        ),
        api_key="or-key",
        strict=True,
    )
    assert floored is not None
    assert floored.provider == "openrouter_web"
    assert floored.timeout_s == _OPENROUTER_WEB_MIN_TIMEOUT_S

    generous = resolve_web_search_runtime(
        cfg=AppConfig(
            model="qwen-max",
            base_url="https://openrouter.ai/api/v1",
            web_search_mode="auto",
            web_search_adapter="openrouter_web",
            web_search_timeout_s=120.0,
        ),
        api_key="or-key",
        strict=True,
    )
    assert generous is not None
    assert generous.timeout_s == 120.0


def test_web_search_auto_status_never_selects_openrouter_for_alysis_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Auto mode used to pick the OpenRouter backend for the proxy during the
    # MiMo trial. The proxy now forwards to DeepSeek, so no OpenRouter routing.
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_ADAPTER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    cfg = AppConfig(
        model="deepseek-v4-flash",
        base_url=DEFAULT_PROXY_BASE_URL,
        web_search_mode="auto",
    )
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="slk_test-key")

    assert status.provider != "openrouter_web"


def test_web_search_explicit_openai_override_uses_only_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_PROVIDER", "openai_responses")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    status = resolve_web_search_runtime_status(cfg=_configured_cfg(), api_key="main-key")

    assert status.provider == "openai_responses"
    assert status.registration_ready is True
    assert "explicit adapter selected openai_responses" in status.notes


def test_web_search_explicit_tavily_override_uses_only_tavily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    status = resolve_web_search_runtime_status(cfg=_configured_cfg(), api_key="main-key")

    assert status.provider == "tavily"
    assert status.registration_ready is True
    assert "explicit adapter selected tavily" in status.notes


def test_web_search_native_mode_rejects_explicit_external_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    cfg = AppConfig(
        model="main-model",
        base_url="https://api.openai.com/v1",
        web_search_mode="native",
        web_search_adapter="tavily",
    )
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")

    assert status.mode == "native"
    assert status.registration_ready is False
    assert status.provider is None
    assert "explicit adapter selected tavily" in status.notes
    assert any("web_search_mode=native is incompatible" in note for note in status.notes)

    with pytest.raises(
        WebSearchError,
        match="web_search_mode=native is incompatible with web_search_adapter=tavily",
    ):
        resolve_web_search_runtime(cfg=cfg, api_key="main-key", strict=True)


def test_web_search_external_mode_rejects_explicit_native_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    cfg = AppConfig(
        model="main-model",
        base_url="https://api.openai.com/v1",
        web_search_mode="external",
        web_search_adapter="openai_responses",
    )
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")

    assert status.mode == "external"
    assert status.registration_ready is False
    assert status.provider is None
    assert "explicit adapter selected openai_responses" in status.notes
    assert any("web_search_mode=external is incompatible" in note for note in status.notes)

    with pytest.raises(
        WebSearchError,
        match="web_search_mode=external is incompatible with web_search_adapter=openai_responses",
    ):
        resolve_web_search_runtime(cfg=cfg, api_key="main-key", strict=True)


def test_web_search_explicit_dashscope_override_uses_only_dashscope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_PROVIDER", "dashscope_chat")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    cfg = AppConfig(
        model="qwen3.5-plus",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        web_search_mode="auto",
    )
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")

    assert status.provider == "dashscope_chat"
    assert status.registration_ready is True
    assert "explicit adapter selected dashscope_chat" in status.notes


def test_web_search_explicit_tavily_override_without_key_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    cfg = _configured_cfg(base_url="https://example-proxy.invalid/v1")
    status = resolve_web_search_runtime_status(cfg=cfg, api_key="main-key")

    assert status.registration_ready is False
    assert status.provider is None
    assert status.availability_label == "auto-unavailable"
    assert "explicit adapter selected tavily" in status.notes
    assert "missing TAVILY_API_KEY" in status.summary

    with pytest.raises(WebSearchError, match="adapter tavily is not ready"):
        resolve_web_search_runtime(cfg=cfg, api_key="main-key", strict=True)


def test_tavily_accepts_generic_web_search_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_API_KEY", "external-search-key")
    cfg = _configured_cfg(
        mode="external",
        base_url="https://api.deepseek.com/v1",
    )
    cfg.web_search_adapter = "tavily"

    status = resolve_web_search_runtime_status(cfg=cfg, api_key="deepseek-key")
    runtime = resolve_web_search_runtime(cfg=cfg, api_key="deepseek-key", strict=True)

    assert status.registration_ready is True
    assert status.provider == "tavily"
    assert runtime is not None
    assert runtime.api_key == "external-search-key"


def test_web_search_auto_can_fall_back_from_openai_to_tavily_in_same_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            return httpx.Response(
                400,
                json={"error": {"message": "This provider does not support web_search."}},
            )
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={
                    "request_id": "tavily_req_2",
                    "answer": "Use the Tavily fallback answer.",
                    "results": [
                        {
                            "title": "Fallback docs",
                            "url": "https://docs.example.com/fallback",
                            "content": "Fallback snippet",
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected host: {request.url.host}")

    result = web_search(
        query="fallback docs",
        cfg=_configured_cfg(),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "tavily"
    assert result["protocol"] == "openai_compat"
    assert result["chat_protocol"] == "openai_compat"
    assert result["search_protocol"] == "tavily"
    assert result["backend_adapter"] == "tavily"
    assert result["provider_hosted_search"] is False
    assert result["external_search_provider"] == "tavily"
    assert result["citation_count"] == 1
    assert result["source_count"] == 1
    assert result["answer"] == "Use the Tavily fallback answer."
    assert result["response_id"] == "tavily_req_2"
    assert result["citations"][0]["start_index"] is None
    assert result["citations"][0]["end_index"] is None
    assert result["sources"][0]["snippet"] == "Fallback snippet"


def test_web_search_native_mode_does_not_fallback_to_tavily_in_same_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(str(request.url.host))
        if request.url.host == "api.tavily.com":
            raise AssertionError("native mode must not call Tavily")
        return httpx.Response(400, json={"error": {"message": "OpenAI failure"}})

    with pytest.raises(WebSearchError, match="OpenAI failure"):
        web_search(
            query="failing docs",
            cfg=_configured_cfg(mode="native"),
            api_key="main-key",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
        )

    assert seen_hosts == ["api.openai.com"]


def test_web_search_external_mode_calls_only_tavily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(str(request.url.host))
        if request.url.host == "api.openai.com":
            raise AssertionError("external mode must not call native provider search")
        return httpx.Response(
            200,
            json={
                "request_id": "external_tavily",
                "answer": "External Tavily answer.",
                "results": [
                    {
                        "title": "External docs",
                        "url": "https://docs.example.com/external",
                        "content": "External snippet",
                    }
                ],
            },
        )

    result = web_search(
        query="external docs",
        cfg=_configured_cfg(mode="external"),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert seen_hosts == ["api.tavily.com"]
    assert result["backend"] == "tavily"
    assert result["response_id"] == "external_tavily"


def test_web_search_auto_reports_combined_error_when_all_backends_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "0")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            return httpx.Response(400, json={"error": {"message": "OpenAI failure"}})
        if request.url.host == "api.tavily.com":
            return httpx.Response(502, json={"error": "Tavily failure"})
        raise AssertionError(f"unexpected host: {request.url.host}")

    with pytest.raises(
        WebSearchError,
        match="web_search failed across auto backends: openai_responses: Responses error 400: OpenAI failure; tavily: Tavily error 502: Tavily failure",
    ):
        web_search(
            query="failing docs",
            cfg=_configured_cfg(),
            api_key="main-key",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
        )


def test_web_search_auto_falls_back_to_keyless_ddgs_when_native_and_tavily_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_KEYLESS", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            return httpx.Response(400, json={"error": {"message": "OpenAI failure"}})
        if request.url.host == "api.tavily.com":
            return httpx.Response(502, json={"error": "Tavily failure"})
        raise AssertionError(f"unexpected host: {request.url.host}")

    def _fake_ddgs_search(**kwargs: object) -> dict[str, object]:
        assert kwargs["query"] == "failing docs"
        return {
            "query": "failing docs",
            "answer": "",
            "citations": [
                {
                    "title": "Keyless result",
                    "url": "https://docs.example.com/keyless",
                    "start_index": None,
                    "end_index": None,
                }
            ],
            "sources": [{"url": "https://docs.example.com/keyless", "title": "Keyless result"}],
            "queries": ["failing docs"],
            "model": None,
            "backend": "ddgs",
            "allowed_domains": [],
            "external_web_access": True,
            "response_id": None,
            "sources_truncated": False,
        }

    monkeypatch.setattr(web_search_module, "ddgs_search", _fake_ddgs_search)

    result = web_search(
        query="failing docs",
        cfg=_configured_cfg(),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "ddgs"
    assert result["backend_adapter"] == "ddgs"
    assert result["search_protocol"] == "ddgs"
    assert result["provider_hosted_search"] is False
    assert result["external_search_provider"] == "ddgs"
    assert result["sources"][0]["url"] == "https://docs.example.com/keyless"
    # Every result carries host wall-clock provenance so the model can anchor
    # "today"/"current" reasoning to real time (parseable, timezone-aware ISO).
    from datetime import datetime as _datetime

    retrieved_at = _datetime.fromisoformat(result["retrieved_at"])
    assert retrieved_at.tzinfo is not None


def test_web_search_auto_combined_error_includes_keyless_ddgs_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_KEYLESS", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            return httpx.Response(400, json={"error": {"message": "OpenAI failure"}})
        if request.url.host == "api.tavily.com":
            return httpx.Response(502, json={"error": "Tavily failure"})
        raise AssertionError(f"unexpected host: {request.url.host}")

    def _failing_ddgs_search(**_kwargs: object) -> dict[str, object]:
        raise DdgsSearchError("keyless (ddgs) search failed: rate limited")

    monkeypatch.setattr(web_search_module, "ddgs_search", _failing_ddgs_search)

    with pytest.raises(
        WebSearchError,
        match=(
            "web_search failed across auto backends: "
            "openai_responses: Responses error 400: OpenAI failure; "
            "tavily: Tavily error 502: Tavily failure; "
            r"ddgs: keyless \(ddgs\) search failed: rate limited"
        ),
    ):
        web_search(
            query="failing docs",
            cfg=_configured_cfg(),
            api_key="main-key",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
        )


def test_web_search_keyless_ddgs_serves_provider_without_native_search_and_no_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DeepSeek scenario: chat key only, unknown base_url, no external key."""

    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_KEYLESS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def _fake_ddgs_search(**kwargs: object) -> dict[str, object]:
        assert kwargs["query"] == "latest python release"
        return {
            "query": "latest python release",
            "answer": "",
            "citations": [],
            "sources": [{"url": "https://python.org/downloads", "title": "Downloads"}],
            "queries": ["latest python release"],
            "model": None,
            "backend": "ddgs",
            "allowed_domains": [],
            "external_web_access": True,
            "response_id": None,
            "sources_truncated": False,
        }

    monkeypatch.setattr(web_search_module, "ddgs_search", _fake_ddgs_search)

    cfg = AppConfig(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        web_search_mode="auto",
    )
    runtime = resolve_web_search_runtime(cfg=cfg, api_key="deepseek-key")
    assert runtime is not None
    assert runtime.provider == "ddgs"

    result = web_search(
        query="latest python release",
        cfg=cfg,
        api_key="deepseek-key",
        resolver=_public_resolver,
    )

    assert result["backend"] == "ddgs"
    assert result["provider_hosted_search"] is False
    assert result["sources"][0]["url"] == "https://python.org/downloads"


def test_web_search_tavily_output_contract_stays_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "tavily_req_3",
                "answer": "Read the migration guide.",
                "results": [
                    {
                        "title": "Guide",
                        "url": "https://docs.example.com/guide",
                        "content": "Guide snippet",
                    }
                ],
            },
        )

    result = web_search(
        query="migration guide",
        cfg=_configured_cfg(base_url="https://example-proxy.invalid/v1"),
        api_key="main-key",
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    # Time-varying host provenance: assert shape separately, compare the rest
    # of the contract exactly.
    from datetime import datetime as _datetime

    retrieved_at = result.pop("retrieved_at")
    assert _datetime.fromisoformat(retrieved_at).tzinfo is not None
    assert result == {
        "query": "migration guide",
        "answer": "Read the migration guide.",
        "citations": [
            {
                "title": "Guide",
                "url": "https://docs.example.com/guide",
                "start_index": None,
                "end_index": None,
            }
        ],
        "sources": [
            {
                "url": "https://docs.example.com/guide",
                "title": "Guide",
                "snippet": "Guide snippet",
            }
        ],
        "queries": ["migration guide"],
        "model": None,
        "backend": "tavily",
        "protocol": "openai_compat",
        "chat_protocol": "openai_compat",
        "search_protocol": "tavily",
        "backend_adapter": "tavily",
        "provider_hosted_search": False,
        "external_search_provider": "tavily",
        "citation_count": 1,
        "source_count": 1,
        "allowed_domains": ["docs.example.com"],
        "external_web_access": True,
        "response_id": "tavily_req_3",
        "sources_truncated": False,
    }


def test_web_search_rejects_external_web_access_false_for_tavily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    with pytest.raises(
        WebSearchError,
        match="external_web_access=false is supported only by the openai_responses web_search backend; tavily always uses external web access",
    ):
        web_search(
            query="migration guide",
            cfg=_configured_cfg(base_url="https://example-proxy.invalid/v1"),
            api_key="main-key",
            external_web_access=False,
        )


def test_web_search_rejects_invalid_query() -> None:
    with pytest.raises(WebSearchError, match="query must be a non-empty string"):
        web_search(query="   ", cfg=_configured_cfg(), api_key="main-key")


def test_web_search_rejects_invalid_allowed_domains() -> None:
    with pytest.raises(WebSearchError, match="allowed_domains must be an array"):
        web_search(
            query="httpx docs",
            cfg=_configured_cfg(),
            api_key="main-key",
            allowed_domains="python.org",  # type: ignore[arg-type]
        )
    with pytest.raises(
        WebSearchError, match="allowed_domains must contain only non-empty domain strings"
    ):
        web_search(
            query="httpx docs",
            cfg=_configured_cfg(),
            api_key="main-key",
            allowed_domains=["python.org", ""],
        )


def test_web_search_plumbs_allowed_domains_and_external_access_to_openai_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        tool_spec = body["tools"][0]
        assert tool_spec["filters"] == {"allowed_domains": ["docs.python.org"]}
        assert tool_spec["external_web_access"] is False
        return httpx.Response(
            200,
            json={
                "id": "resp_3",
                "model": "main-model",
                "output_text": "Use the official docs.",
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "queries": ["python pathlib docs"],
                            "sources": [
                                {
                                    "title": "pathlib",
                                    "url": "https://docs.python.org/3/library/pathlib.html",
                                }
                            ],
                        },
                    }
                ],
            },
        )

    result = web_search(
        query="python pathlib docs",
        cfg=_configured_cfg(),
        api_key="main-key",
        allowed_domains=["docs.python.org"],
        external_web_access=False,
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "openai_responses"
    assert result["allowed_domains"] == ["docs.python.org"]
    assert result["external_web_access"] is False
    assert result["sources"] == [
        {
            "url": "https://docs.python.org/3/library/pathlib.html",
            "title": "pathlib",
        }
    ]


def test_web_search_dispatches_to_dashscope_chat_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://coding-intl.dashscope.aliyuncs.com/v1/chat/completions"
        body = json.loads(request.content.decode("utf-8"))
        assert body["enable_search"] is True
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_dashscope_2",
                "model": "qwen3.5-plus",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "DashScope searched the web.",
                                    "sources": [
                                        {
                                            "title": "Docs",
                                            "url": "https://docs.example.com/search",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ],
            },
        )

    result = web_search(
        query="dashscope search docs",
        cfg=AppConfig(
            model="qwen3.5-plus",
            base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "dashscope_chat"
    assert result["answer"] == "DashScope searched the web."
    assert result["sources"] == [{"url": "https://docs.example.com/search", "title": "Docs"}]


def test_web_search_dispatches_to_xai_responses_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.x.ai/v1/responses"
        body = json.loads(request.content.decode("utf-8"))
        assert body["tools"] == [{"type": "web_search"}]
        assert body["tool_choice"] == "required"
        assert "include" not in body
        return httpx.Response(
            200,
            json={
                "id": "resp_xai_1",
                "model": "grok-4",
                "output_text": "xAI searched the web.",
                "citations": ["https://docs.x.ai/docs"],
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "queries": ["xai docs"],
                        },
                    }
                ],
            },
        )

    result = web_search(
        query="xai docs",
        cfg=AppConfig(
            model="grok-4",
            base_url="https://api.x.ai/v1",
            web_search_adapter="xai_responses",
        ),
        api_key="xai-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "xai_responses"
    assert result["sources"] == [{"url": "https://docs.x.ai/docs", "title": ""}]


def test_web_search_dispatches_to_anthropic_messages_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "ant-key"
        assert request.headers["accept-encoding"] == "identity"
        body = json.loads(request.content.decode("utf-8"))
        assert body["tools"][0]["type"] == "web_search_20260209"
        assert body["tools"][0]["allowed_domains"] == ["docs.example.com"]
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "claude-sonnet-4-6",
                "content": [
                    {"type": "server_tool_use", "input": {"query": "anthropic search"}},
                    {
                        "type": "web_search_tool_result",
                        "content": [
                            {
                                "type": "web_search_result",
                                "title": "Anthropic Docs",
                                "url": "https://docs.example.com/anthropic",
                            }
                        ],
                    },
                    {
                        "type": "text",
                        "text": "Anthropic searched the web.",
                        "citations": [
                            {
                                "type": "web_search_result_location",
                                "title": "Anthropic Docs",
                                "url": "https://docs.example.com/anthropic",
                            }
                        ],
                    },
                ],
            },
        )

    result = web_search(
        query="anthropic search",
        cfg=AppConfig(
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.com/v1/",
            web_search_adapter="anthropic_messages",
        ),
        api_key="ant-key",
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "anthropic_messages"
    assert result["queries"] == ["anthropic search"]
    assert result["sources"] == [
        {"url": "https://docs.example.com/anthropic", "title": "Anthropic Docs"}
    ]


def test_web_search_dispatches_to_gemini_grounding_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            str(request.url)
            == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        )
        assert request.headers["x-goog-api-key"] == "gem-key"
        body = json.loads(request.content.decode("utf-8"))
        assert body["tools"] == [{"google_search": {}}]
        return httpx.Response(
            200,
            json={
                "responseId": "gem_resp_1",
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "Gemini grounded the answer."}],
                            "role": "model",
                        },
                        "groundingMetadata": {
                            "webSearchQueries": ["gemini search"],
                            "groundingChunks": [
                                {
                                    "web": {
                                        "uri": "https://docs.example.com/gemini",
                                        "title": "Gemini Docs",
                                    }
                                }
                            ],
                            "groundingSupports": [
                                {
                                    "segment": {"startIndex": 0, "endIndex": 6},
                                    "groundingChunkIndices": [0],
                                }
                            ],
                        },
                    }
                ],
            },
        )

    result = web_search(
        query="gemini search",
        cfg=AppConfig(
            model="gemini-2.5-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            web_search_adapter="gemini_grounding",
        ),
        api_key="gem-key",
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "gemini_grounding"
    assert result["queries"] == ["gemini search"]
    assert result["citations"][0]["url"] == "https://docs.example.com/gemini"


def test_web_search_dispatches_to_openrouter_web_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        body = json.loads(request.content.decode("utf-8"))
        assert body["tools"][0]["type"] == "openrouter:web_search"
        assert body["tools"][0]["parameters"]["allowed_domains"] == ["docs.example.com"]
        return httpx.Response(
            200,
            json={
                "id": "or_1",
                "model": "anthropic/claude-sonnet-4.6",
                "choices": [
                    {
                        "message": {
                            "content": "OpenRouter searched the web.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "title": "OpenRouter Docs",
                                        "url": "https://docs.example.com/openrouter",
                                        "start_index": 0,
                                        "end_index": 10,
                                    },
                                },
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "title": "Filtered",
                                        "url": "https://outside.example.com/openrouter",
                                        "start_index": 12,
                                        "end_index": 20,
                                    },
                                },
                            ],
                        }
                    }
                ],
            },
        )

    result = web_search(
        query="openrouter search",
        cfg=AppConfig(
            model="anthropic/claude-sonnet-4.6",
            base_url="https://openrouter.ai/api/v1",
            web_search_adapter="openrouter_web",
        ),
        api_key="or-key",
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "openrouter_web"
    assert result["sources"] == [
        {"url": "https://docs.example.com/openrouter", "title": "OpenRouter Docs"}
    ]
    assert [citation["url"] for citation in result["citations"]] == [
        "https://docs.example.com/openrouter"
    ]


def test_web_search_dispatches_to_moonshot_kimi_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.moonshot.cn/v1/chat/completions"
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        assert body["model"] == "kimi-k2.6"
        assert body["tools"] == [{"type": "builtin_function", "function": {"name": "$web_search"}}]
        assert body["thinking"] == {"type": "disabled"}
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "kimi_tool_1",
                    "model": "kimi-k2.6",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "$web_search",
                                            "arguments": json.dumps(
                                                {
                                                    "query": "kimi search",
                                                    "results": [
                                                        {
                                                            "title": "Kimi Docs",
                                                            "url": "https://docs.example.com/kimi",
                                                            "snippet": "Kimi snippet",
                                                        }
                                                    ],
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                },
            )
        assert body["messages"][-1]["role"] == "tool"
        assert body["messages"][-1]["tool_call_id"] == "call_1"
        return httpx.Response(
            200,
            json={
                "id": "kimi_final",
                "model": "kimi-k2.6",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Kimi searched the web."},
                    }
                ],
            },
        )

    result = web_search(
        query="kimi search",
        cfg=AppConfig(
            model="kimi-k2",
            base_url="https://api.moonshot.cn/v1",
            web_search_adapter="moonshot_kimi",
            web_search_model="kimi-k2.6",
        ),
        api_key="moonshot-key",
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "moonshot_kimi"
    assert result["response_id"] == "kimi_final"
    assert result["queries"] == ["kimi search"]
    assert result["sources"] == [
        {
            "url": "https://docs.example.com/kimi",
            "title": "Kimi Docs",
            "snippet": "Kimi snippet",
        }
    ]


def test_web_search_dispatches_to_zhipu_web_search_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://open.bigmodel.cn/api/paas/v4/web_search"
        body = json.loads(request.content.decode("utf-8"))
        assert body == {
            "search_query": "zhipu search",
            "search_engine": "search_pro",
            "search_intent": False,
            "count": 5,
            "search_recency_filter": "noLimit",
            "content_size": "medium",
            "search_domain_filter": "docs.example.com",
        }
        return httpx.Response(
            200,
            json={
                "id": "glm_1",
                "search_intent": [{"query": "zhipu search", "keywords": "zhipu search"}],
                "search_result": [
                    {
                        "title": "GLM Docs",
                        "link": "https://docs.example.com/glm",
                        "content": "GLM snippet",
                        "media": "Docs",
                    }
                ],
            },
        )

    result = web_search(
        query="zhipu search",
        cfg=AppConfig(
            model="glm-4.6",
            base_url="https://open.bigmodel.cn/api/paas/v4/",
            web_search_adapter="zhipu_web_search",
        ),
        api_key="glm-key",
        max_sources=5,
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "zhipu_web_search"
    assert result["answer"] == ""
    assert result["sources"] == [
        {
            "url": "https://docs.example.com/glm",
            "title": "GLM Docs",
            "snippet": "GLM snippet",
        }
    ]


def test_web_search_dispatches_to_volcengine_web_search_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://ark.cn-beijing.volces.com/api/v3/responses"
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "doubao-seed-1-6-250615"
        assert body["tools"] == [{"type": "web_search"}]
        assert body["stream"] is False
        return httpx.Response(
            200,
            json={
                "id": "ark_resp_1",
                "model": "doubao-seed-1-6-250615",
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {"queries": ["doubao search"]},
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Doubao searched the web.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "title": "Doubao Docs",
                                        "url": "https://docs.example.com/doubao",
                                        "content": "Doubao snippet",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
        )

    result = web_search(
        query="doubao search",
        cfg=AppConfig(
            model="doubao-1.5-pro",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            web_search_adapter="volcengine_web_search",
            web_search_model="doubao-seed-1-6-250615",
        ),
        api_key="ark-key",
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "volcengine_web_search"
    assert result["model"] == "doubao-seed-1-6-250615"
    assert result["sources"] == [
        {
            "url": "https://docs.example.com/doubao",
            "title": "Doubao Docs",
            "snippet": "Doubao snippet",
        }
    ]


def test_web_search_openrouter_answer_without_sources_falls_back_to_tavily_in_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(
                200,
                json={
                    "id": "or_no_search",
                    "model": "openai/gpt-5.2",
                    "choices": [{"message": {"content": "Answer without search evidence."}}],
                },
            )
        if request.url.host == "api.tavily.com":
            return httpx.Response(
                200,
                json={
                    "request_id": "tavily_after_openrouter",
                    "answer": "Tavily searched instead.",
                    "results": [
                        {
                            "title": "Fallback Source",
                            "url": "https://docs.example.com/fallback",
                            "content": "fallback snippet",
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected host: {request.url.host}")

    result = web_search(
        query="current docs",
        cfg=AppConfig(
            model="openai/gpt-5.2",
            base_url="https://openrouter.ai/api/v1",
            web_search_adapter="auto",
        ),
        api_key="or-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "tavily"
    assert result["response_id"] == "tavily_after_openrouter"
    assert result["sources"][0]["url"] == "https://docs.example.com/fallback"


def test_web_search_openrouter_answer_without_sources_errors_when_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            raise AssertionError("explicit adapter should not fall back to Tavily")
        return httpx.Response(
            200,
            json={
                "id": "or_no_search",
                "model": "openai/gpt-5.2",
                "choices": [{"message": {"content": "Answer without search evidence."}}],
            },
        )

    with pytest.raises(WebSearchError, match="OpenRouter web_search did not return sources"):
        web_search(
            query="current docs",
            cfg=AppConfig(
                model="openai/gpt-5.2",
                base_url="https://openrouter.ai/api/v1",
                web_search_adapter="openrouter_web",
            ),
            api_key="or-key",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
        )


def test_web_search_dispatches_to_perplexity_sonar_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        # Agent API endpoint; the preset base URL already carries /v1.
        assert str(request.url) == "https://api.perplexity.ai/v1/agent"
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "perplexity/sonar"
        assert body["input"] == "perplexity search"
        # Grounding is opt-in on the Agent API; domain filters live on the tool.
        assert body["tools"] == [
            {
                "type": "web_search",
                "search_context_size": "low",
                "max_results": 5,
                "filters": {"search_domain_filter": ["docs.example.com"]},
            }
        ]
        assert "messages" not in body
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "perplexity/sonar",
                "object": "response",
                "output": [
                    {
                        "type": "search_results",
                        "results": [
                            {
                                "id": 1,
                                "title": "Perplexity Docs",
                                "url": "https://docs.example.com/perplexity",
                                "snippet": "PPLX snippet",
                                "date": "2026-08-01",
                            }
                        ],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "Perplexity searched the web.[1]"}
                        ],
                    },
                ],
                "status": "completed",
            },
        )

    result = web_search(
        query="perplexity search",
        cfg=AppConfig(
            model="perplexity/glm-5.3",
            base_url="https://api.perplexity.ai/v1",
            web_search_adapter="perplexity_sonar",
            web_search_model="perplexity/sonar",
        ),
        api_key="pplx-key",
        allowed_domains=["docs.example.com"],
        max_sources=5,
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "perplexity_sonar"
    assert result["answer"] == "Perplexity searched the web.[1]"
    assert result["sources"][0]["snippet"] == "PPLX snippet"
    assert result["response_id"] == "resp_1"


def test_perplexity_search_migrates_legacy_sonar_profiles_to_the_agent_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile saved against the retired Sonar surface still reaches /v1/agent."""
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.perplexity.ai/v1/agent"
        body = json.loads(request.content.decode("utf-8"))
        # Retired Sonar chat ids are not Agent API models; they map to the
        # hosted Sonar route.
        assert body["model"] == "perplexity/sonar"
        assert "filters" not in body["tools"][0]
        return httpx.Response(
            200,
            json={
                "id": "resp_2",
                "output": [
                    {
                        "type": "search_results",
                        "results": [{"title": "Docs", "url": "https://docs.example.com/x"}],
                    },
                    {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
                ],
            },
        )

    result = web_search(
        query="legacy sonar profile",
        cfg=AppConfig(
            model="sonar-pro",
            base_url="https://api.perplexity.ai",
            web_search_adapter="perplexity_sonar",
            web_search_model="sonar",
        ),
        api_key="pplx-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "perplexity_sonar"
    assert result["answer"] == "ok"
    assert result["sources"][0]["url"] == "https://docs.example.com/x"


def test_web_search_dispatches_to_groq_compound_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "groq/compound-mini"
        assert body["search_settings"] == {"include_domains": ["docs.example.com"]}
        return httpx.Response(
            200,
            json={
                "id": "groq_1",
                "model": "groq/compound-mini",
                "choices": [
                    {
                        "message": {
                            "content": "Groq searched the web.",
                            "executed_tools": [
                                {
                                    "search_results": {
                                        "results": [
                                            {
                                                "title": "Groq Docs",
                                                "url": "https://docs.example.com/groq",
                                                "content": "Groq snippet",
                                            }
                                        ]
                                    }
                                }
                            ],
                        }
                    }
                ],
            },
        )

    result = web_search(
        query="groq search",
        cfg=AppConfig(
            model="llama-3.3-70b-versatile",
            base_url="https://api.groq.com/openai/v1",
            web_search_adapter="groq_compound",
        ),
        api_key="groq-key",
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "groq_compound"
    assert result["model"] == "groq/compound-mini"
    assert result["sources"][0]["snippet"] == "Groq snippet"


def test_web_search_dispatches_to_mistral_conversations_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.mistral.ai/v1/conversations"
        body = json.loads(request.content.decode("utf-8"))
        assert body["tools"] == [{"type": "web_search"}]
        assert body["store"] is False
        return httpx.Response(
            200,
            json={
                "conversation_id": "conv_1",
                "outputs": [
                    {
                        "type": "message.output",
                        "content": [
                            {"type": "text", "text": "Mistral searched the web."},
                            {
                                "type": "tool_reference",
                                "title": "Mistral Docs",
                                "url": "https://docs.example.com/mistral",
                            },
                        ],
                    }
                ],
            },
        )

    result = web_search(
        query="mistral search",
        cfg=AppConfig(
            model="mistral-large-latest",
            base_url="https://api.mistral.ai/v1",
            web_search_adapter="mistral_conversations",
            web_search_model="mistral-medium-latest",
        ),
        api_key="mistral-key",
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "mistral_conversations"
    assert result["sources"] == [
        {"url": "https://docs.example.com/mistral", "title": "Mistral Docs"}
    ]


def test_web_search_dispatches_to_minimax_token_plan_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.minimax.io/v1/coding_plan/search"
        assert request.headers["MM-API-Source"] == "Minimax-MCP"
        assert json.loads(request.content.decode("utf-8")) == {"q": "MiniMax search"}
        return httpx.Response(
            200,
            json={
                "id": "minimax_search_1",
                "organic": [
                    {
                        "title": "MiniMax Docs",
                        "link": "https://docs.example.com/minimax",
                        "snippet": "MiniMax search documentation.",
                    }
                ],
                "related_searches": [{"query": "MiniMax Token Plan"}],
                "base_resp": {"status_code": 0, "status_msg": "success"},
            },
        )

    result = web_search(
        query="MiniMax search",
        cfg=AppConfig(
            model="MiniMax-M2.7",
            base_url="https://api.minimax.io/v1",
            web_search_adapter="minimax_coding_plan",
        ),
        api_key="minimax-token-plan-key",
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "minimax_coding_plan"
    assert result["queries"] == ["MiniMax search", "MiniMax Token Plan"]
    assert result["sources"] == [
        {
            "url": "https://docs.example.com/minimax",
            "title": "MiniMax Docs",
            "snippet": "MiniMax search documentation.",
        }
    ]


def test_web_search_dispatches_to_cohere_hosted_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.cohere.com/v1/chat"
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "command-a-plus-05-2026"
        assert body["connectors"] == [{"id": "web-search"}]
        assert body["prompt_truncation"] == "AUTO"
        return httpx.Response(
            200,
            json={
                "response_id": "cohere_search_1",
                "text": "Cohere searched the web.",
                "search_queries": [{"text": "Cohere hosted search"}],
                "citations": [{"start": 0, "end": 6, "document_ids": ["web-search_1:0"]}],
                "documents": [
                    {
                        "id": "web-search_1:0",
                        "title": "Cohere Docs",
                        "url": "https://docs.example.com/cohere",
                        "snippet": "Cohere connector documentation.",
                    }
                ],
            },
        )

    result = web_search(
        query="Cohere search",
        cfg=AppConfig(
            model="command-a-plus-05-2026",
            base_url="https://api.cohere.ai/compatibility/v1",
            web_search_adapter="cohere_web_search",
        ),
        api_key="cohere-key",
        allowed_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "cohere_web_search"
    assert result["answer"] == "Cohere searched the web."
    assert result["queries"] == ["Cohere hosted search"]
    assert result["citations"] == [
        {
            "title": "Cohere Docs",
            "url": "https://docs.example.com/cohere",
            "start_index": 0,
            "end_index": 6,
        }
    ]


def test_web_search_rejects_external_web_access_false_for_dashscope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    with pytest.raises(
        WebSearchError,
        match="external_web_access=false is supported only by the openai_responses web_search backend; dashscope_chat always uses external web access",
    ):
        web_search(
            query="dashscope search docs",
            cfg=AppConfig(
                model="qwen3.5-plus",
                base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
                web_search_mode="auto",
            ),
            api_key="main-key",
            external_web_access=False,
        )


def test_web_search_does_not_fall_back_to_tavily_when_external_web_access_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            raise AssertionError("should not fall back to Tavily")
        return httpx.Response(
            400,
            json={"error": {"message": "OpenAI failure"}},
        )

    with pytest.raises(WebSearchError, match="OpenAI failure"):
        web_search(
            query="failing docs",
            cfg=_configured_cfg(),
            api_key="main-key",
            external_web_access=False,
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
        )


def test_resolve_web_search_runtime_status_reports_off_mode_as_disabled() -> None:
    status = resolve_web_search_runtime_status(
        cfg=AppConfig(web_search_mode="off"),
        api_key=None,
    )

    assert status.mode == "off"
    assert status.provider is None
    assert status.registration_ready is False
    assert status.availability_label == "disabled"
    assert status.summary.startswith("disabled by policy:")


def test_resolve_web_search_runtime_status_reports_auto_unavailable_notes_are_informative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "0")
    cfg = AppConfig(
        base_url="https://example-proxy.invalid/v1",
        web_search_mode="auto",
    )

    status = resolve_web_search_runtime_status(cfg=cfg, api_key=None)

    assert status.mode == "auto"
    assert status.provider is None
    assert status.registration_ready is False
    assert status.availability_label == "auto-unavailable"
    assert status.base_url == "https://example-proxy.invalid/v1"
    assert status.model is None
    assert status.api_key_available is False
    assert any(
        "OpenAI auto readiness requires explicit web_search_base_url" in note
        for note in status.notes
    )
    assert any("missing model" in note for note in status.notes)
    assert any("missing API key" in note for note in status.notes)
    assert any("missing TAVILY_API_KEY" in note for note in status.notes)
    assert any("keyless web search is disabled" in note for note in status.notes)
    assert "TAVILY_API_KEY" in status.setup_hint
    assert "provider-agnostic fallback" in status.setup_hint


def test_resolve_web_search_runtime_status_reports_keyless_ddgs_ready_without_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_KEYLESS", raising=False)
    cfg = AppConfig(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        web_search_mode="auto",
    )

    status = resolve_web_search_runtime_status(cfg=cfg, api_key=None)

    assert status.mode == "auto"
    assert status.provider == "ddgs"
    assert status.registration_ready is True
    assert status.availability_label == "available"
    assert "Keyless web search" in status.setup_hint


def test_resolve_web_search_runtime_status_reports_ready_setup_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    status = resolve_web_search_runtime_status(
        cfg=_configured_cfg(base_url="https://example-proxy.invalid/v1"),
        api_key="main-key",
    )

    assert status.registration_ready is True
    assert status.provider == "tavily"
    assert "Provider-agnostic web search is ready" in status.setup_hint
    assert status.to_payload()["setup_hint"] == status.setup_hint


def test_resolve_web_search_runtime_status_legacy_on_maps_to_auto_ready_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    status = resolve_web_search_runtime_status(
        cfg=_configured_cfg(
            mode="on",
            base_url="https://example-proxy.invalid/v1",
            web_search_base_url="https://search.example.com/v1",
        ),
        api_key="main-key",
    )

    assert status.mode == "auto"
    assert status.registration_ready is True
    assert status.provider == "openai_responses"
    assert status.availability_label == "available"
    assert status.base_url == "https://search.example.com/v1"


@pytest.mark.parametrize(
    ("cfg", "api_key", "env", "message"),
    [
        (
            AppConfig(
                web_search_mode="auto", model="main-model", base_url="", web_search_timeout_s=20.0
            ),
            "main-key",
            {},
            "missing OpenAI search base URL",
        ),
        (
            AppConfig(
                web_search_mode="auto",
                model="",
                base_url="https://api.openai.com/v1",
                web_search_timeout_s=20.0,
            ),
            "main-key",
            {},
            "missing model",
        ),
        (
            AppConfig(
                web_search_mode="auto",
                model="main-model",
                base_url="https://api.openai.com/v1",
                web_search_timeout_s=20.0,
            ),
            None,
            {},
            "missing API key",
        ),
        (
            AppConfig(
                web_search_mode="auto",
                model="main-model",
                base_url="https://api.openai.com/v1",
                web_search_timeout_s=0.0,
            ),
            "main-key",
            {},
            "web_search_timeout_s must be > 0",
        ),
        (
            AppConfig(
                web_search_mode="auto",
                model="main-model",
                base_url="https://example-proxy.invalid/v1",
                web_search_timeout_s=20.0,
            ),
            "main-key",
            {"ALYSIS_WEB_SEARCH_PROVIDER": "tavily"},
            "adapter tavily is not ready",
        ),
    ],
)
def test_resolve_web_search_runtime_strict_mode_rejects_missing_requirements(
    monkeypatch: pytest.MonkeyPatch,
    cfg: AppConfig,
    api_key: str | None,
    env: dict[str, str],
    message: str,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "0")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(WebSearchError, match=message):
        resolve_web_search_runtime(cfg=cfg, api_key=api_key, strict=True)


def _fake_ddgs_result(query: str) -> dict[str, object]:
    return {
        "query": query,
        "answer": "",
        "citations": [],
        "sources": [{"url": "https://docs.example.com/keyless", "title": "Keyless result"}],
        "queries": [query],
        "model": None,
        "backend": "ddgs",
        "allowed_domains": [],
        "external_web_access": True,
        "response_id": None,
        "sources_truncated": False,
    }


def test_web_search_auto_prefers_working_fallback_for_rest_of_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the native backend hard-fails once, later calls in the same session
    go straight to the external backend that served the result instead of paying
    a doomed native round-trip on every call."""

    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_KEYLESS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    web_search_module._reset_web_search_session_state_for_tests()

    native_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        native_calls.append(str(request.url.host))
        return httpx.Response(
            400,
            json={"error": {"message": "native web search backend is unavailable"}},
        )

    ddgs_calls: list[str] = []

    def _fake_ddgs_search(**kwargs: object) -> dict[str, object]:
        query = str(kwargs["query"])
        ddgs_calls.append(query)
        return _fake_ddgs_result(query)

    monkeypatch.setattr(web_search_module, "ddgs_search", _fake_ddgs_search)

    first = web_search(
        query="first query",
        cfg=_configured_cfg(),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
        session_id="session-sticky-fallback",
    )
    assert first["backend"] == "ddgs"
    native_calls_after_first = len(native_calls)
    assert native_calls_after_first >= 1

    from alysis_code.provider_telemetry import last_web_search_summary

    summary = last_web_search_summary()
    assert summary is not None
    assert summary["fallback_occurred"] is True

    second = web_search(
        query="second query",
        cfg=_configured_cfg(),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
        session_id="session-sticky-fallback",
    )
    assert second["backend"] == "ddgs"
    assert len(native_calls) == native_calls_after_first  # no new native attempt
    assert ddgs_calls == ["first query", "second query"]

    summary = last_web_search_summary()
    assert summary is not None
    assert summary["fallback_occurred"] is True

    web_search_module._reset_web_search_session_state_for_tests()


def test_web_search_auto_fallback_preference_is_scoped_to_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_KEYLESS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    web_search_module._reset_web_search_session_state_for_tests()

    native_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        native_calls.append(str(request.url.host))
        return httpx.Response(400, json={"error": {"message": "native backend down"}})

    monkeypatch.setattr(
        web_search_module,
        "ddgs_search",
        lambda **kwargs: _fake_ddgs_result(str(kwargs["query"])),
    )

    web_search(
        query="first query",
        cfg=_configured_cfg(),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
        session_id="session-a",
    )
    assert len(native_calls) == 1

    # A different session still gives the configured native backend a chance.
    web_search(
        query="other session query",
        cfg=_configured_cfg(),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
        session_id="session-b",
    )
    assert len(native_calls) == 2

    web_search_module._reset_web_search_session_state_for_tests()


def test_web_search_auto_retries_native_after_preferred_fallback_stops_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_KEYLESS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    web_search_module._reset_web_search_session_state_for_tests()

    native_calls: list[str] = []
    native_healthy = False

    def handler(request: httpx.Request) -> httpx.Response:
        native_calls.append(str(request.url.host))
        if not native_healthy:
            return httpx.Response(400, json={"error": {"message": "native backend down"}})
        return httpx.Response(
            200,
            json={
                "id": "resp_native_recovered",
                "model": "main-model",
                "output_text": "Native answer.",
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "sources": [{"url": "https://docs.example.com/native", "title": "N"}]
                        },
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Native answer."}],
                    },
                ],
            },
        )

    ddgs_healthy = True

    def _fake_ddgs_search(**kwargs: object) -> dict[str, object]:
        if not ddgs_healthy:
            raise DdgsSearchError("keyless (ddgs) search failed: rate limited")
        return _fake_ddgs_result(str(kwargs["query"]))

    monkeypatch.setattr(web_search_module, "ddgs_search", _fake_ddgs_search)

    first = web_search(
        query="first query",
        cfg=_configured_cfg(),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
        session_id="session-recovering",
    )
    assert first["backend"] == "ddgs"
    assert len(native_calls) == 1

    # The remembered fallback breaks while the native backend has recovered:
    # the preference is dropped and the native backend serves the call again.
    ddgs_healthy = False
    native_healthy = True
    second = web_search(
        query="second query",
        cfg=_configured_cfg(),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
        session_id="session-recovering",
    )
    assert second["backend"] == "openai_responses"
    assert len(native_calls) == 2

    web_search_module._reset_web_search_session_state_for_tests()


def test_web_search_auto_fallback_preference_is_dropped_when_config_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixing the web-search configuration mid-session must take effect: the
    remembered fallback shadows only the exact native runtime that failed."""

    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_KEYLESS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    web_search_module._reset_web_search_session_state_for_tests()

    native_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        native_calls.append(str(request.url.path))
        if request.url.path.startswith("/v1"):
            return httpx.Response(400, json={"error": {"message": "native backend down"}})
        return httpx.Response(
            200,
            json={
                "id": "resp_fixed_endpoint",
                "model": "better-model",
                "output_text": "Native answer from the fixed endpoint.",
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "sources": [{"url": "https://docs.example.com/fixed", "title": "F"}]
                        },
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Native answer from the fixed endpoint.",
                            }
                        ],
                    },
                ],
            },
        )

    monkeypatch.setattr(
        web_search_module,
        "ddgs_search",
        lambda **kwargs: _fake_ddgs_result(str(kwargs["query"])),
    )

    first = web_search(
        query="first query",
        cfg=_configured_cfg(),
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
        session_id="session-config-fix",
    )
    assert first["backend"] == "ddgs"
    assert native_calls == ["/v1/responses"]

    # The user reconfigures the endpoint in the same session: the remembered
    # fallback no longer applies, so the fixed native backend serves the call.
    fixed_cfg = AppConfig(
        model="better-model",
        base_url="https://api.openai.com/v2",
        web_search_mode="auto",
        web_search_base_url="https://api.openai.com/v2",
    )
    second = web_search(
        query="second query",
        cfg=fixed_cfg,
        api_key="main-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
        session_id="session-config-fix",
    )
    assert second["backend"] == "openai_responses"
    assert len(native_calls) == 2

    web_search_module._reset_web_search_session_state_for_tests()


def test_web_search_native_mode_failure_names_the_config_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(str(request.url.host))
        if request.url.host != "api.openai.com":
            raise AssertionError("native mode must not call external backends")
        return httpx.Response(400, json={"error": {"message": "gateway rejected the request"}})

    with pytest.raises(WebSearchError) as excinfo:
        web_search(
            query="failing docs",
            cfg=_configured_cfg(mode="native"),
            api_key="main-key",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
        )

    assert seen_hosts == ["api.openai.com"]
    message = str(excinfo.value)
    assert "web_search_mode" in message
    assert "'auto' or 'external'" in message
    assert "gateway rejected the request" in message
