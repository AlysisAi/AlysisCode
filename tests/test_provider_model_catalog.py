from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

import alysis_code.provider_model_catalog as catalog_mod
from alysis_code.llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
)
from alysis_code.profile_presets import (
    PROFILE_PRESETS,
    make_profile_from_preset,
    profile_provider_family,
)
from alysis_code.profiles import ProfileSpec
from alysis_code.provider_model_catalog import (
    ProviderModelCatalogError,
    ProviderModelOption,
    discover_provider_models,
    provider_model_catalog_strategy,
)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_gemini_catalog_paginates_filters_and_normalizes_models() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        token = request.url.params.get("pageToken")
        if not token:
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-next-001",
                            "baseModelId": "gemini-next",
                            "displayName": "Gemini Next",
                            "description": "Fast routing model",
                            "supportedGenerationMethods": ["generateContent", "countTokens"],
                        },
                        {
                            "name": "models/gemini-embedding-001",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ],
                    "nextPageToken": "page-2",
                },
            )
        assert token == "page-2"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-next",
                        "baseModelId": "gemini-next",
                        "displayName": "duplicate",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemma-router",
                        "displayName": "Gemma Router",
                        "supportedGenerationMethods": ["GENERATECONTENT"],
                    },
                ]
            },
        )

    profile = ProfileSpec(
        name="gemini-compat",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        extra_headers={"X-Custom-Account": "team-a"},
    )
    models = discover_provider_models(
        profile=profile,
        api_key="gemini-secret",
        transport=_transport(handler),
    )

    assert models == (
        ProviderModelOption(
            id="gemini-next",
            label="Gemini Next",
            description="Fast routing model",
            chat_compatibility="chat",
        ),
        ProviderModelOption(
            id="gemma-router",
            label="Gemma Router",
            chat_compatibility="chat",
        ),
    )
    assert len(requests) == 2
    assert requests[0].url.path == "/v1beta/models"
    assert requests[0].url.params["pageSize"] == "1000"
    assert "pageToken" not in requests[0].url.params
    assert requests[1].url.params["pageToken"] == "page-2"
    assert requests[0].headers["x-goog-api-key"] == "gemini-secret"
    assert requests[0].headers["x-custom-account"] == "team-a"
    assert "authorization" not in requests[0].headers


def test_anthropic_catalog_uses_native_headers_and_cursor_pagination() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        after_id = request.url.params.get("after_id")
        if not after_id:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "claude-fast",
                            "display_name": "Claude Fast",
                            "max_input_tokens": 200_000,
                            "max_tokens": 16_000,
                        }
                    ],
                    "has_more": True,
                    "last_id": "claude-fast",
                },
            )
        assert after_id == "claude-fast"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-fast", "display_name": "duplicate"},
                    {
                        "id": "claude-strong",
                        "display_name": "Claude Strong",
                        "description": "Best for hard tasks",
                    },
                ],
                "has_more": False,
            },
        )

    profile = ProfileSpec(
        name="anthropic",
        protocol=ANTHROPIC_MESSAGES_PROTOCOL,
        base_url="https://api.anthropic.com/v1/",
        extra_headers={"anthropic-version": "2026-01-01", "X-Workspace": "alpha"},
    )
    models = discover_provider_models(
        profile=profile,
        api_key="anthropic-secret",
        transport=_transport(handler),
    )

    assert models == (
        ProviderModelOption(
            id="claude-fast",
            label="Claude Fast",
            description="200,000 input tokens · 16,000 max output tokens",
            chat_compatibility="chat",
        ),
        ProviderModelOption(
            id="claude-strong",
            label="Claude Strong",
            description="Best for hard tasks",
            chat_compatibility="chat",
        ),
    )
    assert len(requests) == 2
    assert requests[0].url.path == "/v1/models"
    assert requests[0].url.params["limit"] == "1000"
    assert "after_id" not in requests[0].url.params
    assert requests[1].url.params["after_id"] == "claude-fast"
    assert requests[0].headers["x-api-key"] == "anthropic-secret"
    assert requests[0].headers["anthropic-version"] == "2026-01-01"
    assert requests[0].headers["x-workspace"] == "alpha"


