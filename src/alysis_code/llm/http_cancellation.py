"""Cancellation boundary for one owned synchronous HTTP request.

Native provider clients create a short-lived ``httpx.Client`` per call. Merely
closing an ``httpx.Response`` from another thread is not enough to interrupt a
blocked SSL read on every supported OS. This module therefore arms the owned
client's network backend before the request starts and shuts down its live
sockets when cancellation fires. Response/client close remain as fallbacks for
custom transports and normal httpx bookkeeping.
"""

from __future__ import annotations

import select
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpcore
import httpx

from ..cancellation import CooperativeCancellationError

_LEGACY_TOKEN_OBSERVATION_SECONDS = 0.05


def _shutdown_stream(stream: Any) -> None:
    """Interrupt a blocking socket operation without invalidating its descriptor.

    On macOS in particular, ``socket.close()`` in one thread need not wake a
    concurrent SSL ``recv()`` in another. ``shutdown(SHUT_RDWR)`` is the
    cross-thread interruption boundary; closing afterward releases ownership.
    This is deliberately best-effort because an I/O failure may already have
    retired the socket. The owner must defer close until in-flight I/O returns.
    """

    get_extra_info = getattr(stream, "get_extra_info", None)
    raw_socket = None
    if callable(get_extra_info):
        try:
            raw_socket = get_extra_info("socket")
        except Exception:
            raw_socket = None
    shutdown = getattr(raw_socket, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown(socket.SHUT_RDWR)
        except Exception:
            pass


def _shutdown_then_close(stream: Any) -> None:
    _shutdown_stream(stream)
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class _InterruptibleNetworkStream:
    """Track one httpcore stream and make cross-thread interruption reliable."""

    def __init__(self, owner: _InterruptibleNetworkBackend, stream: Any) -> None:
        self._owner = owner
        self._stream = stream
        self._state_lock = threading.Lock()
        self._interrupted = False
        self._closed = False
        self._close_requested = False
        self._active_io = 0
        self._tls_socket: socket.socket | None = None

    def _begin_io(self) -> Any:
        with self._state_lock:
            if self._interrupted or self._closed:
                raise OSError("HTTP request cancelled or closed")
            self._active_io += 1
            return self._stream

    def _end_io(self) -> None:
        with self._state_lock:
            self._active_io -= 1
            close_requested = self._close_requested
        if close_requested:
            self.close()

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        stream = self._begin_io()
        try:
            raw = stream.get_extra_info("socket")
            if isinstance(raw, socket.socket):
                return self._socket_io(raw, max_bytes, timeout=timeout)
            return stream.read(max_bytes, timeout=timeout)
        finally:
            self._end_io()

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        stream = self._begin_io()
        try:
            raw = stream.get_extra_info("socket")
            if isinstance(raw, socket.socket):
                self._socket_io(raw, buffer, timeout=timeout)
                return
            stream.write(buffer, timeout=timeout)
        finally:
            self._end_io()

    def _socket_io(self, raw: socket.socket, data: int | bytes, *, timeout: float | None) -> bytes:
        """Bound socket waits without abandoning a blocked read on Windows.

        TLS recv/send, like the handshake, may ignore cross-thread shutdown on
        Windows. Nonblocking operations preserve SSL buffering and partial writes;
        select only waits for the direction requested by OpenSSL. Cancellation
        and the original operation deadline are checked between bounded waits.
        """
        reading = isinstance(data, int)
        pending = memoryview(b"" if reading else data)
        original_timeout = raw.gettimeout()
        until = None if timeout is None else time.monotonic() + timeout
        raw.setblocking(False)
        try:
            while True:
                with self._state_lock:
                    if self._interrupted:
                        raise OSError("HTTP request cancelled")
                try:
                    if reading:
                        return raw.recv(data)
                    if not pending:
                        return b""
                    sent = raw.send(pending)
                    if not sent:
                        raise OSError("HTTP socket closed during write")
                    pending = pending[sent:]
                    continue
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError) as exc:
                    wants_read = isinstance(exc, ssl.SSLWantReadError) or (
                        isinstance(exc, BlockingIOError) and reading
                    )
                    remaining = None if until is None else until - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError("HTTP socket operation timed out") from exc
                    wait = 0.05 if remaining is None else min(0.05, remaining)
                    self._wait_socket_ready(raw, wants_read, wait)
        except TimeoutError as exc:
            error = httpcore.ReadTimeout if reading else httpcore.WriteTimeout
            raise error(str(exc)) from exc
        except OSError as exc:
            error = httpcore.ReadError if reading else httpcore.WriteError
            raise error(str(exc)) from exc
        finally:
            raw.settimeout(original_timeout)

    @staticmethod
    def _wait_socket_ready(raw: socket.socket, reading: bool, wait: float) -> None:
        select.select([raw] if reading else [], [] if reading else [raw], [], wait)

    def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> _InterruptibleNetworkStream:
        stream = self._begin_io()
        try:
            return self._start_tls(stream, ssl_context, server_hostname, timeout)
        finally:
            self._end_io()

    def _start_tls(
        self,
        stream: Any,
        ssl_context: Any,
        server_hostname: str | None,
        timeout: float | None,
    ) -> _InterruptibleNetworkStream:
        # httpcore's SSL wrapping detaches its raw socket before the handshake
        # completes. Publish the actual SSL socket before any blocking handshake
        # work, so cancellation has a live resource to shutdown on every OS.
        owner = self

        class HandshakeContext:
            def __getattr__(self, name: str) -> Any:
                return getattr(ssl_context, name)

            def wrap_socket(self, *args: Any, **kwargs: Any) -> Any:
                handshake = kwargs.pop("do_handshake_on_connect", True)
                wrapped = ssl_context.wrap_socket(*args, do_handshake_on_connect=False, **kwargs)
                with owner._state_lock:
                    owner._tls_socket = wrapped
                    interrupted = owner._interrupted
                try:
                    if interrupted:
                        wrapped.shutdown(socket.SHUT_RDWR)
                    if handshake:
                        owner._complete_tls_handshake(wrapped)
                    return wrapped
                except BaseException:
                    wrapped.close()
                    raise

        try:
            tls_stream = stream.start_tls(
                HandshakeContext(),
                server_hostname=server_hostname,
                timeout=timeout,
            )
            with self._state_lock:
                self._stream = tls_stream
                interrupted = self._interrupted
        finally:
            with self._state_lock:
                self._tls_socket = None
        if interrupted:
            _shutdown_then_close(tls_stream)
        return self

    def _complete_tls_handshake(self, wrapped: ssl.SSLSocket) -> None:
        # Some Windows SSL builds do not wake a pending handshake on shutdown.
        # Nonblocking handshake steps retain the original total connect bound,
        # while bounded select waits make cancellation observable without a
        # detached handshake worker that could survive the request.
        original_timeout = wrapped.gettimeout()
        until = None if original_timeout is None else time.monotonic() + original_timeout
        wrapped.setblocking(False)
        try:
            while True:
                with self._state_lock:
                    if self._interrupted:
                        raise OSError("TLS handshake cancelled")
                try:
                    wrapped.do_handshake()
                    return
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError) as exc:
                    remaining = None if until is None else until - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError("TLS handshake timed out") from exc
                    wait = 0.05 if remaining is None else min(0.05, remaining)
                    reading = isinstance(exc, ssl.SSLWantReadError)
                    select.select(
                        [wrapped] if reading else [], [] if reading else [wrapped], [], wait
                    )
        finally:
            wrapped.settimeout(original_timeout)

    def get_extra_info(self, info: str) -> Any:
        with self._state_lock:
            stream = self._stream
        return stream.get_extra_info(info)

    def close(self) -> None:
        with self._state_lock:
            self._close_requested = True
            if self._closed or self._active_io:
                return
            self._closed = True
            stream = self._stream
        try:
            stream.close()
        finally:
            self._owner.forget(self)

    def interrupt(self) -> None:
        with self._state_lock:
            self._interrupted = True
            self._close_requested = True
            stream = self._stream
            tls_socket = self._tls_socket
        try:
            if tls_socket is not None:
                try:
                    tls_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            # Still attempt shutdown if a concurrent ordinary close already
            # started. That close is precisely the operation that may leave a
            # read blocked on platforms where close alone is not an interrupt.
            # macOS can leave a timeout-backed recv waiting if another thread
            # closes its descriptor before the shutdown readiness is consumed.
            # Keep it valid until the owned read/write/TLS operation retires.
            _shutdown_stream(stream)
        finally:
            self.close()


