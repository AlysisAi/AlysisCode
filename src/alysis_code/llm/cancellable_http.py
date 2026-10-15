"""Cancellation-aware HTTPX requests for protected provider turns.

The main agent API is synchronous, while HTTPX's synchronous transport cannot
reliably interrupt a blocking socket read from another thread.  Protected
Computer Use turns therefore run their non-public-stream provider request on
an async transport in a short-lived, joined thread.  Cancelling the request
cancels the async task and closes its client before the synchronous caller is
released.

Ordinary provider calls do not use this module and retain their existing
synchronous connection behavior.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any

import httpx

from ..cancellation import CooperativeCancellationError
from ..task_budget import current_task_budget
from ..task_budget import need as budget_need

_CANCELLATION_POLL_SECONDS = 0.025
_CANCELLED = object()


class CancellationTransportUnsupportedError(RuntimeError):
    """The configured transport cannot provide prompt in-flight cancellation."""


def _is_cancelled(cancellation_token: Any) -> bool:
    try:
        return bool(getattr(cancellation_token, "is_cancelled", False))
    except Exception:
        return False


def _raise_cancelled(cancellation_token: Any) -> None:
    throw_if_cancelled = getattr(cancellation_token, "throw_if_cancelled", None)
    if callable(throw_if_cancelled):
        try:
            throw_if_cancelled("cancelled_by_user")
        except CooperativeCancellationError as exc:
            exc.__traceback__ = None
            exc.__cause__ = None
            exc.__context__ = None
            raise exc from None
    if _is_cancelled(cancellation_token):
        raise CooperativeCancellationError("cancelled_by_user") from None
    raise CooperativeCancellationError("cancelled_by_user") from None


def _scrub_httpx_message(value: Any) -> None:
    """Erase request material retained by an HTTPX request/response object."""

    request = getattr(value, "request", None)
    if request is not None and request is not value:
        _scrub_httpx_message(request)

    headers = getattr(value, "headers", None)
    clear_headers = getattr(headers, "clear", None)
    if callable(clear_headers):
        try:
            clear_headers()
        except Exception:
            pass

    extensions = getattr(value, "extensions", None)
    if isinstance(extensions, dict):
        extensions.clear()

    if hasattr(value, "_content"):
        try:
            value._content = b""  # noqa: SLF001 - privacy scrub of HTTPX state
        except Exception:
            pass

    stream = getattr(value, "stream", None)
    if hasattr(stream, "_stream"):
        try:
            stream._stream = b""  # noqa: SLF001 - privacy scrub of HTTPX state
        except Exception:
            pass

    url = getattr(value, "url", None)
    copy_with = getattr(url, "copy_with", None)
    if callable(copy_with):
        try:
            value.url = copy_with(query=None)
        except Exception:
            pass


def _scrub_exception_transport_state(error: BaseException) -> None:
    stack: list[BaseException] = [error]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        cause = current.__cause__
        context = current.__context__
        if cause is not None:
            stack.append(cause)
        if context is not None:
            stack.append(context)
        _scrub_httpx_message(getattr(current, "request", None))
        _scrub_httpx_message(getattr(current, "response", None))
        current.args = ("protected provider request cancelled",)
        current.__traceback__ = None
        current.__cause__ = None
        current.__context__ = None


@dataclass
class _RequestOutcome:
    value: Any = None
    error: BaseException | None = None


async def _perform_request(
    *,
    timeout_s: float | httpx.Timeout,
    transport: httpx.AsyncBaseTransport | None,
    cancellation_token: Any,
    method: str,
    url: str,
    request_kwargs: dict[str, Any],
) -> object:
    captured_requests: list[httpx.Request] = []
    budget = current_task_budget()

    async def _capture_request(request: httpx.Request) -> None:
        captured_requests.append(request)

    try:
        async with httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport or (httpx.AsyncHTTPTransport(retries=0) if budget else None),
            follow_redirects=False,
            **({"trust_env": False} if budget else {}),
            event_hooks={"request": [_capture_request]},
        ) as client:

            async def send() -> httpx.Response:
                if budget is None:
                    return await client.request(method, url, **request_kwargs)
                request = client.build_request(method, url, **request_kwargs)
                budget_need(not request.url.username and not request.url.password)
                body = await request.aread()
                attempt = budget.begin(
                    method=request.method,
                    url=str(request.url),
                    headers=list(request.headers.raw),
                    body=bytes(body),
                )
                try:
                    budget.check(lease=True)
                    if _is_cancelled(cancellation_token):
                        _raise_cancelled(cancellation_token)
                    # No callbacks, redirects, auth handlers or connection retries
                    # may substitute a request after this exact durable ACK.
                    return await client.send(request, auth=httpx.Auth(), follow_redirects=False)
                finally:
                    budget.settled(attempt)

            request_task = asyncio.create_task(
                send(),
                name="alysis-provider-request",
            )
            while True:
                if _is_cancelled(cancellation_token):
                    request_task.cancel()
                    try:
                        response = await request_task
                    except BaseException as exc:  # cancellation may be BaseException
                        _scrub_exception_transport_state(exc)
                    else:
                        _scrub_httpx_message(response)
                    for request in captured_requests:
                        _scrub_httpx_message(request)
                    return _CANCELLED

                done, _pending = await asyncio.wait(
                    {request_task},
                    timeout=_CANCELLATION_POLL_SECONDS,
                )
                if not done:
                    continue

                if _is_cancelled(cancellation_token):
                    try:
                        response = request_task.result()
                    except BaseException as exc:
                        _scrub_exception_transport_state(exc)
                    else:
                        _scrub_httpx_message(response)
                    for request in captured_requests:
                        _scrub_httpx_message(request)
                    return _CANCELLED
                return request_task.result()
    finally:
        if _is_cancelled(cancellation_token):
            for request in captured_requests:
                _scrub_httpx_message(request)


def _run_request_in_joined_thread(
    *,
    timeout_s: float | httpx.Timeout,
    transport: httpx.AsyncBaseTransport | None,
    cancellation_token: Any,
    method: str,
    url: str,
    request_kwargs: dict[str, Any],
) -> object:
    outcomes: queue.SimpleQueue[_RequestOutcome] = queue.SimpleQueue()

    def _runner() -> None:
        try:
            value = asyncio.run(
                _perform_request(
                    timeout_s=timeout_s,
                    transport=transport,
                    cancellation_token=cancellation_token,
                    method=method,
                    url=url,
                    request_kwargs=request_kwargs,
                )
            )
        except BaseException as exc:
            outcomes.put(_RequestOutcome(error=exc))
        else:
            outcomes.put(_RequestOutcome(value=value))

    context = copy_context()
    worker = threading.Thread(
        target=lambda: context.run(_runner),
        name="alysis-provider-http",
        daemon=False,
    )
    worker.start()
    worker.join()
    outcome = outcomes.get()
    if outcome.error is not None:
        if _is_cancelled(cancellation_token):
            _scrub_exception_transport_state(outcome.error)
            return _CANCELLED
        raise outcome.error
    return outcome.value


class CancellationAwareHTTPXClient:
    """Small synchronous facade over a cancellation-aware AsyncClient."""

    def __init__(
        self,
        *,
        timeout_s: float | httpx.Timeout,
        transport: httpx.AsyncBaseTransport | None,
        cancellation_token: Any,
    ) -> None:
        self._timeout_s = timeout_s
        self._transport = transport
        self._cancellation_token = cancellation_token

    def __enter__(self) -> CancellationAwareHTTPXClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def close(self) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        request_kwargs = dict(kwargs)
        try:
            if _is_cancelled(self._cancellation_token):
                request_kwargs.clear()
                _raise_cancelled(self._cancellation_token)
            outcome = _run_request_in_joined_thread(
                timeout_s=self._timeout_s,
                transport=self._transport,
                cancellation_token=self._cancellation_token,
                method=str(method),
                url=str(url),
                request_kwargs=request_kwargs,
            )
        finally:
            request_kwargs.clear()

        if outcome is _CANCELLED or _is_cancelled(self._cancellation_token):
            if isinstance(outcome, httpx.Response):
                _scrub_httpx_message(outcome)
            _raise_cancelled(self._cancellation_token)
        if not isinstance(outcome, httpx.Response):
            raise RuntimeError("Cancellation-aware HTTP request returned no response")
        return outcome

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    @contextmanager
    def stream(self, method: str, url: str, **kwargs: Any) -> Iterator[httpx.Response]:
        # The async request is intentionally buffered.  This path is used only
        # when a provider-auth adapter requires SSE while the protected caller
        # has disabled public streaming.
        response = self.request(method, url, **kwargs)
        # AsyncClient.request() has already buffered the body and closed its
        # AsyncByteStream. Calling Response.close() here would incorrectly use
        # the synchronous close API on that async stream.
        yield response


def cancellation_aware_httpx_client(
    *,
    timeout_s: float | httpx.Timeout,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None,
    cancellation_token: Any,
) -> CancellationAwareHTTPXClient:
    """Build a client that cannot silently fall back to blocking sync I/O."""

    budget = current_task_budget()
    if budget is not None:
        budget.check()
        original_token = cancellation_token

        class BudgetCancellation:
            @property
            def is_cancelled(self) -> bool:
                return budget.is_cancelled or _is_cancelled(original_token)

            def throw_if_cancelled(self, reason: str) -> None:
                budget.check()
                if _is_cancelled(original_token):
                    _raise_cancelled(original_token)

        cancellation_token = BudgetCancellation()
        timeout = httpx.Timeout(timeout_s)
        remaining = budget.remaining()
        timeout_s = httpx.Timeout(
            **{
                key: min(value, remaining) if value is not None else remaining
                for key, value in timeout.as_dict().items()
            }
        )
    if _is_cancelled(cancellation_token):
        _raise_cancelled(cancellation_token)
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        if _is_cancelled(cancellation_token):
            _raise_cancelled(cancellation_token)
        raise CancellationTransportUnsupportedError(
            "The configured provider transport does not support in-flight cancellation"
        )
    return CancellationAwareHTTPXClient(
        timeout_s=timeout_s,
        transport=transport,
        cancellation_token=cancellation_token,
    )


def provider_httpx_client(*, timeout: Any, transport: Any = None) -> Any:
    """All builtin provider HTTP paths, including count/cache/inner fallbacks.

    Legacy calls retain HTTPX behavior. Protected envelope calls have the same
    cancellable prepared-request gate even when an auxiliary API has no token.
    """
    budget = current_task_budget()
    if budget is None:
        return httpx.Client(timeout=timeout, transport=transport)
    return cancellation_aware_httpx_client(
        timeout_s=timeout, transport=transport, cancellation_token=budget
    )