def test_openai_style_catalog_classifies_capabilities_without_name_guessing() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "chat-a", "display_name": "Chat A", "description": "quick"},
                    {"id": "chat-a", "display_name": "duplicate"},
                    {"id": "embed-a", "mode": "embedding"},
                    {
                        "id": "vision-output",
                        "architecture": {"output_modalities": ["image"]},
                    },
                    {"id": "gpt-image-1"},
                    {
                        "id": "gpt-image-chat",
                        "supported_endpoints": ["/v1/chat/completions"],
                    },
                    {
                        "id": "images-only",
                        "supported_endpoints": ["/v1/images/generations"],
                    },
                    {
                        "id": "explicit-no-chat",
                        "capabilities": {"chat": {"supported": False}},
                    },
                    {
                        "id": "neutral-capability-id",
                        "capabilities": {"embeddings": {"supported": True}},
                    },
                    {
                        "id": "legacy-completion",
                        "supported_endpoints": ["/v1/completions"],
                    },
                    {"id": "unknown-new-model"},
                ]
            },
        )

    profile = ProfileSpec(
        name="openrouter",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://openrouter.example/api/v1/",
        extra_headers={
            "authorization": "Bearer profile-override",
            "HTTP-Referer": "https://alysis.example",
        },
    )
    models = discover_provider_models(
        profile=profile,
        api_key="generic-secret",
        transport=_transport(handler),
    )

    assert models == (
        ProviderModelOption(id="chat-a", label="Chat A", description="quick"),
        ProviderModelOption(id="gpt-image-1", label="gpt-image-1"),
        ProviderModelOption(
            id="gpt-image-chat",
            label="gpt-image-chat",
            chat_compatibility="chat",
        ),
        ProviderModelOption(id="unknown-new-model", label="unknown-new-model"),
    )
    assert models[0].chat_compatibility == "unknown"
    assert models[1].chat_compatibility == "unknown"
    assert models[2].chat_compatibility == "chat"
    assert models[3].chat_compatibility == "unknown"
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/models"
    assert requests[0].headers.get_list("authorization") == ["Bearer profile-override"]
    assert requests[0].headers["http-referer"] == "https://alysis.example"


def test_openai_style_catalog_accepts_a_root_list_and_public_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json=[
                "local-chat",
                "whisper-1",
                {"model": "local-second", "displayName": "Local Second"},
            ],
        )

    profile = ProfileSpec(
        name="local",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="http://localhost:8000/v1",
    )
    models = discover_provider_models(
        profile=profile,
        api_key=None,
        transport=_transport(handler),
    )

    assert models == (
        ProviderModelOption(id="local-chat", label="local-chat"),
        ProviderModelOption(id="whisper-1", label="whisper-1"),
        ProviderModelOption(id="local-second", label="Local Second"),
    )


@pytest.mark.parametrize(
    "profile",
    [
        ProfileSpec(
            name="gemini",
            protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
        ),
        ProfileSpec(
            name="anthropic",
            protocol=ANTHROPIC_MESSAGES_PROTOCOL,
            base_url="https://api.anthropic.com/v1",
        ),
    ],
)
def test_native_catalogs_require_an_api_key(profile: ProfileSpec) -> None:
    with pytest.raises(ProviderModelCatalogError, match="API key is required"):
        discover_provider_models(
            profile=profile,
            api_key="",
            transport=_transport(lambda _request: pytest.fail("must not request")),
        )


@pytest.mark.parametrize("status_code", [301, 302, 401, 403, 429, 500])
def test_http_errors_are_sanitized_and_redirects_are_not_followed(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code,
            headers={"location": "https://other.example/models"},
            text="provider echoed super-secret and raw diagnostics",
        )

    profile = ProfileSpec(
        name="openai",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://provider.example/v1",
    )
    with pytest.raises(ProviderModelCatalogError) as raised:
        discover_provider_models(
            profile=profile,
            api_key="super-secret",
            transport=_transport(handler),
        )

    assert len(requests) == 1
    assert f"HTTP {status_code}" in str(raised.value)
    assert "super-secret" not in str(raised.value)
    assert "raw diagnostics" not in str(raised.value)


