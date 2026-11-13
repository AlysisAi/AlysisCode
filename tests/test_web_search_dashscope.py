from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.tools.web_search_dashscope import (
    DashScopeChatSearchError,
    _sources_from_urls,
    dashscope_chat_search,
    dashscope_model_supports_web_search,
    dashscope_model_uses_responses_search,
)


def _public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def test_dashscope_chat_search_posts_enable_search_and_returns_sources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://coding-intl.dashscope.aliyuncs.com/v1/chat/completions"
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "qwen3.5-plus"
        assert body["enable_search"] is True
        assert body["search_options"] == {
            "forced_search": True,
            "search_strategy": "agent",
            "enable_source": True,
        }
        assert body["enable_thinking"] is False
        assert body["stream"] is True
        prompt = body["messages"][0]["content"]
        assert "Use live web search" in prompt
        assert "docs.example.com" in prompt
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_dashscope_1",
                "model": "qwen3.5-plus",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "Read the official docs.",
                                    "sources": [
                                        {
                                            "title": "Docs",
                                            "url": "https://docs.example.com/start",
                                            "content": "Docs snippet",
                                        },
                                        {
                                            "title": "Duplicate",
                                            "url": "https://docs.example.com/start",
                                        },
                                        {
                                            "title": "Filtered",
                                            "url": "https://other.example.com/start",
                                        },
                                    ],
                                }
                            )
                        }
                    }
                ],
            },
        )

    result = dashscope_chat_search(
        query="docs example",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key="dashscope-key",
        model="qwen3.5-plus",
        include_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "dashscope_chat"
    assert result["answer"] == "Read the official docs."
    assert result["model"] == "qwen3.5-plus"
    assert result["response_id"] == "chatcmpl_dashscope_1"
    assert result["allowed_domains"] == ["docs.example.com"]
    assert result["sources"] == [
        {
            "url": "https://docs.example.com/start",
            "title": "Docs",
            "snippet": "Docs snippet",
        }
    ]
    assert result["citations"] == [
        {
            "title": "Docs",
            "url": "https://docs.example.com/start",
            "start_index": None,
            "end_index": None,
        }
    ]


@pytest.mark.parametrize(
    "model",
    ["qwen3.7-plus", "qwen3.7-max", "qwen3.6-plus", "qwen3.6-flash"],
)
def test_current_qwen_models_use_responses_web_search(model: str) -> None:
    assert dashscope_model_supports_web_search(model) is True
    assert dashscope_model_uses_responses_search(model) is True


def test_dashscope_responses_search_supports_qwen37_sources_and_queries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            str(request.url) == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/responses"
        )
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "qwen3.7-plus"
        assert body["tools"] == [{"type": "web_search"}]
        assert body["stream"] is False
        assert "docs.example.com" in body["input"]
        return httpx.Response(
            200,
            json={
                "id": "resp_qwen37_1",
                "model": "qwen3.7-plus",
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "query": "Qwen web search docs",
                            "sources": [
                                {
                                    "title": "Qwen Docs",
                                    "url": "https://docs.example.com/qwen",
                                }
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Use the current Qwen documentation.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://docs.example.com/qwen",
                                        "title": "Qwen Docs",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
        )

    result = dashscope_chat_search(
        query="current Qwen web search",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="dashscope-key",
        model="qwen3.7-plus",
        include_domains=["docs.example.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "dashscope_responses"
    assert result["answer"] == "Use the current Qwen documentation."
    assert result["queries"] == ["Qwen web search docs"]
    assert result["sources"] == [{"url": "https://docs.example.com/qwen", "title": "Qwen Docs"}]


def test_dashscope_sources_from_urls_preserves_parenthesized_path() -> None:
    assert _sources_from_urls("See https://docs.example.com/Function_(mathematics) for more.") == [
        {
            "url": "https://docs.example.com/Function_(mathematics)",
            "normalized_url": "https://docs.example.com/Function_(mathematics)",
            "title": "",
        }
    ]


def test_dashscope_sources_from_urls_parses_parenthesized_markdown_link_target() -> None:
    assert _sources_from_urls("See ([docs](https://docs.example.com/spec)) for more.") == [
        {
            "url": "https://docs.example.com/spec",
            "normalized_url": "https://docs.example.com/spec",
            "title": "",
        }
    ]


def test_dashscope_sources_from_urls_parses_markdown_link_title_parenthesized_target() -> None:
    assert _sources_from_urls(
        'See [Function](https://docs.example.com/Function_(mathematics) "Function docs") for more.'
    ) == [
        {
            "url": "https://docs.example.com/Function_(mathematics)",
            "normalized_url": "https://docs.example.com/Function_(mathematics)",
            "title": "",
        }
    ]


def test_dashscope_sources_from_urls_preserves_structured_markdown_target_punctuation() -> None:
    assert _sources_from_urls(
        "See [docs](https://docs.example.com/path.) and <https://docs.example.com/path:>."
    ) == [
        {
            "url": "https://docs.example.com/path.",
            "normalized_url": "https://docs.example.com/path.",
            "title": "",
        },
        {
            "url": "https://docs.example.com/path:",
            "normalized_url": "https://docs.example.com/path:",
            "title": "",
        },
    ]


def test_dashscope_sources_from_urls_preserves_bracket_query_params() -> None:
    assert _sources_from_urls("See https://docs.example.com/path?foo[bar]=1 for more.") == [
        {
            "url": "https://docs.example.com/path?foo[bar]=1",
            "normalized_url": "https://docs.example.com/path?foo[bar]=1",
            "title": "",
        }
    ]


def test_dashscope_sources_from_urls_preserves_exclamation_and_apostrophe_urls() -> None:
    assert _sources_from_urls(
        "See https://docs.example.com/Yahoo! and https://docs.example.com/O'Connor."
    ) == [
        {
            "url": "https://docs.example.com/Yahoo!",
            "normalized_url": "https://docs.example.com/Yahoo!",
            "title": "",
        },
        {
            "url": "https://docs.example.com/O'Connor",
            "normalized_url": "https://docs.example.com/O'Connor",
            "title": "",
        },
    ]


def test_dashscope_sources_from_urls_preserves_legal_trailing_parenthesis() -> None:
    assert _sources_from_urls("See https://docs.example.com/path?x=a) for more.") == [
        {
            "url": "https://docs.example.com/path?x=a)",
            "normalized_url": "https://docs.example.com/path?x=a)",
            "title": "",
        }
    ]


@pytest.mark.parametrize(
    ("query", "content", "expected_url"),
    [
        pytest.param(
            "docs markdown link",
            "See ([docs](https://docs.example.com/spec)) for more.",
            "https://docs.example.com/spec",
            id="parenthesized-markdown-link-target",
        ),
        pytest.param(
            "function markdown title",
            'See [Function](https://docs.example.com/Function_(mathematics) "Function docs") for more.',
            "https://docs.example.com/Function_(mathematics)",
            id="markdown-link-title-parenthesized-target",
        ),
        pytest.param(
            "docs bracket query",
            "See https://docs.example.com/path?foo[bar]=1 for more.",
            "https://docs.example.com/path?foo[bar]=1",
            id="bracket-query-url",
        ),
        pytest.param(
            "yahoo docs",
            "See https://docs.example.com/Yahoo! for more.",
            "https://docs.example.com/Yahoo!",
            id="exclamation-url",
        ),
        pytest.param(
            "docs trailing parenthesis",
            "See https://docs.example.com/path?x=a) for more.",
            "https://docs.example.com/path?x=a)",
            id="url-with-legal-trailing-parenthesis",
        ),
        pytest.param(
            "function mathematics docs",
            "See https://docs.example.com/Function_(mathematics) for more.",
            "https://docs.example.com/Function_(mathematics)",
            id="parenthesized-url",
        ),
    ],
)
def test_dashscope_chat_search_extracts_literal_urls_from_plain_answers(
    query: str, content: str, expected_url: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = dashscope_chat_search(
        query=query,
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key="dashscope-key",
        model="qwen3.5-plus",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["sources"] == [{"url": expected_url, "title": ""}]
    assert result["citations"] == [
        {"title": "", "url": expected_url, "start_index": None, "end_index": None}
    ]


def test_dashscope_chat_search_structured_sources_preserve_parenthesized_url() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "Use the function reference.",
                                    "sources": [
                                        {
                                            "title": "Function (mathematics)",
                                            "url": "https://docs.example.com/Function_(mathematics)",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = dashscope_chat_search(
        query="function mathematics docs",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key="dashscope-key",
        model="qwen3.5-plus",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["sources"] == [
        {
            "url": "https://docs.example.com/Function_(mathematics)",
            "title": "Function (mathematics)",
        }
    ]


def test_dashscope_chat_search_plain_answer_canonicalizes_wrappers_without_duplicates() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "Use https://docs.example.com/spec. Then (https://docs.example.com/spec) "
                                'and "https://docs.example.com/spec".'
                            )
                        }
                    }
                ]
            },
        )

    result = dashscope_chat_search(
        query="spec docs",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key="dashscope-key",
        model="qwen3.5-plus",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["sources"] == [{"url": "https://docs.example.com/spec", "title": ""}]


def test_dashscope_chat_search_plain_answer_canonicalizes_markdown_wrappers_without_duplicates() -> (
    None
):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "Use `https://docs.example.com/spec`, *https://docs.example.com/spec*, "
                                "and _https://docs.example.com/spec_."
                            )
                        }
                    }
                ]
            },
        )

    result = dashscope_chat_search(
        query="spec docs",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key="dashscope-key",
        model="qwen3.5-plus",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["sources"] == [{"url": "https://docs.example.com/spec", "title": ""}]


def test_dashscope_chat_search_extracts_urls_from_plain_answer() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "Use the migration guide at https://docs.example.com/migration."
                            )
                        }
                    }
                ]
            },
        )

    result = dashscope_chat_search(
        query="migration guide",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key="dashscope-key",
        model="qwen3.5-plus",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["answer"].startswith("Use the migration guide")
    assert result["sources"] == [{"url": "https://docs.example.com/migration", "title": ""}]


def test_dashscope_chat_search_parses_fenced_json_answer() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```json\n"
                                '{"answer":"Use the official download page.",'
                                '"sources":[{"title":"Download Python",'
                                '"url":"https://www.python.org/downloads/"}]}'
                                "\n```"
                            )
                        }
                    }
                ]
            },
        )

    result = dashscope_chat_search(
        query="python downloads",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key="dashscope-key",
        model="qwen3.5-plus",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["answer"] == "Use the official download page."
    assert result["sources"] == [
        {"url": "https://www.python.org/downloads/", "title": "Download Python"}
    ]


def test_dashscope_chat_search_parses_streaming_sse_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"id":"chatcmpl_stream_1","model":"qwen3.5-plus",'
                '"choices":[{"delta":{"content":"Use "}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"the docs."}}],'
                '"search_info":{"search_results":[{"title":"Docs",'
                '"url":"https://docs.example.com/stream"}]}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    result = dashscope_chat_search(
        query="stream docs",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key="dashscope-key",
        model="qwen3.5-plus",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["answer"] == "Use the docs."
    assert result["response_id"] == "chatcmpl_stream_1"
    assert result["sources"] == [{"url": "https://docs.example.com/stream", "title": "Docs"}]


def test_dashscope_chat_search_surfaces_http_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "invalid_api_key",
                    "message": "invalid access token or token expired",
                }
            },
        )

    with pytest.raises(
        DashScopeChatSearchError,
        match="DashScope chat search error 401: invalid_api_key: invalid access token",
    ):
        dashscope_chat_search(
            query="docs",
            base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
            api_key="bad-key",
            model="qwen3.5-plus",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
            provider_retry_settings=ProviderRetrySettings(max_retries=0),
        )


def test_dashscope_chat_search_surfaces_timeout_with_configured_limit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("The read operation timed out")

    with pytest.raises(
        DashScopeChatSearchError,
        match=r"DashScope chat search timed out during response read .*overall=45s",
    ):
        dashscope_chat_search(
            query="slow search",
            base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
            api_key="dashscope-key",
            model="qwen3.5-plus",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
            provider_retry_settings=ProviderRetrySettings(max_retries=0),
        )
