from __future__ import annotations

import pytest

from alysis_code.mcp.resources import (
    McpReadResourceContent,
    McpResourceNormalizationError,
    normalize_list_resources_result,
    normalize_read_resource_result,
    resources_runtime_supported,
)
from alysis_code.mcp.untrusted_content import build_untrusted_mcp_text_block
from alysis_code.runtime_kind import RuntimeKind


@pytest.mark.parametrize(
    ("runtime_kind", "expected_supported"),
    [
        (RuntimeKind.INTERACTIVE_CHAT, True),
        (RuntimeKind.ONE_SHOT, True),
        (RuntimeKind.SUBAGENT, False),
    ],
)
def test_resources_runtime_supported_only_in_allowed_runtimes(
    runtime_kind: RuntimeKind, expected_supported: bool
) -> None:
    assert resources_runtime_supported(runtime_kind) is expected_supported


def test_normalize_list_resources_result_preserves_order_and_stable_fields() -> None:
    resources, next_cursor = normalize_list_resources_result(
        {
            "resources": [
                {
                    "uri": "file:///alpha.txt",
                    "name": "alpha",
                    "description": "Alpha text",
                    "mimeType": "text/plain",
                    "size": 12,
                    "ignored": True,
                },
                {
                    "uri": "https://example.com/spec.json",
                    "name": "spec",
                    "mimeType": "application/json",
                },
            ],
            "nextCursor": "page:1",
        }
    )

    assert [resource.uri for resource in resources] == [
        "file:///alpha.txt",
        "https://example.com/spec.json",
    ]
    assert resources[0].description == "Alpha text"
    assert resources[0].mime_type == "text/plain"
    assert resources[0].size == 12
    assert resources[1].description is None
    assert next_cursor == "page:1"


def test_normalize_read_resource_result_surfaces_safe_text() -> None:
    result = normalize_read_resource_result(
        {
            "contents": [
                {
                    "uri": "file:///alpha.txt",
                    "mimeType": "text/plain",
                    "text": "hello world",
                }
            ]
        },
        expected_uri="file:///alpha.txt",
    )

    assert result.text == "hello world"
    assert result.mime_type == "text/plain"
    assert result.contents[0].text == "hello world"
    assert "text/plain" in result.content_summary
    assert "text(" in result.content_summary


def test_normalize_read_resource_result_surfaces_json_text_safely() -> None:
    raw_json = '{"ok":true,"items":[1,2]}'
    result = normalize_read_resource_result(
        {
            "contents": [
                {
                    "uri": "https://example.com/spec.json",
                    "mimeType": "application/json",
                    "text": raw_json,
                }
            ]
        },
        expected_uri="https://example.com/spec.json",
    )

    assert result.text == raw_json
    assert result.contents[0].text == raw_json
    assert "application/json" in result.content_summary
    assert "json text" in result.content_summary


def test_mcp_read_resource_content_payload_wraps_untrusted_text_with_provenance() -> None:
    content = McpReadResourceContent(
        uri="file:///alpha.txt",
        mime_type="text/plain",
        content_summary="text/plain text(11 chars)",
        text="hello world",
    )

    payload = content.as_tool_payload(server_id="alpha", resource_uri="file:///alpha.txt")

    assert payload["text"] == build_untrusted_mcp_text_block(
        source_type="resource_read",
        server_id="alpha",
        source_name="file:///alpha.txt",
        text="hello world",
        mime_type="text/plain",
    )


def test_normalize_read_resource_result_summarizes_binary_blob_without_inlining() -> None:
    result = normalize_read_resource_result(
        {
            "contents": [
                {
                    "uri": "file:///image.bin",
                    "mimeType": "application/octet-stream",
                    "blob": "QUJDREVGRw==",
                }
            ]
        },
        expected_uri="file:///image.bin",
    )

    assert result.text == ""
    assert result.contents[0].text is None
    assert "blob omitted" in result.content_summary
    assert "application/octet-stream" in result.content_summary


def test_normalize_read_resource_result_rejects_mismatched_uri() -> None:
    with pytest.raises(McpResourceNormalizationError) as exc_info:
        normalize_read_resource_result(
            {
                "contents": [
                    {
                        "uri": "file:///other.txt",
                        "mimeType": "text/plain",
                        "text": "nope",
                    }
                ]
            },
            expected_uri="file:///alpha.txt",
        )
    assert "must match the requested resource URI" in str(exc_info.value)
