from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, Protocol, TypeVar, runtime_checkable

from .types import InputTokenCount, LLMError, LLMResponse, UsageContract

_REQUIRED_TOOL_NAMES_ATTRIBUTE = "_alysis_required_tool_names"
_SERIAL_TOOL_CALLS_ATTRIBUTE = "_alysis_serial_tool_calls_required"
_STATELESS_RESPONSES_ATTRIBUTE = "_alysis_stateless_responses_required"
_OMITTED_REASONING_DISPLAY_ATTRIBUTE = "_alysis_omitted_reasoning_display_required"
_SCOPED_TOKEN_COUNTER_ATTRIBUTE = "_alysis_scoped_token_counter"
_Counter = TypeVar("_Counter", bound=Callable[..., InputTokenCount | None])
_token_count_request_context: ContextVar[Callable[[], AbstractContextManager[None]] | None] = (
    ContextVar("input_token_count_request_context", default=None)
)


def scoped_input_token_counter(counter: _Counter) -> _Counter:
    """Declare that a counter scopes its network work with input_token_count_request.

    Local estimation and disabled counting need no request scope. Unmarked
    extension counters remain conservatively guarded as a whole.
    """
    setattr(counter, _SCOPED_TOKEN_COUNTER_ATTRIBUTE, True)
    return counter


@contextmanager
def input_token_count_request() -> Iterator[None]:
    request_context = _token_count_request_context.get()
    with request_context() if request_context is not None else nullcontext():
        yield


class RequiredToolCallingUnsupportedError(LLMError):
    """The active route cannot supply tools that the current session requires."""


@runtime_checkable
class ChatClient(Protocol):
    """Protocol-neutral chat client surface used by the agent runtime."""

    base_url: str
    model: str
    supports_tool_calling: bool
    supports_forced_tool_choice: bool
    usage_contract: UsageContract

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        response_format: dict[str, Any] | None = None,
        stream: bool = False,
        on_text_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cancellation_token: Any | None = None,
    ) -> LLMResponse: ...


@runtime_checkable
class InputTokenCountingClient(Protocol):
    usage_contract: UsageContract

    def count_input_tokens(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
    ) -> InputTokenCount | None: ...


def require_tool_calling(client: Any, tool_names: list[str] | tuple[str, ...]) -> None:
    """Mark one session client as fail-closed when its fixed tools are unavailable.

    Ordinary chat may legitimately retry a provider without optional tools. A host-mediated
    Computer Use session cannot: omitting its fixed tools would turn a requested desktop action
    into an ungrounded text-only answer. The marker is kept on the per-session client and is never
    serialized into a provider request.
    """

    normalized = tuple(dict.fromkeys(str(name).strip() for name in tool_names if str(name).strip()))
    if not normalized:
        raise ValueError("required tool names must not be empty")
    setattr(client, _REQUIRED_TOOL_NAMES_ATTRIBUTE, normalized)


def require_serial_tool_calls(client: Any) -> None:
    """Require one provider tool call per assistant response for this client.

    Computer Use snapshot identifiers are linear capabilities: a later action
    cannot be constructed safely until the model has received the preceding
    host result. Provider request flags are advisory, so the agent loop also
    enforces this marker before any tool in a returned batch is dispatched.
    """

    setattr(client, _SERIAL_TOOL_CALLS_ATTRIBUTE, True)


def serial_tool_calls_required(client: Any) -> bool:
    return getattr(client, _SERIAL_TOOL_CALLS_ATTRIBUTE, False) is True


def require_stateless_responses(client: Any) -> None:
    """Disable provider-side Responses storage and continuation for one client."""

    setattr(client, _STATELESS_RESPONSES_ATTRIBUTE, True)


def stateless_responses_required(client: Any) -> bool:
    return getattr(client, _STATELESS_RESPONSES_ATTRIBUTE, False) is True


def require_omitted_reasoning_display(client: Any) -> None:
    """Prevent provider reasoning summaries from entering a sensitive session.

    Computer Use providers must still be free to reason and to retain opaque
    continuation capabilities.  They must not return a readable reasoning
    summary derived from a private desktop snapshot, however.  This marker is
    scoped to the per-session client and is never serialized into a request.
    """

    setattr(client, _OMITTED_REASONING_DISPLAY_ATTRIBUTE, True)


def omitted_reasoning_display_required(client: Any) -> bool:
    return getattr(client, _OMITTED_REASONING_DISPLAY_ATTRIBUTE, False) is True


def required_tool_names_for_client(client: Any) -> tuple[str, ...]:
    raw = getattr(client, _REQUIRED_TOOL_NAMES_ATTRIBUTE, ())
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(name).strip() for name in raw if str(name).strip())


def effective_tools_for_client(
    client: Any,
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Return only tools the client's current transport state can send."""

    if getattr(client, "supports_tool_calling", True) is not False:
        return tools
    required = required_tool_names_for_client(client)
    if required:
        raise RequiredToolCallingUnsupportedError(
            "The selected model or provider does not support the required Computer Use tools "
            f"({', '.join(required)}). Choose a tool-capable model or provider."
        )
    return None


def count_input_tokens_if_supported(
    *,
    client: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any | None = None,
    request_context: Callable[[], AbstractContextManager[None]] | None = None,
) -> InputTokenCount | None:
    contract = getattr(client, "usage_contract", None)
    if not isinstance(contract, UsageContract) or not contract.supports_input_token_count:
        return None
    counter = getattr(client, "count_input_tokens", None)
    if not callable(counter):
        return None
    if getattr(counter, _SCOPED_TOKEN_COUNTER_ATTRIBUTE, False):
        token = _token_count_request_context.set(request_context)
        try:
            result = counter(messages=messages, tools=tools, tool_choice=tool_choice)
        finally:
            _token_count_request_context.reset(token)
    else:
        with request_context() if request_context is not None else nullcontext():
            result = counter(messages=messages, tools=tools, tool_choice=tool_choice)
    return result if isinstance(result, InputTokenCount) else None
