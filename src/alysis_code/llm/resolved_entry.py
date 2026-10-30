"""Private, request-scoped lexical evidence for resolved Computer Use entry.

No proof is serialized or accepted from provider metadata. Outside an explicitly
bound request every helper preserves the existing parser behavior.
"""

from __future__ import annotations

import codecs
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

NAME = "computer_start_resolved_v1"
MAX_ARGUMENT_BYTES = 131072
_CLIENT_SEAMS = {}


@dataclass
class _ParseState:
    owner: object
    invalid_parent: bool = False
    source_seen: bool = False


_STATE: ContextVar[_ParseState | None] = ContextVar("resolved_entry_parser", default=None)


@contextmanager
def parser_scope(owner: object):
    token = _STATE.set(_ParseState(owner))
    try:
        yield
    finally:
        _STATE.reset(token)


def active() -> bool:
    return _STATE.get() is not None


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _depth(value: Any, n: int = 0) -> None:
    if isinstance(value, (dict, list)):
        n += 1
        if n > 16:
            raise ValueError("depth")
        for child in value.values() if isinstance(value, dict) else value:
            _depth(child, n)
    elif isinstance(value, str):
        value.encode("utf-8", errors="strict")


def strict_loads(raw: str | bytes, *, limit: int = MAX_ARGUMENT_BYTES) -> Any:
    if isinstance(raw, bytes):
        if len(raw) > limit:
            raise ValueError("size")
        raw = raw.decode("utf-8", errors="strict")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > limit:
        raise ValueError("size")
    # Bound depth before recursive JSON decoding (strings/escapes do not count).
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 16:
                raise ValueError("depth")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            key.encode("utf-8")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("number")

    value = json.loads(raw, object_pairs_hook=pairs, parse_float=invalid, parse_constant=invalid)
    _depth(value)
    if len(canonical(value)) > limit:
        raise ValueError("size")
    return value


# Only structural selectors are checked in outer provider envelopes. Unrelated
# provider data retains its existing numeric/key behavior, even during V turns.
_SELECTORS = frozenset(
    {
        "output",
        "choices",
        "message",
        "delta",
        "tool_calls",
        "function",
        "name",
        "arguments",
        "call_id",
        "id",
        "type",
        "index",
        "item",
        "response",
        "output_index",
    }
)


def provider_loads(raw: str | bytes) -> Any:
    state = _STATE.get()
    if state is None:
        return json.loads(raw)
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeError:
            state.invalid_parent = True
            raise ValueError("Invalid resolved provider encoding.") from None

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result and key in _SELECTORS:
                state.invalid_parent = True
            result[key] = value
        return result

    result = json.loads(raw, object_pairs_hook=pairs)
    state.source_seen = True
    return result


def decode_chunks(chunks, *, encoding):
    if not active():
        from .streaming import decode_text_chunks

        yield from decode_text_chunks(chunks, encoding=encoding)
        return
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    for chunk in chunks:
        try:
            yield decoder.decode(chunk)
        except UnicodeError:
            # Never let replacement decoding manufacture a valid V argument.
            _STATE.get().invalid_parent = True
            raise ValueError("Invalid resolved entry transport encoding") from None
    yield decoder.decode(b"", final=True)


@dataclass(frozen=True, repr=False)
class _Proof:
    owner: object = field(repr=False)
    call_id: str
    digest: bytes = field(repr=False)


def arguments(raw: Any, *, name: str, call_id: str):
    state = _STATE.get()
    if state is None or name != NAME:
        return None
    if state.invalid_parent or not state.source_seen or not isinstance(raw, str):
        return {}, None
    try:
        value = strict_loads(raw)
        if not isinstance(value, dict):
            return {}, None
        digest = hashlib.sha256(canonical(value)).digest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {}, None
    return value, _Proof(state.owner, call_id, digest)


