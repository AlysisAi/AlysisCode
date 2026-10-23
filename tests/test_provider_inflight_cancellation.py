from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable
from time import perf_counter
from typing import Any

import httpx
import pytest

from alysis_code.agent.llm_calls import _main_agent_chat
from alysis_code.cancellation import (
    CooperativeCancellationError,
    EventCancellationToken,
)
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.cancellable_http import (
    CancellationTransportUnsupportedError,
    cancellation_aware_httpx_client,
)
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.provider_limits import (
    ProviderRetrySettings,
    run_provider_limited_call,
)
from alysis_code.llm.types import LLMError, LLMResponse
from alysis_code.provider_auth import ProviderRequestAuthorization
from alysis_code.provider_auth import openai_codex as openai_codex_mod
from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth
from alysis_code.provider_auth.store import ProviderTokenRecord

_PRIVATE_WINDOW = "Private Window 8BA047"
_PRIVATE_SNAPSHOT = "snapshot_private_9F6D2A"


class _ConcreteCancellationError(CooperativeCancellationError):
    pass


class _ConcreteCancellationToken(EventCancellationToken):
    error_class = _ConcreteCancellationError


class _BlockingAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.request: httpx.Request | None = None
        self.request_count = 0
        self.stream_requested = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        self.request = request
        try:
            body = json.loads(request.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        self.stream_requested = body.get("stream") is True
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("blocking transport unexpectedly resumed")

    async def aclose(self) -> None:
        self.closed.set()


class _SyncOnlyTransport(httpx.BaseTransport):
    def handle_request(self, _request: httpx.Request) -> httpx.Response:
        raise AssertionError("sync-only transport must not receive a protected request")


def _read_http_request(connection: socket.socket) -> None:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            return
        data.extend(chunk)
    raw_headers, body = bytes(data).split(b"\r\n\r\n", 1)
    content_length = 0
    for line in raw_headers.split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        if separator and name.strip().lower() == b"content-length":
            content_length = int(value.strip())
            break
    while len(body) < content_length:
        chunk = connection.recv(65536)
        if not chunk:
            return
        body += chunk


def _wait_for_peer_close(connection: socket.socket) -> bool:
    connection.settimeout(2.0)
    try:
        while connection.recv(65536):
            pass
    except ConnectionResetError:
        return True
    return True


class _CancellationAwareStreamingAuth:
    requires_streaming = True
    supports_previous_response_id = False

    def __init__(self) -> None:
        self.received_token: Any | None = None

    def route_credential_scope(self) -> str:
        return "test-codex-scope"

    def authorize_request(
        self,
        _url: str,
        *,
        force_refresh: bool = False,
        session_id: str | None = None,
        expected_credential_scope: str | None = None,
        cancellation_token: Any | None = None,
    ) -> ProviderRequestAuthorization:
        del force_refresh, session_id
        assert expected_credential_scope == "test-codex-scope"
        self.received_token = cancellation_token
        return ProviderRequestAuthorization(
            credential_scope="test-codex-scope",
            headers={"Authorization": "Bearer subscription-token"},
        )

    def adapt_responses_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload)


def _openai_client(transport: httpx.AsyncBaseTransport) -> OpenAIResponsesClient:
    return OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="private-openai-key",
        model="gpt-5.4",
        transport=transport,  # type: ignore[arg-type]
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
    )


def _openai_compat_client(transport: httpx.AsyncBaseTransport) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://compat.example/v1",
        api_key="private-compat-key",
        model="compat-tool-model",
        transport=transport,  # type: ignore[arg-type]
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
    )


def _anthropic_client(transport: httpx.AsyncBaseTransport) -> AnthropicMessagesClient:
    return AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="private-anthropic-key",
        model="claude-sonnet-4-6",
        transport=transport,  # type: ignore[arg-type]
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
    )


def _openai_internal_sse_client(
    transport: httpx.AsyncBaseTransport,
) -> OpenAIResponsesClient:
    return OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="",
        model="gpt-5.4",
        transport=transport,  # type: ignore[arg-type]
        provider_auth=_CancellationAwareStreamingAuth(),  # type: ignore[arg-type]
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
    )


def _gemini_client(transport: httpx.AsyncBaseTransport) -> GeminiGenerateContentClient:
    return GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="private-gemini-key",
        model="gemini-3-flash-preview",
        transport=transport,  # type: ignore[arg-type]
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
    )


