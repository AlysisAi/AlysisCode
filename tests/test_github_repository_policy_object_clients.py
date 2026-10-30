"""Object-argument provider ingress must not erase duplicate GitHub keys."""

import httpx
import pytest

from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.mcp.github_policy_json import argument_scope


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_duplicate_object_arguments_at_actual_response_parser(protocol):
    raw = '{"page":1,"page":2}'
    if protocol == "anthropic":
        body = (
            '{"id":"msg_fixture","type":"message","role":"assistant","model":"fixture","stop_reason":"tool_use","content":[{"type":"tool_use","id":"call_fixture","name":"github_repositories","input":'
            + raw
            + '}],"usage":{"input_tokens":1,"output_tokens":1}}'
        )

        def parser(r):
            return AnthropicMessagesClient._parse_chat_response(
                response=r, reasoning_is_summary=False
            )
    else:
        body = (
            '{"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":"github_repositories","args":'
            + raw
            + '}}]},"finishReason":"STOP"}]}'
        )
        parser = GeminiGenerateContentClient._parse_chat_response
    with argument_scope(True):
        response = parser(
            httpx.Response(200, content=body, headers={"content-type": "application/json"})
        )
    assert response.tool_calls[0].arguments == {"_invalid_github_arguments": True}
    with argument_scope(False):
        response = parser(
            httpx.Response(200, content=body, headers={"content-type": "application/json"})
        )
    assert response.tool_calls[0].arguments == {"page": 2}


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_stream_duplicate_object_arguments_at_actual_sse_parser(protocol):
    import json

    if protocol == "anthropic":
        events = [
            dict(
                type="message_start",
                message=dict(
                    id="m",
                    type="message",
                    role="assistant",
                    model="fixture",
                    content=[],
                    usage=dict(input_tokens=1, output_tokens=0),
                ),
            ),
            dict(
                type="content_block_start",
                index=0,
                content_block=dict(
                    type="tool_use", id="call_fixture", name="github_repositories", input={}
                ),
            ),
            dict(
                type="content_block_delta",
                index=0,
                delta=dict(type="input_json_delta", partial_json='{"page":1,"page":2}'),
            ),
            dict(type="content_block_stop", index=0),
            dict(
                type="message_delta",
                delta=dict(stop_reason="tool_use"),
                usage=dict(output_tokens=1),
            ),
            dict(type="message_stop"),
        ]
        raw = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events)
        parser = AnthropicMessagesClient._parse_stream_response
    else:
        raw = 'data: {"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":"github_repositories","args":{"page":1,"page":2}}}]},"finishReason":"STOP"}]}\n\n'
        parser = GeminiGenerateContentClient._parse_stream_response
    with argument_scope(True):
        result = parser(
            httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"}),
            on_text_delta=None,
            on_reasoning_delta=None,
            reasoning_is_summary=False,
        )
    assert result.tool_calls[0].arguments == {"_invalid_github_arguments": True}


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_unrelated_tool_nested_data_is_not_github_ingress(protocol):
    import json

    from alysis_code.mcp.github_policy_json import provider_payload

    nested = '{"name":"github_repositories","args":{"page":1,"page":2},"type":"tool_use","input":{"page":1,"page":2}}'
    raw = '{"unrelated_tool_data":' + nested + "}"
    with argument_scope(True):
        assert provider_payload(raw, protocol=protocol) == json.loads(raw)
