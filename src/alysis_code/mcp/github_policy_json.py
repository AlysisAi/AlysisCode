"""Strict JSON only for the negotiated restricted GitHub namespace."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ..host_actions import HostActionError
from .github_repository_policy import NATIVE, json_copy

_RESTRICTED = ContextVar("github_repository_policy_restricted", default=False)
NAMES = frozenset(name for name, _ in NATIVE.values()) | {
    "github_discover",
    "github_describe",
    "github_call",
}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def strict_object(raw: str) -> dict[str, Any]:
    result = json.loads(
        raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
    )
    if type(result) is not dict:
        raise ValueError("object required")
    return json_copy(result)


@contextmanager
def argument_scope(restricted: bool):
    token = _RESTRICTED.set(restricted)
    try:
        yield
    finally:
        _RESTRICTED.reset(token)


def active() -> bool:
    return _RESTRICTED.get()


def arguments(raw: Any, name: str) -> dict[str, Any] | None:
    if not _RESTRICTED.get() or name not in NAMES:
        return None
    try:
        if type(raw) is not str:
            raise ValueError("raw JSON required")
        return strict_object(raw)
    except (ValueError, TypeError, RecursionError, HostActionError):
        # Fail through the ordinary closed tool/host argument validator. Never
        # echo malformed raw JSON or drop the failed call silently.
        return {"_invalid_github_arguments": True}


def validate_sidecar_frame(raw: str) -> None:
    """Preserve other protocol behavior; inspect duplicates only on this path."""

    class Pairs(list):
        pass

    try:
        frame = json.loads(raw, object_pairs_hook=Pairs)
    except (ValueError, TypeError, RecursionError):
        return  # Existing protocol decoder owns unrelated malformed frames.
    if not isinstance(frame, Pairs):
        return
    params_values = [v for k, v in frame if k == "params" and isinstance(v, Pairs)]
    if not any(any(k == "github_repository_policy" for k, _ in p) for p in params_values):
        return

    # Envelope and params keys determine ownership; duplicates there could select
    # a different sidecar or method. Unrelated nested namespaces keep their decoder.
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate policy envelope key")
            result[k] = v
        return result

    def plain(value):
        if isinstance(value, Pairs):
            return {k: plain(v) for k, v in unique(value).items()}
        if isinstance(value, list):
            return [plain(v) for v in value]
        return value

    envelope = unique(frame)
    params = unique(envelope["params"])
    json_copy(plain(params["github_repository_policy"]))


def provider_payload(raw: str, *, protocol: str) -> Any:
    """Decode object-argument providers, replacing only ambiguous GitHub inputs.

    Other tool inputs and provider envelope fields retain ordinary JSON decoding.
    This runs before SDK/stream reconstruction would collapse their duplicate keys.
    """
    if not _RESTRICTED.get():
        return json.loads(raw)

    class Pairs(list):
        pass

    tree = json.loads(raw, object_pairs_hook=Pairs)

    def strict_plain(value):
        if isinstance(value, Pairs):
            return {k: strict_plain(v) for k, v in _pairs(value).items()}
        if isinstance(value, list):
            return [strict_plain(v) for v in value]
        return value

    def plain(value, path=()):
        if isinstance(value, Pairs):
            result = {k: plain(v, (*path, k)) for k, v in value}
            # Exact provider-owned call positions only: a nested user object with
            # these field names is data, not another tool-call envelope.
            anthropic_position = (
                path == ("content_block",)
                or len(path) == 2
                and path[0] == "content"
                and type(path[1]) is int
                or len(path) == 3
                and path[:2] == ("message", "content")
                and type(path[2]) is int
            )
            gemini_position = (
                len(path) == 6
                and path[0] == "candidates"
                and type(path[1]) is int
                and path[2:4] == ("content", "parts")
                and type(path[4]) is int
                and path[5] == "functionCall"
            )
            is_call = (
                protocol == "anthropic"
                and anthropic_position
                and result.get("type") == "tool_use"
                or protocol == "gemini"
                and gemini_position
            )
            names = [v for k, v in value if k == "name" and isinstance(v, str)]
            if is_call and any(name in NAMES for name in names):
                key = "input" if protocol == "anthropic" else "args"
                try:
                    obj = _pairs(value)
                    args = strict_plain(obj.get(key))
                    if type(args) is not dict:
                        raise ValueError("object required")
                    result[key] = json_copy(args)
                except (ValueError, TypeError, RecursionError, HostActionError):
                    result[key] = {"_invalid_github_arguments": True}
            return result
        if isinstance(value, list):
            return [plain(v, (*path, index)) for index, v in enumerate(value)]
        return value

    return plain(tree)