@pytest.mark.parametrize(
    ("client_factory", "expects_internal_stream"),
    [
        (_openai_client, False),
        (_openai_compat_client, False),
        (_openai_internal_sse_client, True),
        (_anthropic_client, False),
        (_gemini_client, False),
    ],
    ids=[
        "openai-responses",
        "openai-compatible",
        "openai-auth-forced-internal-sse",
        "anthropic-messages",
        "gemini-generate-content",
    ],
)
def test_provider_request_is_aborted_and_scrubbed_on_cancellation(
    client_factory: Callable[[httpx.AsyncBaseTransport], Any],
    expects_internal_stream: bool,
) -> None:
    transport = _BlockingAsyncTransport()
    client = client_factory(transport)
    cancellation_event = threading.Event()
    token = _ConcreteCancellationToken(cancellation_event)
    errors: list[BaseException] = []

    def _chat() -> None:
        try:
            client.chat(
                messages=[
                    {
                        "role": "user",
                        "content": f"window={_PRIVATE_WINDOW} snapshot={_PRIVATE_SNAPSHOT}",
                    }
                ],
                stream=False,
                cancellation_token=token,
            )
        except BaseException as exc:  # cancellation is the asserted outcome
            errors.append(exc)

    caller = threading.Thread(target=_chat, name="provider-cancellation-test")
    caller.start()
    assert transport.started.wait(2.0), "provider request never reached the transport"

    cancelled_at = perf_counter()
    cancellation_event.set()
    caller.join(2.0)
    elapsed = perf_counter() - cancelled_at

    assert not caller.is_alive(), "provider call did not return promptly after cancellation"
    assert elapsed < 1.0
    assert len(errors) == 1
    error = errors[0]
    assert type(error) is _ConcreteCancellationError
    assert error.__cause__ is None
    assert error.__context__ is None
    assert transport.cancelled.is_set()
    assert transport.closed.is_set()
    assert transport.request_count == 1
    assert transport.stream_requested is expects_internal_stream
    assert not any(
        thread.name == "alysis-provider-http" and thread.is_alive()
        for thread in threading.enumerate()
    )

    request = transport.request
    assert request is not None
    assert request.content == b""
    assert not request.headers
    assert request.url.query == b""
    assert _PRIVATE_WINDOW.encode() not in request.content
    assert _PRIVATE_SNAPSHOT.encode() not in request.content


@pytest.mark.parametrize("request_surface", ["helper", "openai_compat"])
@pytest.mark.parametrize("stall_stage", ["response_headers", "response_body"])
def test_real_http_socket_is_aborted_before_complete_response(
    stall_stage: str,
    request_surface: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stalled = threading.Event()
    peer_closed = threading.Event()
    server_errors: list[BaseException] = []

    def _serve() -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                _read_http_request(connection)
                if stall_stage == "response_body":
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: 100000\r\n"
                        b"Connection: close\r\n\r\n"
                        b'{"partial":"'
                    )
                stalled.set()
                if _wait_for_peer_close(connection):
                    peer_closed.set()
        except BaseException as exc:
            server_errors.append(exc)

    server = threading.Thread(target=_serve, name=f"provider-stall-{stall_stage}")
    server.start()
    cancellation_event = threading.Event()
    token = _ConcreteCancellationToken(cancellation_event)
    errors: list[BaseException] = []

    def _request() -> None:
        try:
            base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            if request_surface == "openai_compat":
                client = OpenAICompatClient(
                    base_url=f"{base_url}/v1",
                    api_key="private-compat-key",
                    model="compat-tool-model",
                    timeout_s=30.0,
                    provider_retry_settings=ProviderRetrySettings(max_retries=2),
                )
                client.chat(
                    messages=[{"role": "user", "content": _PRIVATE_SNAPSHOT}],
                    stream=False,
                    cancellation_token=token,
                )
            else:
                with cancellation_aware_httpx_client(
                    timeout_s=30.0,
                    transport=None,
                    cancellation_token=token,
                ) as client:
                    client.post(
                        f"{base_url}/responses",
                        json={"private_snapshot": _PRIVATE_SNAPSHOT},
                    )
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=_request, name=f"provider-call-{stall_stage}")
    caller.start()
    assert stalled.wait(2.0), f"server never reached stalled {stall_stage} state"
    cancelled_at = perf_counter()
    cancellation_event.set()
    caller.join(2.0)
    elapsed = perf_counter() - cancelled_at
    server.join(2.0)
    listener.close()

    assert not caller.is_alive()
    assert not server.is_alive()
    assert elapsed < 1.0
    assert len(errors) == 1
    assert type(errors[0]) is _ConcreteCancellationError
    assert peer_closed.is_set()
    assert not server_errors
    assert not any(
        thread.name == "alysis-provider-http" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_openai_auth_forced_internal_sse_succeeds_through_buffered_async_facade() -> None:
    auth = _CancellationAwareStreamingAuth()
    token = _ConcreteCancellationToken(threading.Event())

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["stream"] is True
        completed = {
            "type": "response.completed",
            "response": {
                "id": "resp_internal_sse",
                "model": "gpt-5.4",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done."}],
                    }
                ],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            },
        }
        frame = f"event: response.completed\ndata: {json.dumps(completed)}\n\n"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=frame.encode("utf-8"),
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="",
        model="gpt-5.4",
        provider_auth=auth,  # type: ignore[arg-type]
        transport=httpx.MockTransport(_handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=0),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hello"}],
        stream=False,
        cancellation_token=token,
    )

    assert response.content == "Done."
    assert auth.received_token is token