def valid_proof(call, owner: object, value: Any) -> bool:
    proof = getattr(call, "_resolved_entry_proof", None)
    if (
        type(proof) is not _Proof
        or proof.owner is not owner
        or call.name != NAME
        or proof.call_id != call.id
    ):
        return False
    try:
        return (
            proof.digest
            == hashlib.sha256(canonical(call.arguments)).digest()
            == hashlib.sha256(canonical(value)).digest()
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return False


def bound_stream(arguments: Any, name: str = NAME) -> None:
    state = _STATE.get()
    if state is not None and name == NAME and isinstance(arguments, str):
        if len(arguments.encode("utf-8", errors="surrogatepass")) > MAX_ARGUMENT_BYTES:
            state.invalid_parent = True
            raise ValueError("Resolved entry arguments exceed their bound")


def eligible(client: Any) -> bool:
    from .openai_compat import OpenAICompatClient
    from .openai_responses import OpenAIResponsesClient

    expected = _CLIENT_SEAMS.get(type(client))
    return (
        type(client) in (OpenAICompatClient, OpenAIResponsesClient)
        and expected is not None
        and all(
            getattr(getattr(client, name, None), "__func__", getattr(client, name, None)) is func
            for name, func in expected.items()
        )
        and getattr(client, "stream_assembly", "concat") == "concat"
    )


def object_members(raw: str) -> list[tuple[str, str]]:
    """Slice members lexically; never decode a nested value before its byte bound."""
    decoder = json.JSONDecoder()

    def ws(pos):
        while pos < len(raw) and raw[pos] in " \t\r\n":
            pos += 1
        return pos

    def end_value(pos):
        stack = []
        quoted = escaped = False
        for end in range(pos, len(raw)):
            char = raw[end]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                stack.append(char)
            elif char in "]}":
                if not stack:
                    return end
                if stack.pop() != ("[" if char == "]" else "{"):
                    raise ValueError("container")
            elif not stack and char == ",":
                return end
        raise ValueError("unterminated object")

    i = ws(0)
    if i >= len(raw) or raw[i] != "{":
        raise ValueError("object")
    i = ws(i + 1)
    members = []
    while i < len(raw) and raw[i] != "}":
        key, end = decoder.raw_decode(raw, i)
        i = ws(end)
        if not isinstance(key, str) or i >= len(raw) or raw[i] != ":":
            raise ValueError("member")
        i = ws(i + 1)
        end = end_value(i)
        members.append((key, raw[i:end].rstrip()))
        i = ws(end)
        if raw[i] == "}":
            break
        if raw[i] != ",":
            raise ValueError("separator")
        i = ws(i + 1)
    if i >= len(raw) or ws(i + 1) != len(raw):
        raise ValueError("object end")
    return members


class BridgeFrameInvalid(ValueError):
    def __init__(self, *, response_frame: bool, request_id=None):
        super().__init__("Invalid resolved Computer Use frame.")
        self.response_frame = response_frame
        self.request_id = request_id


def validate_bridge_request(raw: str, payload=None) -> None:
    """Validate V before generic decoding; unrelated RPC/T fields keep their policy."""
    try:
        members = object_members(raw)
        methods = [json.loads(v) for k, v in members if k == "method"]
        param_raw = [v for k, v in members if k == "params"]
        v_request = "computer_start_resolved.respond" in methods
        v_create = (
            any(
                k == "computer_start_resolved_host"
                for p in param_raw
                for k, _ in (object_members(p) if p.lstrip().startswith("{") else [])
            )
            if "session.create" in methods
            else False
        )
    except (ValueError, TypeError, IndexError, RecursionError):
        # Generic malformed JSON is rejected by the unchanged RPC decoder.
        return
    if not v_request and not v_create:
        return
    try:
        raw.encode("utf-8", errors="strict")
        for key in ("id", "method", "params", "protocol_version"):
            if sum(k == key for k, _ in members) != 1:
                raise ValueError("duplicate framing")
        if v_request:
            strict_loads(param_raw[0], limit=8192)
        else:
            params = object_members(param_raw[0])
            for key in ("computer_start_resolved_host", "browser_start_host"):
                values = [v for k, v in params if k == key]
                if len(values) > 1:
                    raise ValueError("duplicate descriptor")
                if key == "computer_start_resolved_host":
                    strict_loads(values[0], limit=131072)
                    discovery = [v for k, v in object_members(values[0]) if k == "discovery"]
                    if len(discovery) != 1:
                        raise ValueError("discovery")
                    strict_loads(discovery[0], limit=65536)
    except (ValueError, TypeError, IndexError, RecursionError):
        ids = [v for k, v in members if k == "id"]
        request_id = None
        if len(ids) == 1:
            try:
                candidate = strict_loads(ids[0], limit=8192)
                if type(candidate) in (str, int):
                    request_id = candidate
            except (ValueError, TypeError, RecursionError):
                pass
        raise BridgeFrameInvalid(
            response_frame=methods == ["computer_start_resolved.respond"],
            request_id=request_id,
        ) from None


def canonical_call_proof(call, call_id: str):
    """Trusted ephemeral-ID projection, after whole-response linkage validation."""
    proof = getattr(call, "_resolved_entry_proof", None)
    if type(proof) is _Proof and valid_proof(call, proof.owner, call.arguments):
        return _Proof(proof.owner, call_id, proof.digest)
    return None


def _register_client(cls, names):
    _CLIENT_SEAMS[cls] = {name: getattr(cls, name) for name in names}


def invalidate_parent():
    state = _STATE.get()
    if state is not None:
        state.invalid_parent = True
