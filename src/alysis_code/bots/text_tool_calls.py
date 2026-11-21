"""Tool calls a model wrote into its reply instead of making them.

A model, more often a small one or one served through a compatible endpoint, sometimes writes
the call it means to make as text: ``<tool_call>{"name": …, "arguments": {…}}</tool_call>``,
``<function=name>{…}</function>`` or ``<function=name><parameter=key>value</parameter>
</function>``, or a reply that is nothing but ``{"name": …, "arguments": {…}}``. Published,
the person would read markup and nothing would happen.

``read`` decides from structure alone, never from the words of any language:

- ``None``: no tool-call markup outside code blocks; the reply is a reply. A reply that is
  only a JSON object counts only when it names a tool offered now with its arguments.
- ``Written(calls)``: markup that ends the reply (any text before it is kept as the reply's
  preface) and names tools offered now with object arguments: the caller runs these as real
  calls, through the same path and checks.
- ``Written([])``: markup that cannot be run (text after it, a tool not offered now,
  arguments that are not an object): the caller publishes nothing and asks once for a real
  call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ..llm.types import ToolCall

# Shown code is content, not a call: code blocks and spans are left out of the markup search.
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_MARKUP = re.compile(r"</?tool_call>|<function=", re.IGNORECASE)
_TOOL_CALL = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.DOTALL | re.IGNORECASE)
_FUNCTION = re.compile(r"<function=([^>\s]+)\s*>(.*?)(?:</function>|\Z)", re.DOTALL | re.IGNORECASE)
_PARAMETER = re.compile(r"\s*<parameter=([^>\s]+)\s*>(.*?)</parameter>", re.DOTALL | re.IGNORECASE)
_WHOLE_FENCE = re.compile(r"\A```[^\n]*\n(.*?)\n?```\Z", re.DOTALL)

WRITTEN_CALL_NUDGE = (
    "Your previous reply wrote a tool call as text, so it was neither run nor shown to the "
    "person. To use a tool, make the call itself through tool calling, with a tool offered now. "
    "Otherwise answer in plain text, without tool-call markup."
)
WRITTEN_CALL_ERROR = (
    "The model wrote its next step as text instead of taking it, twice, so nothing was "
    "published. Send your message again, or try a different model. Saved results remain "
    "available."
)


@dataclass(frozen=True)
class Written:
    calls: list[ToolCall] = field(default_factory=list)
    # Text before the calls, kept as the reply's own text.
    preface: str = ""


class _NotACall(ValueError):
    pass


def read(content: Any, offered: list[dict]) -> Written | None:
    """The tool calls written into ``content``; ``offered``: the schemas offered now."""
    if not isinstance(content, str) or not content.strip():
        return None
    properties = {
        schema["function"]["name"]: (schema["function"].get("parameters") or {}).get("properties")
        or {}
        for schema in offered
        if isinstance(schema, dict) and isinstance(schema.get("function"), dict)
    }
    hidden = _CODE.sub(lambda match: " " * len(match.group(0)), content)
    found = _MARKUP.search(hidden)
    if found is None:
        call = _whole_json_call(content.strip(), properties)
        return Written([call]) if call is not None else None
    try:
        calls = _markup_calls(content[found.start() :], properties)
    except ValueError:  # Not JSON, or not a call that can run now.
        return Written()
    return Written(calls, content[: found.start()].strip())


def _whole_json_call(text: str, properties: dict) -> ToolCall | None:
    fenced = _WHOLE_FENCE.match(text)
    try:
        value = json.loads(fenced.group(1) if fenced else text)
        return _call(value, properties)
    except (ValueError, TypeError):
        return None


def _markup_calls(tail: str, properties: dict) -> list[ToolCall]:
    """Every call in ``tail``, which must be calls and whitespace only, up to its end."""
    calls, position = [], 0
    while position < len(tail):
        rest = tail[position:]
        if not rest.strip():
            break
        offset = len(rest) - len(rest.lstrip())
        block = _TOOL_CALL.match(rest, offset) or _FUNCTION.match(rest, offset)
        if block is None:
            raise _NotACall("text after a written call")
        if block.re is _TOOL_CALL:
            body = block.group(1).strip()
            inner = _FUNCTION.fullmatch(body)
            calls.append(
                _function(inner, properties)
                if inner is not None
                else _call(json.loads(body), properties)
            )
        else:
            calls.append(_function(block, properties))
        position += block.end()
    if not calls:
        raise _NotACall("no call")
    return calls


def _function(block: re.Match, properties: dict) -> ToolCall:
    name, body = block.group(1).strip(), block.group(2).strip()
    if body.startswith("{"):
        try:
            arguments = json.loads(body)
        except ValueError:
            raise _NotACall("arguments are not JSON") from None
        return _call({"name": name, "arguments": arguments}, properties)
    arguments, position = {}, 0
    schema = properties.get(name) or {}
    while position < len(body):
        parameter = _PARAMETER.match(body, position)
        if parameter is None:
            if body[position:].strip():
                raise _NotACall("text inside a written call")
            break
        key, value = parameter.group(1).strip(), parameter.group(2)
        arguments[key] = _parameter_value(value.strip("\n"), schema.get(key))
        position = parameter.end()
    return _call({"name": name, "arguments": arguments}, properties)


def _parameter_value(text: str, schema: Any) -> Any:
    """A parameter written as text, as its declared type: strings stay as written."""
    declared = schema.get("type") if isinstance(schema, dict) else None
    kinds = declared if isinstance(declared, list) else [declared]
    if "string" in kinds:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _call(value: Any, properties: dict) -> ToolCall:
    if isinstance(value, dict) and isinstance(value.get("function"), dict):
        value = value["function"]
    if not isinstance(value, dict):
        raise _NotACall("not a call object")
    name = value.get("name")
    arguments = value.get("arguments", value.get("parameters", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            raise _NotACall("arguments are not JSON") from None
    if not isinstance(name, str) or name not in properties or not isinstance(arguments, dict):
        raise _NotACall("not a call to an offered tool")
    return ToolCall(f"written_{uuid4().hex[:16]}", name, arguments)