def test_transport_and_malformed_json_errors_do_not_leak_provider_details() -> None:
    profile = ProfileSpec(
        name="openai",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://provider.example/v1",
    )

    def explode(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("super-secret leaked by transport")

    with pytest.raises(ProviderModelCatalogError) as transport_error:
        discover_provider_models(
            profile=profile,
            api_key="super-secret",
            transport=_transport(explode),
        )
    assert "RuntimeError" in str(transport_error.value)
    assert "super-secret" not in str(transport_error.value)

    with pytest.raises(ProviderModelCatalogError, match="malformed JSON") as json_error:
        discover_provider_models(
            profile=profile,
            api_key="super-secret",
            transport=_transport(
                lambda _request: httpx.Response(200, text="super-secret is not JSON")
            ),
        )
    assert "super-secret" not in str(json_error.value)


def test_pagination_repetition_is_bounded() -> None:
    gemini = ProfileSpec(
        name="gemini",
        protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    gemini_calls = 0

    def gemini_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal gemini_calls
        gemini_calls += 1
        return httpx.Response(200, json={"models": [], "nextPageToken": "same"})

    with pytest.raises(ProviderModelCatalogError, match="repeated Gemini page token"):
        discover_provider_models(
            profile=gemini,
            api_key="key",
            transport=_transport(gemini_handler),
        )
    assert gemini_calls == 2

    anthropic = ProfileSpec(
        name="anthropic",
        protocol=ANTHROPIC_MESSAGES_PROTOCOL,
        base_url="https://api.anthropic.com/v1",
    )
    anthropic_calls = 0

    def anthropic_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal anthropic_calls
        anthropic_calls += 1
        return httpx.Response(
            200,
            json={"data": [], "has_more": True, "last_id": "same"},
        )

    with pytest.raises(ProviderModelCatalogError, match="repeated Anthropic cursor"):
        discover_provider_models(
            profile=anthropic,
            api_key="key",
            transport=_transport(anthropic_handler),
        )
    assert anthropic_calls == 2


def test_page_model_and_response_size_limits_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gemini = ProfileSpec(
        name="gemini",
        protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    monkeypatch.setattr(catalog_mod, "_MAX_PAGES", 1)
    page_calls = 0

    def endless_pages(_request: httpx.Request) -> httpx.Response:
        nonlocal page_calls
        page_calls += 1
        return httpx.Response(200, json={"models": [], "nextPageToken": "more"})

    with pytest.raises(ProviderModelCatalogError, match="Gemini page limit"):
        discover_provider_models(
            profile=gemini,
            api_key="key",
            transport=_transport(endless_pages),
        )
    assert page_calls == 1

    generic = ProfileSpec(
        name="gateway",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://gateway.example/v1",
    )
    monkeypatch.setattr(catalog_mod, "_MAX_MODELS", 1)
    with pytest.raises(ProviderModelCatalogError, match="model limit"):
        discover_provider_models(
            profile=generic,
            api_key="key",
            transport=_transport(
                lambda _request: httpx.Response(
                    200,
                    json={"data": [{"id": "chat-a"}, {"id": "chat-b"}]},
                )
            ),
        )

    monkeypatch.setattr(catalog_mod, "_MAX_RESPONSE_BYTES", 10)
    with pytest.raises(ProviderModelCatalogError, match="response was too large"):
        discover_provider_models(
            profile=generic,
            api_key="key",
            transport=_transport(
                lambda _request: httpx.Response(200, json={"data": [{"id": "chat-a"}]})
            ),
        )


def test_invalid_timeout_and_missing_base_url_fail_before_network() -> None:
    handler = lambda _request: pytest.fail("must not request")  # noqa: E731
    missing_url = ProfileSpec(name="custom", protocol=OPENAI_COMPAT_PROTOCOL)
    with pytest.raises(ProviderModelCatalogError, match="URL is not configured"):
        discover_provider_models(
            profile=missing_url,
            api_key=None,
            transport=_transport(handler),
        )

    profile = ProfileSpec(
        name="openai",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://provider.example/v1",
    )
    for invalid in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ProviderModelCatalogError, match="timeout must be positive"):
            discover_provider_models(
                profile=profile,
                api_key="key",
                timeout_s=invalid,
                transport=_transport(handler),
            )


def test_every_profile_preset_has_a_stable_catalog_strategy() -> None:
    observed: dict[str, str] = {}
    for preset in PROFILE_PRESETS:
        profile = make_profile_from_preset(preset)
        strategy = provider_model_catalog_strategy(profile)
        observed[preset.key] = strategy
        assert strategy in {"gemini", "anthropic", "openai"}
        family = profile_provider_family(profile)
        if family == "gemini":
            assert strategy == "gemini"
        elif family == "anthropic":
            assert strategy == "anthropic"
        else:
            assert strategy == "openai"

    assert observed["gemini"] == "gemini"
    assert observed["gemini-compat"] == "gemini"
    assert observed["gemini-native"] == "gemini"
    assert observed["anthropic"] == "anthropic"
    assert observed["anthropic-compat"] == "anthropic"
    assert observed["anthropic-native"] == "anthropic"
    assert observed["openai"] == "openai"
    assert observed["openai-responses"] == "openai"


@pytest.mark.parametrize("name", ["gemini-proxy", "claude", "anthropic-gateway"])
def test_custom_compat_gateway_name_does_not_select_a_first_party_strategy(name: str) -> None:
    profile = ProfileSpec(
        name=name,
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://gateway.example/v1",
    )

    assert provider_model_catalog_strategy(profile) == "openai"


def test_catalog_option_is_frozen() -> None:
    option = ProviderModelOption(id="model", label="Model")
    with pytest.raises((AttributeError, TypeError)):
        option.id = "changed"  # type: ignore[misc]


def test_extra_headers_are_applied_case_insensitively() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get_list("authorization")
        seen["agent"] = request.headers["x-agent"]
        return httpx.Response(200, json={"data": []})

    profile = ProfileSpec(
        name="gateway",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://gateway.example/v1",
        extra_headers={"authorization": "Token custom", "X-Agent": "router"},
    )
    assert (
        discover_provider_models(
            profile=profile,
            api_key="default-secret",
            transport=_transport(handler),
        )
        == ()
    )
    assert seen == {"authorization": ["Token custom"], "agent": "router"}


def test_catalog_metadata_is_terminal_safe_and_model_ids_are_bounded() -> None:
    profile = ProfileSpec(
        name="gateway",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://gateway.example/v1",
    )

    options = discover_provider_models(
        profile=profile,
        api_key="key",
        transport=_transport(
            lambda _request: httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "safe\x1b[31m-model",
                            "display_name": "\x1b[31mOwned\x1b[0m\x07\x85Name",
                            "description": "hello\x00world",
                        },
                        {"id": "x" * (catalog_mod._MAX_MODEL_ID_LENGTH + 1)},
                    ]
                },
            )
        ),
    )

    assert options == (
        ProviderModelOption(
            id="safe-model",
            label="OwnedName",
            description="helloworld",
        ),
    )


