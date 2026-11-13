from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code.error_text import sanitize_error_text_for_output
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.gemini_interactions import GeminiInteractionsClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError
from alysis_code.llm_error_display import friendly_llm_error_message
from alysis_code.profiles import validate_base_url
from alysis_code.session_store import SessionStore

ClientFactory = Callable[[str, httpx.BaseTransport], Any]


def _client_kwargs(base_url: str, transport: httpx.BaseTransport) -> dict[str, Any]:
    return {
        "base_url": base_url,
        "api_key": "test-key",
        "model": "test-model",
        "transport": transport,
        "provider_retry_settings": ProviderRetrySettings(max_retries=0),
    }


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda base_url, transport: OpenAICompatClient(**_client_kwargs(base_url, transport)),
            id="openai-compat",
        ),
        pytest.param(
            lambda base_url, transport: OpenAIResponsesClient(
                **_client_kwargs(base_url, transport)
            ),
            id="openai-responses",
        ),
        pytest.param(
            lambda base_url, transport: AnthropicMessagesClient(
                **_client_kwargs(base_url, transport)
            ),
            id="anthropic-messages",
        ),
        pytest.param(
            lambda base_url, transport: GeminiGenerateContentClient(
                **_client_kwargs(base_url, transport)
            ),
            id="gemini-generate-content",
        ),
        pytest.param(
            lambda base_url, transport: GeminiInteractionsClient(
                **_client_kwargs(base_url, transport)
            ),
            id="gemini-interactions",
        ),
    ],
)
@pytest.mark.parametrize("failure_kind", ["transport", "status_body"])
def test_provider_http_error_url_secrets_never_reach_source_log_or_display(
    factory: ClientFactory,
    failure_kind: str,
    tmp_path: Path,
) -> None:
    sentinel = "PRIVATE_PROVIDER_URL_SENTINEL"
    bearer_secret = "BEARER_PRIVATE_SENTINEL_123456+/="
    api_key_secret = "API_KEY_PRIVATE_SENTINEL_123456"
    secret_base_url = (
        f"https://route-user:route-pa'ssword@api.example.test:8443/secret-route-segment<{sentinel}"
    )
    assert validate_base_url(secret_base_url, key="base_url") == secret_base_url
    secret_error_url = f"{secret_base_url}?token={sentinel}#{sentinel}"
    provider_error = (
        f"connection failed for {secret_error_url} "
        f"api_key='{api_key_secret}' Authorization: Bearer {bearer_secret}"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if failure_kind == "transport":
            raise httpx.ConnectError(provider_error, request=request)
        return httpx.Response(
            400,
            json={"error": {"message": provider_error}},
            request=request,
        )

    client = factory(secret_base_url, httpx.MockTransport(handler))
    with pytest.raises(LLMError) as exc_info:
        client.chat(messages=[{"role": "user", "content": "hello"}], stream=False)

    error_text = str(exc_info.value)
    display_text = friendly_llm_error_message(exc_info.value)
    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path,
        session_id="provider-url-privacy",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    try:
        store.append("error", {"error": sanitize_error_text_for_output(exc_info.value)})
    finally:
        store.close()
    source_log = (tmp_path / "provider-url-privacy.jsonl").read_text(encoding="utf-8")

    for rendered in (
        error_text,
        display_text,
        source_log,
        getattr(exc_info.value, "provider_error_body", ""),
    ):
        assert sentinel not in rendered
        assert bearer_secret not in rendered
        assert api_key_secret not in rendered
        assert "route-user" not in rendered
        assert "route-pa'ssword" not in rendered
        assert "secret-route-segment" not in rendered
        assert "token=" not in rendered
    parsed_event = json.loads(source_log)
    assert "api.example.test" in parsed_event["payload"]["error"]