class _InterruptibleNetworkBackend:
    """Delegate connects while retaining the streams owned by one client."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self._state_lock = threading.Lock()
        self._streams: set[_InterruptibleNetworkStream] = set()
        self._interrupted = False

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> _InterruptibleNetworkStream:
        stream = self._backend.connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        return self._track(stream)

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> _InterruptibleNetworkStream:
        stream = self._backend.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )
        return self._track(stream)

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)

    def _track(self, stream: Any) -> _InterruptibleNetworkStream:
        tracked = _InterruptibleNetworkStream(self, stream)
        with self._state_lock:
            interrupted = self._interrupted
            if not interrupted:
                self._streams.add(tracked)
        if interrupted:
            tracked.interrupt()
        return tracked

    def forget(self, stream: _InterruptibleNetworkStream) -> None:
        with self._state_lock:
            self._streams.discard(stream)

    def interrupt(self) -> None:
        with self._state_lock:
            self._interrupted = True
            streams = tuple(self._streams)
        for stream in streams:
            stream.interrupt()


def _client_transports(client: httpx.Client) -> Iterator[Any]:
    """Yield each distinct transport configured on an httpx client."""

    seen: set[int] = set()
    transports = [getattr(client, "_transport", None)]
    mounts = getattr(client, "_mounts", None)
    if isinstance(mounts, dict):
        transports.extend(mounts.values())
    for transport in transports:
        if transport is None or id(transport) in seen:
            continue
        seen.add(id(transport))
        yield transport


def _arm_client_network_backends(client: httpx.Client) -> tuple[_InterruptibleNetworkBackend, ...]:
    """Install interruptible backends before an owned client's first request.

    httpx does not currently expose its httpcore network backend as public
    configuration. Its synchronous ``HTTPTransport`` does consistently own a
    pool with ``_network_backend`` (httpcore is a direct, bounded dependency of
    this project), so the narrow adaptation stays here at the ownership
    boundary. Unknown/custom transports are left untouched and retain the
    response/client-close fallback.
    """

    backends: list[_InterruptibleNetworkBackend] = []
    seen: set[int] = set()
    for transport in _client_transports(client):
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if backend is None:
            continue
        if isinstance(backend, _InterruptibleNetworkBackend):
            interruptible = backend
        else:
            interruptible = _InterruptibleNetworkBackend(backend)
            try:
                pool._network_backend = interruptible  # noqa: SLF001
            except Exception:
                continue
        if id(interruptible) not in seen:
            seen.add(id(interruptible))
            backends.append(interruptible)
    return tuple(backends)


class _OwnedClientAbort:
    """Abort live network I/O and then retire httpx-owned resources."""

    def __init__(self, client: httpx.Client, *, arm_network: bool) -> None:
        self._client = client
        self._backends = _arm_client_network_backends(client) if arm_network else ()

    def __call__(self, response: httpx.Response | None = None) -> None:
        for backend in self._backends:
            try:
                backend.interrupt()
            except Exception:
                pass
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        try:
            self._client.close()
        except Exception:
            pass


class _RequestAbortBoundary:
    """Bind one token to the currently blocking HTTP resource.

    Native interactive tokens expose a scalar abort callback and newer tokens
    may expose cancellation subscribers. Older event-backed tokens expose only
    ``wait``; a short-lived observer bridges those without imposing an overall
    timeout on the provider call. The observer is used only when neither
    callback mechanism exists and is retired as soon as the request finishes.
    """

    def __init__(self, token: Any | None, target: Callable[[], None]) -> None:
        self._token = token
        self._target_lock = threading.Lock()
        self._target: Callable[[], None] | None = target
        self._stop = threading.Event()
        self._clear_abort = getattr(token, "clear_abort_callback", None)
        self._unsubscribe: Callable[[], None] | None = None
        self._observer: threading.Thread | None = None

        set_abort = getattr(token, "set_abort_callback", None)
        if callable(set_abort):
            set_abort(self._abort_current)
            return
        subscribe = getattr(token, "subscribe", None)
        if callable(subscribe):
            unsubscribe = subscribe(self._abort_current)
            if callable(unsubscribe):
                self._unsubscribe = unsubscribe
            return
        wait_for_cancel = getattr(token, "wait", None)
        if callable(wait_for_cancel):
            self._observer = threading.Thread(
                target=self._observe_legacy_token,
                args=(wait_for_cancel,),
                name="alysis-http-cancellation",
                daemon=True,
            )
            self._observer.start()

    def replace_target(self, target: Callable[[], None]) -> None:
        with self._target_lock:
            self._target = target

    def close(self) -> None:
        self._stop.set()
        if callable(self._clear_abort):
            self._clear_abort()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        with self._target_lock:
            self._target = None

    def _observe_legacy_token(self, wait_for_cancel: Callable[[float], object]) -> None:
        while not self._stop.is_set():
            if bool(wait_for_cancel(_LEGACY_TOKEN_OBSERVATION_SECONDS)):
                self._abort_current()
                return

    def _abort_current(self) -> None:
        with self._target_lock:
            target = self._target
        if target is None:
            return
        try:
            target()
        except Exception:
            # Cancellation remains observable even when a transport is already
            # closed or its close hook fails.
            pass


def raise_if_cancelled(cancellation_token: Any | None) -> None:
    """Raise the token's native cancellation outcome when it is cancelled."""

    if cancellation_token is None or not bool(getattr(cancellation_token, "is_cancelled", False)):
        return
    throw_if_cancelled = getattr(cancellation_token, "throw_if_cancelled", None)
    if callable(throw_if_cancelled):
        throw_if_cancelled("cancelled_by_user")
    # Minimal external tokens may expose only ``is_cancelled``. Interactive
    # provider cancellation retains the established host control-flow type.
    raise KeyboardInterrupt("cancelled_by_user")