def test_response_bound_is_enforced_for_declared_and_chunked_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProfileSpec(
        name="gateway",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://gateway.example/v1",
    )
    monkeypatch.setattr(catalog_mod, "_MAX_RESPONSE_BYTES", 10)

    class NeverRead(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            pytest.fail("oversized declared body must not be read")
            yield b""

    with pytest.raises(ProviderModelCatalogError, match="response was too large"):
        discover_provider_models(
            profile=profile,
            api_key="key",
            transport=_transport(
                lambda _request: httpx.Response(
                    200,
                    headers={"content-length": "11"},
                    stream=NeverRead(),
                )
            ),
        )

    class ChunkedBody(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield b"123456"
            yield b"789012"

    with pytest.raises(ProviderModelCatalogError, match="response was too large"):
        discover_provider_models(
            profile=profile,
            api_key="key",
            transport=_transport(lambda _request: httpx.Response(200, stream=ChunkedBody())),
        )


@pytest.mark.parametrize(
    "metadata,expected",
    [
        (
            {"supported_efforts": None, "mandatory": True},
            ("minimal", "low", "medium", "high", "xhigh", "max"),
        ),
        ({"supported_efforts": ["none", "low", "low", "high"], "mandatory": True}, ("low", "high")),
        ({"supported_efforts": [], "mandatory": False}, ()),
        ({"mandatory": False}, ()),
        ({"supported_efforts": "high", "mandatory": False}, None),
        ({"supported_efforts": [False], "mandatory": False}, None),
        (None, None),
    ],
)
def test_openrouter_preserves_capabilities_without_inventing_efforts(metadata, expected):
    from alysis_code.profile_presets import PRESET_BY_KEY

    profile = make_profile_from_preset(PRESET_BY_KEY["openrouter"])
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"data": [{"id": "vendor/new-model", "reasoning": metadata}]}
        )
    )
    option = discover_provider_models(profile=profile, api_key=None, transport=transport)[0]
    if expected is None:
        assert option.reasoning_contract is None
    else:
        assert option.reasoning_contract.values == expected


def test_custom_gateway_does_not_inherit_openrouter_metadata():
    profile = ProfileSpec(
        name="openrouter", base_url="https://gateway.test/v1", protocol="openai_compat"
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vendor/model",
                        "reasoning": {"supported_efforts": ["high"], "mandatory": False},
                    }
                ]
            },
        )
    )
    option = discover_provider_models(profile=profile, api_key=None, transport=transport)[0]
    assert option.reasoning_contract is None
