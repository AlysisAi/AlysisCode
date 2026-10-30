"""Local preflight for the hosted Chat Completions envelope.

Keep these bounds in sync with the hosted API admission contract. They measure
gateway admission pressure, not actual token usage or billable tokens.
"""

from __future__ import annotations

import json
from typing import Any

from .types import LLMError

MAX_BODY_BYTES = 8 * 1024 * 1024
_FLASH_ALIASES = {
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4.1-flash-expires-on-0910",
}
_CONTROL_FIELDS = (
    "thinking",
    "reasoning_effort",
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "response_format",
    "tool_choice",
    "logprobs",
    "top_logprobs",
)


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )


def hosted_chat_input_bound(payload: dict[str, Any]) -> int:
    model = payload.get("model")
    if model in _FLASH_ALIASES:
        model = "deepseek-flash"
    images = 0
    messages = []
    for raw in payload.get("messages", []):
        message = {
            key: raw[key]
            for key in (
                "role",
                "content",
                "name",
                "tool_call_id",
                "reasoning_content",
                "prefix",
                "tool_calls",
            )
            if key in raw
        }
        if isinstance(message.get("content"), list):
            content = []
            for part in message["content"]:
                if part.get("type") == "text":
                    content.append({"type": "text", "text": part["text"]})
                else:
                    images += 1
                    content.append({"type": "text", "text": "[image]"})
            message["content"] = content
        messages.append(message)
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": payload.get("max_tokens", payload.get("max_completion_tokens", 8192)),
        "stream": payload.get("stream") is True,
    }
    for key in (*_CONTROL_FIELDS, "tools"):
        if key in payload:
            body[key] = payload[key]
    if body["stream"]:
        body["stream_options"] = {"include_usage": True}
    if model == "glm-5.3-flash":
        body["thinking"] = {"type": "enabled", "clear_thinking": False}
        body["reasoning_effort"] = payload.get("reasoning_effort", "max")
        if body["stream"]:
            body["tool_stream"] = True
    return (
        _json_bytes(body) * 2
        + 4096
        + len(messages) * 256
        + len(body.get("tools", [])) * 1024
        + images * 1280
    )


_INPUT_LIMITS = {
    "glm-5.3-flash": 1000000,
    "deepseek-v4.1-flash": 1000000,
    "deepseek-v4-pro": 1048576,
    "mistral-large-4": 1000000,
    "minimax-m3.1-flash": 1000000,
    "qwen3.8-flash": 1000000,
    "reflection-beam": 1000000,
    "nemotron-3.5-super-vl": 262144,
    "glm-5.3": 1000000,
    "qwen3.8-2.4t": 1000000,
    "kimi-k3": 1000000,
}


def _input_limit(payload: dict[str, Any]) -> int:
    return _INPUT_LIMITS.get(str(payload.get("model") or ""), 1_048_576)


def hosted_chat_capacity_ratio(payload: dict[str, Any], *, headroom: int = 0) -> float:
    return max(
        (hosted_chat_input_bound(payload) + headroom) / (_input_limit(payload) - 1),
        (_json_bytes(payload) + headroom) / MAX_BODY_BYTES,
        len(payload.get("messages", [])) / 2048,
    )


def validate_hosted_chat_capacity(payload: dict[str, Any]) -> None:
    if _json_bytes(payload) > MAX_BODY_BYTES:
        code, status, message = (
            "hosted_request_too_large",
            413,
            "Hosted request body exceeds 8 MiB. Reduce attachments or conversation size.",
        )
    elif (
        hosted_chat_input_bound(payload) >= _input_limit(payload)
        or len(payload.get("messages", [])) > 2048
    ):
        code, status, message = (
            "context_length_exceeded",
            400,
            "Hosted context limit exceeded. Compact the conversation or start a new session.",
        )
    else:
        return
    raise LLMError(
        f"LLM error {status}: " + json.dumps({"error": {"code": code, "message": message}})
    )
