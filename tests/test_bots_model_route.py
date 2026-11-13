"""Owls pick their model client by the same per-model route as the desktop agent."""

from types import SimpleNamespace

import pytest

from alysis_code.bots.app import OWLS_CLIENTS, owls_protocol
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient


@pytest.mark.parametrize(
    ("provider", "model", "protocol", "client"),
    [
        # Signed-in owls on hosted OpenAI models must use Responses (the endpoint refuses tools
        # on chat completions: "Hosted OpenAI models use /v1/responses for reasoning and tools").
        ("alysis", "gpt-6-luna", "openai_compat", OpenAIResponsesClient),
        ("alysis", "claude-sonnet-5-5", "openai_compat", AnthropicMessagesClient),
        ("alysis", "glm-5.3-flash", "openai_compat", OpenAICompatClient),
        ("openai", "gpt-6.1-sol", "openai_compat", OpenAIResponsesClient),
        ("anthropic", "claude-opus-5", "anthropic_messages", AnthropicMessagesClient),
        ("openrouter", "some/model", "openai_compat", OpenAICompatClient),
    ],
)
def test_owls_use_the_agents_model_route(provider, model, protocol, client):
    settings = SimpleNamespace(provider_key=provider, model=model, protocol=protocol)
    assert OWLS_CLIENTS.get(owls_protocol(settings), OpenAICompatClient) is client