def test_openai_subscription_token_refresh_is_cancelled_and_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _BlockingAsyncTransport()
    auth = OpenAICodexSubscriptionAuth(transport=transport)  # type: ignore[arg-type]
    record = ProviderTokenRecord(
        client_id="test-client",
        subject="test-subject",
        scopes=("chatgpt.tokens.use.direct",),
        access_token="expired-access-secret",
        refresh_token="private-refresh-secret",
        expires_at=time.time() - 60.0,
        account_id="account-1",
        credential_generation="cg_0123456789abcdef0123456789abcdef",
    )
    monkeypatch.setattr(openai_codex_mod, "load_provider_token", lambda _provider_id: record)

    cancellation_event = threading.Event()
    token = _ConcreteCancellationToken(cancellation_event)
    errors: list[BaseException] = []

    def _authorize() -> None:
        try:
            auth.authorize_request(
                "https://api.openai.com/v1/responses",
                cancellation_token=token,
            )
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=_authorize)
    caller.start()
    assert transport.started.wait(2.0), "credential refresh never reached the transport"
    cancellation_event.set()
    caller.join(2.0)

    assert not caller.is_alive()
    assert len(errors) == 1
    assert type(errors[0]) is _ConcreteCancellationError
    assert transport.cancelled.is_set()
    assert transport.closed.is_set()
    request = transport.request
    assert request is not None
    assert request.content == b""
    assert not request.headers
    assert b"private-refresh-secret" not in request.content


def test_main_agent_chat_does_not_strip_supported_token_after_internal_type_error() -> None:
    cancellation_token = object()

    class Client:
        supports_tool_calling = True

        def __init__(self) -> None:
            self.calls = 0
            self.received_token: object | None = None

        def chat(
            self,
            *,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
            stream: bool,
            on_text_delta: Any,
            cancellation_token: Any,
        ) -> LLMResponse:
            del messages, tools, stream, on_text_delta
            self.calls += 1
            self.received_token = cancellation_token
            raise TypeError("internal provider parser failure")

    client = Client()
    with pytest.raises(TypeError, match="internal provider parser failure"):
        _main_agent_chat(
            client=client,
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            stream=False,
            on_text_delta=None,
            cancellation_token=cancellation_token,
        )

    assert client.calls == 1
    assert client.received_token is cancellation_token


def test_main_agent_chat_omits_token_for_legacy_signature_without_retry() -> None:
    class Client:
        supports_tool_calling = True

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            *,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
            stream: bool,
            on_text_delta: Any,
        ) -> LLMResponse:
            del messages, tools, stream, on_text_delta
            self.calls += 1
            return LLMResponse(content="ok", tool_calls=[], raw={})

    client = Client()
    response = _main_agent_chat(
        client=client,
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        stream=False,
        on_text_delta=None,
        cancellation_token=object(),
    )

    assert response.content == "ok"
    assert client.calls == 1


