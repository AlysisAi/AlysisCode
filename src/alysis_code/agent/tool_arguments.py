"""Check a tool call's arguments against the tool's own parameter schema before dispatch.

A model sometimes sends valid JSON with a value of the wrong type, for example
``{"path": "app.ts", "start_line": ","}``. Without this check the first code to read the value
(a read cache, a tool, a telemetry hook) raises ``ValueError`` and the whole turn ends. With it,
the call gets a structured ``invalid_tool_arguments`` result naming each bad field, and the
model can correct the call, as it already can for arguments that are not JSON at all.

The rules are deterministic and deliberately small. A schema describes what a tool
documents, not everything its handler accepts (``subagent_spawn`` also takes project-defined
names outside its ``enum``), so this check only rejects what can never work:

* Only the top-level ``properties`` of an object schema are checked; a property whose schema
  uses ``anyOf``/``oneOf``/``allOf``/``$ref`` or no ``type`` is passed through unchanged.
* Values the tools already accepted keep working, converted to the declared type: an integer
  written as a string (``"190"``), a whole float (``190.0``), ``"true"``/``"false"`` for a
  boolean, a number for a string, a single value for an array of scalars, and an enum value in
  another letter case (the schema's spelling is used).
* ``null`` is passed through: the tools treat it as "not given".
* The one problem: a property declared only as ``integer``/``number`` whose value is not a
  number (``","``, ``true``, ``1.5`` for an integer, a list). No reading of it is a number.
* Everything else is left to the tool: missing or unknown properties, values outside an
  ``enum``, other type mismatches and numeric bounds.

Only built-in tools are checked (see ``agent/turn/core.py``); Computer Use, MCP and custom
tools are validated by the host or server that runs them.
"""

from __future__ import annotations

import math
import re
from typing import Any

_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")
_SCALAR_TYPES = {"string", "integer", "number", "boolean"}
_NUMERIC_TYPES = {"integer", "number", "null"}


def _types(schema: dict[str, Any]) -> list[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [t for t in declared if isinstance(t, str)]
    return []


def _shown(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _convert(value: Any, kind: str) -> tuple[bool, Any]:
    """(converted?, value) for one JSON Schema type."""

    if kind == "integer":
        if isinstance(value, bool):
            return False, value
        if isinstance(value, int):
            return True, value
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return True, int(value)
        if isinstance(value, str) and _INTEGER_TEXT.match(value.strip()):
            return True, int(value.strip())
        return False, value
    if kind == "number":
        if isinstance(value, bool):
            return False, value
        if isinstance(value, (int, float)) and math.isfinite(value):
            return True, value
        if isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                return False, value
            if math.isfinite(number):
                return True, int(number) if _INTEGER_TEXT.match(value.strip()) else number
        return False, value
    if kind == "boolean":
        if isinstance(value, bool):
            return True, value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return True, value.strip().lower() == "true"
        return False, value
    if kind == "string":
        if isinstance(value, str):
            return True, value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True, str(value)
        return False, value
    if kind == "array":
        return isinstance(value, list), value
    if kind == "object":
        return isinstance(value, dict), value
    if kind == "null":
        return value is None, value
    return True, value  # a type this check does not know is passed through


def _conform_value(name: str, value: Any, schema: dict[str, Any]) -> tuple[Any, str | None]:
    if value is None or any(key in schema for key in ("anyOf", "oneOf", "allOf", "$ref")):
        return value, None
    kinds = _types(schema)
    if kinds:
        for kind in kinds:
            ok, converted = _convert(value, kind)
            if ok:
                value = converted
                break
        else:
            items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
            item_kinds = _types(items)
            if "array" in kinds and item_kinds and set(item_kinds) <= _SCALAR_TYPES:
                for item_kind in item_kinds:
                    ok, converted_item = _convert(value, item_kind)
                    if ok:
                        return [_conform_value(name, converted_item, items)[0]], None
            if not (set(kinds) <= _NUMERIC_TYPES and set(kinds) & {"integer", "number"}):
                return value, None  # the tool decides; it may accept other shapes
            expected = " or ".join(kinds)
            article = "an" if expected[:1] in "aeiou" else "a"
            return value, f"{name} must be {article} {expected} (got {_shown(value)})"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and value not in enum and isinstance(value, str):
        folded = {str(option).casefold(): option for option in enum if isinstance(option, str)}
        if value.casefold() in folded:
            return folded[value.casefold()], None
    return value, None


def conform_tool_arguments(schema: Any, arguments: Any) -> tuple[dict[str, Any], list[str]]:
    """Arguments converted to the schema's types, and the problems that remain.

    The input is not modified. A schema that is not an object schema with ``properties``
    checks nothing.
    """

    if not isinstance(arguments, dict):
        return {}, ["arguments must be a JSON object"]
    conformed = dict(arguments)
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return conformed, []
    problems: list[str] = []
    for name, property_schema in schema["properties"].items():
        if name not in conformed or not isinstance(property_schema, dict):
            continue
        conformed[name], problem = _conform_value(name, conformed[name], property_schema)
        if problem:
            problems.append(problem)
    return conformed, problems


def invalid_tool_arguments_result(tool_name: str, problems: list[str]) -> dict[str, Any]:
    return {
        "error": f"Invalid arguments for {tool_name}: " + "; ".join(problems),
        "error_code": "invalid_tool_arguments",
        "problems": list(problems),
        "guidance": "Re-issue the call with arguments that match the tool's parameter schema.",
    }