@contextmanager
def cancellable_httpx_request(
    *,
    client: httpx.Client,
    cancellation_token: Any | None,
    method: str,
    url: str,
    stream: bool,
    **request_kwargs: Any,
) -> Iterator[httpx.Response]:
    """Yield one response while pre-header and response-body reads are abortable.

    ``client`` must be owned by the surrounding provider call. Cancellation can
    therefore shut down its live sockets and close it safely while the request
    is waiting for response headers. For a streaming response the callback is
    replaced, without an unregistered gap, with one that also closes the
    response after interrupting the transport.
    """

    owned_client_abort = _OwnedClientAbort(
        client,
        arm_network=cancellation_token is not None,
    )
    abort_boundary = _RequestAbortBoundary(cancellation_token, owned_client_abort)
    try:
        raise_if_cancelled(cancellation_token)
        if stream:
            with client.stream(method, url, **request_kwargs) as response:
                abort_boundary.replace_target(lambda: owned_client_abort(response))
                raise_if_cancelled(cancellation_token)
                yield response
                raise_if_cancelled(cancellation_token)
        else:
            response = client.request(method, url, **request_kwargs)
            raise_if_cancelled(cancellation_token)
            yield response
            raise_if_cancelled(cancellation_token)
    except CooperativeCancellationError:
        # Protected transports already scrubbed their cancellation outcome.
        # Preserve it without chaining a second exception to the private request.
        raise
    except Exception:
        # Closing a live transport can surface through provider-specific httpx
        # exceptions. Once the token fired, its reason is the truthful outcome.
        raise_if_cancelled(cancellation_token)
        raise
    finally:
        abort_boundary.close()


def cancellable_httpx_send(
    *,
    client: httpx.Client,
    cancellation_token: Any | None,
    method: str,
    url: str,
    **request_kwargs: Any,
) -> httpx.Response:
    """Send one non-streaming request through the same cancellation boundary."""

    with cancellable_httpx_request(
        client=client,
        cancellation_token=cancellation_token,
        method=method,
        url=url,
        stream=False,
        **request_kwargs,
    ) as response:
        return response


__all__ = [
    "cancellable_httpx_request",
    "cancellable_httpx_send",
    "raise_if_cancelled",
]