def test_provider_retry_wait_is_interrupted_without_reissuing_request() -> None:
    cancellation_event = threading.Event()
    token = _ConcreteCancellationToken(cancellation_event)
    retry_started = threading.Event()
    calls = 0
    errors: list[BaseException] = []

    def _provider_call() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionResetError("transient transport failure")

    def _run() -> None:
        try:
            run_provider_limited_call(
                call=_provider_call,
                provider_key=None,
                retry_settings=ProviderRetrySettings(
                    max_retries=3,
                    base_delay_seconds=10.0,
                    max_delay_seconds=10.0,
                ),
                operation="cancellation-test",
                random_fn=lambda: 0.5,
                on_retry=lambda *_args: retry_started.set(),
                cancellation_token=token,
            )
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=_run)
    caller.start()
    assert retry_started.wait(2.0)
    cancellation_event.set()
    caller.join(2.0)

    assert not caller.is_alive()
    assert calls == 1
    assert len(errors) == 1
    assert type(errors[0]) is _ConcreteCancellationError


def test_provider_concurrency_wait_is_interrupted_before_request() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    first_errors: list[BaseException] = []

    def _hold_provider_slot() -> str:
        first_started.set()
        assert release_first.wait(2.0)
        return "done"

    def _run_first() -> None:
        try:
            run_provider_limited_call(
                call=_hold_provider_slot,
                provider_key="qwen",
                provider_concurrency_caps={"qwen": 1},
                retry_settings=ProviderRetrySettings(max_retries=0),
                operation="concurrency-holder",
            )
        except BaseException as exc:
            first_errors.append(exc)

    first = threading.Thread(target=_run_first)
    first.start()
    assert first_started.wait(2.0)

    cancellation_event = threading.Event()
    token = _ConcreteCancellationToken(cancellation_event)
    second_errors: list[BaseException] = []
    second_calls = 0
    second_attempting = threading.Event()
    second_finished = threading.Event()

    def _unexpected_second_call() -> None:
        nonlocal second_calls
        second_calls += 1

    def _run_second() -> None:
        second_attempting.set()
        try:
            run_provider_limited_call(
                call=_unexpected_second_call,
                provider_key="qwen",
                provider_concurrency_caps={"qwen": 1},
                retry_settings=ProviderRetrySettings(max_retries=0),
                operation="concurrency-waiter",
                cancellation_token=token,
            )
        except BaseException as exc:
            second_errors.append(exc)
        finally:
            second_finished.set()

    second = threading.Thread(target=_run_second)
    second.start()
    assert second_attempting.wait(2.0)
    assert not second_finished.wait(0.1), "second request did not wait for the provider slot"
    cancellation_event.set()
    second.join(2.0)
    release_first.set()
    first.join(2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not first_errors
    assert second_calls == 0
    assert len(second_errors) == 1
    assert type(second_errors[0]) is _ConcreteCancellationError


def test_sync_only_transport_fails_closed_instead_of_losing_cancellation() -> None:
    with pytest.raises(CancellationTransportUnsupportedError):
        cancellation_aware_httpx_client(
            timeout_s=20.0,
            transport=_SyncOnlyTransport(),
            cancellation_token=_ConcreteCancellationToken(threading.Event()),
        )


def test_openai_compat_sync_only_transport_fails_closed_without_retry() -> None:
    client = OpenAICompatClient(
        base_url="https://compat.example/v1",
        api_key="private-compat-key",
        model="compat-tool-model",
        transport=_SyncOnlyTransport(),
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
        provider_sleep_fn=lambda _seconds: pytest.fail("unsupported transport was retried"),
    )

    with pytest.raises(LLMError, match="does not support in-flight cancellation") as exc_info:
        client.chat(
            messages=[{"role": "user", "content": _PRIVATE_SNAPSHOT}],
            stream=False,
            cancellation_token=_ConcreteCancellationToken(threading.Event()),
        )

    assert isinstance(exc_info.value.__cause__, CancellationTransportUnsupportedError)


def test_pre_cancelled_sync_only_transport_preserves_concrete_cancellation() -> None:
    cancellation_event = threading.Event()
    cancellation_event.set()
    with pytest.raises(_ConcreteCancellationError):
        cancellation_aware_httpx_client(
            timeout_s=20.0,
            transport=_SyncOnlyTransport(),
            cancellation_token=_ConcreteCancellationToken(cancellation_event),
        )
