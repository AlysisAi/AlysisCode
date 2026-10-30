"""Bounded operation-local discovery for host-authorized GitHub capabilities.

The desktop owns catalog policy, accounts, target resolution and exact approval.
These references never grant authority outside the existing host-action broker.
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any

from ..host_actions import HostActionError
from .github_host import _TOOL_NAME, _json_copy
from .untrusted_content import build_untrusted_mcp_text_block

DISCOVER = "mcp.github.discover"
DESCRIBE = "mcp.github.describe"
CALL = "mcp.github.call"
ACTIONS = frozenset({DISCOVER, DESCRIBE, CALL})
MAX_RESULTS = 20
MAX_DESCRIBE = 4
MAX_SUMMARIES = 256
MAX_LOADED = 32
_SCOPES = {"account", "organization", "repository", "workspace"}
_TARGET_FIELDS = {
    "owner",
    "repo",
    "repository",
    "organization",
    "org",
    "account",
    "account_login",
    "login",
    "headers",
    "token",
    "access_token",
    "authorization",
    "endpoint",
    "base_url",
}
_IDENTIFIER = re.compile(r"[\x21-\x7e]{1,256}\Z")
_DESTINATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _fail(message: str) -> None:
    raise HostActionError("invalid_github_discovery", message)


def _finite(value: Any) -> bool:
    try:
        return type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        return False


def _keys(raw: Any, required: set[str], optional: set[str] | None = None) -> None:
    if (
        not isinstance(raw, dict)
        or not required <= set(raw)
        or set(raw) - required - (optional or set())
    ):
        _fail("GitHub discovery fields do not match the negotiated contract.")


def _ref(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _fail("GitHub discovery references must be bounded host-issued identifiers.")
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str) or not _TOOL_NAME.fullmatch(value):
        _fail("GitHub tool name must be a bounded identifier.")
    return value


def _hash(value: Any) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail("GitHub schema identity must be a SHA-256 identifier.")
    return value


def _target(raw: Any) -> dict[str, Any]:
    _keys(raw, {"scope"}, {"owner", "repo", "organization"})
    scope = raw["scope"]
    if not isinstance(scope, str) or scope not in _SCOPES:
        _fail("GitHub target scope is unsupported.")
    fields = {"scope"}
    if scope == "repository":
        fields |= {"owner", "repo"}
    elif scope == "organization":
        fields |= {"organization"}
    _keys(raw, fields)
    for key in fields - {"scope"}:
        if (
            not isinstance(raw[key], str)
            or not _DESTINATION.fullmatch(raw[key])
            or raw[key] in {".", ".."}
        ):
            _fail("GitHub destination must be a bounded ordinary identifier.")
    return raw


def discovery_arguments(action: str, raw: Any) -> dict[str, Any]:
    raw = _json_copy(raw, limit=8 * 1024)
    if action == DISCOVER:
        _keys(raw, set(), {"query", "cursor", "limit"})
        if "query" in raw and (
            not isinstance(raw["query"], str) or len(raw["query"]) > 256 or "\0" in raw["query"]
        ):
            _fail("GitHub discovery query must be a bounded string.")
        if "cursor" in raw:
            _ref(raw["cursor"])
        if "limit" in raw and (
            type(raw["limit"]) is not int or not 1 <= raw["limit"] <= MAX_RESULTS
        ):
            _fail("GitHub discovery limit must be 1 to 20.")
    elif action == DESCRIBE:
        _keys(raw, {"catalog_id", "names", "target"})
        _ref(raw["catalog_id"])
        names = raw["names"]
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_DESCRIBE:
            _fail("Load 1 to 4 GitHub definitions at a time.")
        for name in names:
            _name(name)
        if len(set(names)) != len(names):
            _fail("GitHub definition names must be unique.")
        _target(raw["target"])
    elif action == CALL:
        _keys(raw, {"catalog_id", "target_id", "schema_hash", "name", "arguments"})
        _ref(raw["catalog_id"])
        _ref(raw["target_id"])
        _hash(raw["schema_hash"])
        _name(raw["name"])
        if not isinstance(raw["arguments"], dict) or set(raw["arguments"]) & _TARGET_FIELDS:
            _fail("GitHub arguments cannot override host account, transport or target binding.")
    else:
        _fail("Unsupported GitHub discovery action.")
    return raw


def _schema(raw: Any) -> None:
    if not isinstance(raw, dict) or raw.get("type") != "object":
        _fail("GitHub schema must describe an object.")
    properties = raw.get("properties", {})
    if not isinstance(properties, dict) or len(properties) > 64 or set(properties) & _TARGET_FIELDS:
        _fail("GitHub schema must bind account, transport and target fields in the host.")
    required = raw.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(k, str) or k not in properties for k in required
    ):
        _fail("GitHub required fields must be declared properties.")
    _json_copy(raw, limit=24_000)

    # Validate bounded schema structure without following references or evaluating regexes.
    def walk(schema: Any, depth: int = 0) -> None:
        if depth > 12 or not isinstance(schema, dict):
            _fail("GitHub schema is too deep or unsupported.")
        if set(schema) & {
            "$ref",
            "$dynamicRef",
            "pattern",
            "patternProperties",
            "not",
            "if",
            "then",
            "else",
            "dependentSchemas",
            "contains",
            "unevaluatedProperties",
        }:
            _fail("GitHub schema contains unsupported validation or reference keywords.")
        kind = schema.get("type")
        types = kind if isinstance(kind, list) else [kind]
        if any(
            t is not None
            and (
                not isinstance(t, str)
                or t not in {"object", "array", "string", "integer", "number", "boolean", "null"}
            )
            for t in types
        ):
            _fail("GitHub schema type is unsupported.")
        for keyword in ("properties",):
            if keyword in schema:
                if not isinstance(schema[keyword], dict) or len(schema[keyword]) > 64:
                    _fail("GitHub schema properties are unsupported.")
                for child in schema[keyword].values():
                    walk(child, depth + 1)
        for keyword in ("items", "additionalProperties"):
            if keyword in schema and not isinstance(schema[keyword], bool):
                walk(schema[keyword], depth + 1)
        for keyword in ("anyOf", "oneOf", "allOf"):
            if keyword in schema:
                parts = schema[keyword]
                if not isinstance(parts, list) or not 1 <= len(parts) <= 16:
                    _fail("GitHub schema composition is unsupported.")
                for part in parts:
                    walk(part, depth + 1)
        for keyword in (
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "minProperties",
            "maxProperties",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
        ):
            if keyword in schema and (not _finite(schema[keyword])):
                _fail("GitHub schema bounds must be finite numbers.")
        if "required" in schema and (
            not isinstance(schema["required"], list)
            or any(not isinstance(k, str) for k in schema["required"])
        ):
            _fail("GitHub schema required fields are unsupported.")
        if "enum" in schema and not isinstance(schema["enum"], list):
            _fail("GitHub schema enum is unsupported.")

    walk(raw)


def _descriptor(raw: Any, *, loaded: bool = False) -> None:
    _keys(
        raw,
        {"name", "description", "readOnly", "scope", "schema_hash"}
        | ({"inputSchema"} if loaded else set()),
    )
    _name(raw["name"])
    _hash(raw["schema_hash"])
    if (
        not isinstance(raw["description"], str)
        or len(raw["description"]) > 2000
        or type(raw["readOnly"]) is not bool
    ):
        _fail("GitHub descriptions and host policy must be bounded and typed.")
    if not isinstance(raw["scope"], str) or raw["scope"] not in _SCOPES:
        _fail("GitHub capability scope is unsupported.")
    if loaded:
        _schema(raw["inputSchema"])


def discovery_result(action: str, raw: Any) -> dict[str, Any]:
    raw = _json_copy(raw, limit=64 * 1024)
    _keys(
        raw,
        {"catalog_id", "tools"} | ({"target_id"} if action == DESCRIBE else set()),
        {"next_cursor", "instructions"} if action == DISCOVER else set(),
    )
    _ref(raw["catalog_id"])
    if "instructions" in raw and (
        not isinstance(raw["instructions"], str) or len(raw["instructions"]) > 12_000
    ):
        _fail("GitHub server instructions must be a bounded string.")
    if action == DESCRIBE:
        _ref(raw["target_id"])
    elif action != DISCOVER:
        _fail("Unsupported GitHub discovery result.")
    if "next_cursor" in raw:
        _ref(raw["next_cursor"])
    tools = raw["tools"]
    maximum = MAX_DESCRIBE if action == DESCRIBE else MAX_RESULTS
    if not isinstance(tools, list) or len(tools) > maximum or (action == DESCRIBE and not tools):
        _fail("GitHub discovery result contains too many or missing definitions.")
    for tool in tools:
        _descriptor(tool, loaded=action == DESCRIBE)
    if len({t["name"] for t in tools}) != len(tools):
        _fail("GitHub discovery returned duplicate names.")
    return raw


def valid_arguments(value: Any, schema: dict[str, Any], depth: int = 0) -> bool:
    """Conservative bounded checks; the host additionally validates before HTTP dispatch."""
    if depth > 12:
        return False
    if "enum" in schema and not any(type(value) is type(x) and value == x for x in schema["enum"]):
        return False
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        return False
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            checks = [valid_arguments(value, s, depth + 1) for s in schema[keyword]]
            if (
                (keyword == "allOf" and not all(checks))
                or (keyword == "anyOf" and not any(checks))
                or (keyword == "oneOf" and sum(checks) != 1)
            ):
                return False
    kind = schema.get("type")
    if isinstance(kind, list):
        return any(valid_arguments(value, {**schema, "type": k}, depth + 1) for k in kind)
    if kind == "object" or "properties" in schema:
        if not isinstance(value, dict) or any(k not in value for k in schema.get("required", [])):
            return False
        if not schema.get("minProperties", 0) <= len(value) <= schema.get("maxProperties", 64):
            return False
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", False)
        return all(
            valid_arguments(v, props[k], depth + 1)
            if k in props
            else extra is True or isinstance(extra, dict) and valid_arguments(v, extra, depth + 1)
            for k, v in value.items()
        )
    if kind == "array":
        items = schema.get("items", {})
        return (
            isinstance(value, list)
            and schema.get("minItems", 0) <= len(value) <= min(schema.get("maxItems", 100), 100)
            and (
                not schema.get("uniqueItems")
                or len({json.dumps(x, sort_keys=True) for x in value}) == len(value)
            )
            and all(
                items is True or isinstance(items, dict) and valid_arguments(v, items, depth + 1)
                for v in value
            )
        )
    if kind == "string":
        return (
            isinstance(value, str)
            and "\0" not in value
            and schema.get("minLength", 0)
            <= len(value)
            <= min(schema.get("maxLength", 20000), 20000)
        )
    if kind in {"integer", "number"}:
        if not _finite(value) or kind == "integer" and value != int(value):
            return False
        return (
            schema.get("minimum", -1e100) <= value <= schema.get("maximum", 1e100)
            and value > schema.get("exclusiveMinimum", -1e100)
            and value < schema.get("exclusiveMaximum", 1e100)
            and (
                "multipleOf" not in schema
                or schema["multipleOf"] > 0
                and math.isclose(value / schema["multipleOf"], round(value / schema["multipleOf"]))
            )
        )
    if kind == "boolean":
        return type(value) is bool
    if kind == "null":
        return value is None
    return True


class GitHubDiscoveryState:
    """Never persisted; a new broker job must rediscover its available definitions."""

    def __init__(self) -> None:
        self.operation: str | None = None
        self.catalog_id: str | None = None
        self.summaries: dict[str, dict[str, Any]] = {}
        self.loaded: dict[tuple[str, str], dict[str, Any]] = {}
        self.targets: dict[str, dict[str, Any]] = {}
        self.cursors: dict[str, str] = {}

    def begin(self, operation: str) -> None:
        if self.operation != operation:
            self.__init__()
            self.operation = operation

    def check(
        self, action: str, args: dict[str, Any], *, readonly: bool = False, approvals: bool = True
    ) -> None:
        if action == DISCOVER:
            if "cursor" in args and self.cursors.get(args["cursor"]) != args.get("query", ""):
                _fail("GitHub cursor was not issued for this operation and query.")
            return
        if self.catalog_id is None or args["catalog_id"] != self.catalog_id:
            _fail("GitHub catalog is stale or was not discovered in this operation.")
        if action == DESCRIBE:
            for name in args["names"]:
                tool = self.summaries.get(name)
                if tool is None:
                    _fail("Discover the GitHub capability before loading its definition.")
                if readonly and not tool["readOnly"]:
                    _fail("GitHub write capabilities are unavailable in this scope.")
                # Scope is descriptive. The host checks compatibility and can
                # resolve account creation to an authorized organization, or a
                # repository capability to the current workspace repository.
            return
        tool = self.loaded.get((args["target_id"], args["name"]))
        if tool is None or tool["schema_hash"] != args["schema_hash"]:
            _fail("Load the exact GitHub definition and target before calling it.")
        if not tool["readOnly"] and (readonly or not approvals):
            _fail(
                "Host-managed approval and unrestricted write scope are required for GitHub writes."
            )
        if not valid_arguments(args["arguments"], tool["inputSchema"]):
            _fail("GitHub arguments do not match the loaded definition.")

    def record(
        self, action: str, args: dict[str, Any], result: dict[str, Any], *, readonly: bool = False
    ) -> None:
        if action == DISCOVER:
            if len(result["tools"]) > args.get("limit", 10):
                _fail("GitHub discovery exceeded the requested page size.")
            if self.catalog_id != result["catalog_id"]:
                if "cursor" in args:
                    _fail("GitHub catalog changed during pagination; restart discovery.")
                self.summaries.clear()
                self.loaded.clear()
                self.targets.clear()
                self.cursors.clear()
            summaries = copy.deepcopy(self.summaries)
            for tool in result["tools"]:
                if readonly and not tool["readOnly"]:
                    _fail("The host advertised a write capability in readonly scope.")
                if tool["name"] in summaries and summaries[tool["name"]] != tool:
                    _fail("GitHub definition changed within the same catalog.")
                summaries[tool["name"]] = copy.deepcopy(tool)
            if len(summaries) > MAX_SUMMARIES or len(self.cursors) >= MAX_SUMMARIES:
                _fail("GitHub discovery state is full; start a fresh operation.")
            cursor = result.get("next_cursor")
            if cursor and (cursor == args.get("cursor") or cursor in self.cursors):
                _fail("GitHub discovery returned a repeated cursor.")
            self.catalog_id = result["catalog_id"]
            self.summaries = summaries
            if cursor:
                self.cursors[cursor] = args.get("query", "")
        elif action == DESCRIBE:
            self.check(action, args, readonly=readonly)
            if result["catalog_id"] != self.catalog_id or {
                t["name"] for t in result["tools"]
            } != set(args["names"]):
                _fail("GitHub describe returned stale or unexpected definitions.")
            target_id = result["target_id"]
            if target_id in self.targets and self.targets[target_id] != args["target"]:
                _fail("GitHub host attempted to rebind an existing target reference.")
            loaded = copy.deepcopy(self.loaded)
            for tool in result["tools"]:
                descriptor = {k: v for k, v in tool.items() if k != "inputSchema"}
                if descriptor != self.summaries[tool["name"]]:
                    _fail("GitHub describe changed the discovered schema or policy identity.")
                key = (target_id, tool["name"])
                if key in loaded and loaded[key] != tool:
                    _fail("GitHub schema changed within the same identity.")
                loaded[key] = copy.deepcopy(tool)
            if len(loaded) > MAX_LOADED:
                _fail("GitHub loaded-definition limit reached; start a fresh operation.")
            self.targets[target_id] = copy.deepcopy(args["target"])
            self.loaded = loaded


def wrap_discovery_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "catalog_id": result["catalog_id"],
        **({"target_id": result["target_id"]} if "target_id" in result else {}),
        **({"next_cursor": result["next_cursor"]} if "next_cursor" in result else {}),
        "text": build_untrusted_mcp_text_block(
            source_type="tool_result",
            server_id="alysis.github/github",
            source_name=action,
            text=json.dumps(result, ensure_ascii=False),
        ),
    }


_REF_SCHEMA = {"type": "string", "maxLength": 256}
TOOL_SPECS = (
    (
        DISCOVER,
        "github_discover",
        "Search the selected account's available GitHub capabilities with a short query; browse bounded pages when needed. Discover repository, collaboration, CI, release, organization and project operations. Load relevant definitions with github_describe before execution. Tool availability is distinct from write approval.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 256},
                "cursor": _REF_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "additionalProperties": False,
        },
    ),
    (
        DESCRIBE,
        "github_describe",
        "Load 1 to 4 discovered GitHub tool schemas for an explicit account, organization, repository or current workspace target. The host resolves access and confirms target changes. Account means the selected account; creation can precede a repository/workspace. Use returned target_id and schema_hash with github_call.",
        {
            "type": "object",
            "properties": {
                "catalog_id": _REF_SCHEMA,
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 4,
                },
                "target": {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string", "enum": sorted(_SCOPES)},
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "organization": {"type": "string"},
                    },
                    "required": ["scope"],
                    "additionalProperties": False,
                },
            },
            "required": ["catalog_id", "names", "target"],
            "additionalProperties": False,
        },
    ),
    (
        CALL,
        "github_call",
        "Execute a previously loaded GitHub capability against its exact host-resolved target, using the loaded schema's arguments. Supply catalog_id, target_id and schema_hash unchanged. The host owns account and write approval. Inspect outcomes before retrying uncertain writes. Choose and compose capabilities according to the user's goal.",
        {
            "type": "object",
            "properties": {
                "catalog_id": _REF_SCHEMA,
                "target_id": _REF_SCHEMA,
                "schema_hash": {"type": "string", "maxLength": 64},
                "name": {"type": "string"},
                "arguments": {"type": "object", "additionalProperties": True},
            },
            "required": ["catalog_id", "target_id", "schema_hash", "name", "arguments"],
            "additionalProperties": False,
        },
    ),
)
