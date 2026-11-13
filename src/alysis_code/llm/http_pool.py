"""Reusable HTTP clients with exclusive, cancellation-safe request ownership."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from .http_cancellation import _arm_client_network_backends


class ProviderHTTPPool:
    """Keep a small idle pool; never lend one client to concurrent requests.

    Cancelling an owned request may close all of its client's sockets. Exclusive
    leases preserve that contract even for HTTP/2. Protected async requests and
    caller-supplied transports retain their separate ownership paths.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: list[httpx.Client] = []
        self._scope: str | None = None
        self._generation = 0
        self._closed = False

    @contextmanager
    def lease(self, *, scope: str, timeout: httpx.Timeout) -> Iterator[httpx.Client]:
        stale: list[httpx.Client] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("Provider HTTP pool is closed")
            if self._scope != scope:
                stale, self._idle = self._idle, []
                self._scope = scope
                self._generation += 1
            generation = self._generation
            client = self._idle.pop() if self._idle else None
        for old in stale:
            old.close()
        if client is None:
            client = httpx.Client(
                http2=True,
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=1, max_keepalive_connections=1, keepalive_expiry=60
                ),
            )
            # Arm before the first request, including requests without a token:
            # a later lease may be cancellable and reuse this same socket.
            _arm_client_network_backends(client)
        else:
            # Request deadlines can change per turn; they must not defeat reuse.
            client.timeout = timeout
        healthy = False
        try:
            yield client
            healthy = True
        finally:
            with self._lock:
                reuse = (
                    healthy
                    and not client.is_closed
                    and not self._closed
                    and generation == self._generation
                    and len(self._idle) < 2
                )
                if reuse:
                    # Keep only transport state, not provider-issued cookies.
                    client.cookies.clear()
                    self._idle.append(client)
            if not reuse:
                client.close()

    def clear(self) -> None:
        """Retire idle clients now and active leases when they return."""
        with self._lock:
            idle, self._idle = self._idle, []
            self._generation += 1
        for client in idle:
            client.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.clear()
