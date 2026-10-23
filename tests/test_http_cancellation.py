from __future__ import annotations

import ipaddress
import socket
import ssl
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.llm.http_cancellation import cancellable_httpx_request


class _BlockedTLSServer:
    """Accept one HTTPS request and deliberately withhold its next bytes."""

    def __init__(self, context: ssl.SSLContext, *, send_headers: bool) -> None:
        self.request_received = threading.Event()
        self._release = threading.Event()
        self._errors: list[BaseException] = []
        self._context = context
        self._send_headers = send_headers
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(0.1)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(
            target=self._serve,
            name="alysis-test-blocked-tls-server",
            daemon=True,
        )
        self._thread.start()

    def _serve(self) -> None:
        try:
            raw_connection: socket.socket | None = None
            while raw_connection is None and not self._release.is_set():
                try:
                    raw_connection, _address = self._listener.accept()
                except TimeoutError:
                    continue
            if raw_connection is None:
                return
            raw_connection.settimeout(2)
            with self._context.wrap_socket(raw_connection, server_side=True) as connection:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += connection.recv(4096)
                if self._send_headers:
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"content-type: text/event-stream\r\n"
                        b"transfer-encoding: chunked\r\n"
                        b"connection: close\r\n\r\n"
                    )
                self.request_received.set()
                self._release.wait(timeout=5)
        except BaseException as exc:  # noqa: BLE001 - relayed to the test thread
            if not self._release.is_set():
                self._errors.append(exc)
            self.request_received.set()

    def close(self) -> None:
        self._release.set()
        self._listener.close()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise AssertionError("loopback TLS server did not stop")
        if self._errors:
            raise AssertionError("loopback TLS server failed") from self._errors[0]


@pytest.fixture(scope="module")
def tls_contexts(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """Create a short-lived CA certificate trusted only by this loopback test."""

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    certificate_path = tmp_path_factory.mktemp("http-cancellation") / "server.pem"
    key_path = certificate_path.with_name("server-key.pem")
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_path, key_path)
    client_context = ssl.create_default_context(cafile=str(certificate_path))
    return server_context, client_context


def _wait_for_socket_read(worker: threading.Thread, *, timeout: float = 2.0) -> None:
    """Wait until the worker is waiting for its real socket to become readable."""

    deadline = monotonic() + timeout
    pause = threading.Event()
    while monotonic() < deadline:
        frame = sys._current_frames().get(worker.ident or -1)
        while frame is not None:
            filename = Path(frame.f_code.co_filename).as_posix()
            if frame.f_code.co_name == "_wait_socket_ready" and filename.endswith(
                "/alysis_code/llm/http_cancellation.py"
            ):
                return
            frame = frame.f_back
        pause.wait(timeout=0.001)
    raise AssertionError("provider worker did not enter a blocking socket read")


@pytest.mark.parametrize("phase", ["before_headers", "response_body"])
def test_cancellation_interrupts_real_blocked_tls_read(
    phase: str,
    tls_contexts: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    server_context, client_context = tls_contexts
    server = _BlockedTLSServer(
        server_context,
        send_headers=phase == "response_body",
    )
    token = InteractiveCancellationToken()
    response_received = threading.Event()
    outcome: list[BaseException] = []

    def _request() -> None:
        try:
            with httpx.Client(
                timeout=60,
                verify=client_context,
                trust_env=False,
            ) as client:
                with cancellable_httpx_request(
                    client=client,
                    cancellation_token=token,
                    method="GET",
                    url=f"https://127.0.0.1:{server.port}/blocked",
                    stream=True,
                ) as response:
                    response_received.set()
                    next(response.iter_raw())
        except BaseException as exc:  # noqa: BLE001 - cancellation is the assertion
            outcome.append(exc)

    worker = threading.Thread(
        target=_request,
        name=f"alysis-test-tls-{phase}",
        daemon=True,
    )
    worker.start()
    cancelled_in_time = False
    try:
        ready = server.request_received if phase == "before_headers" else response_received
        assert ready.wait(timeout=2)
        _wait_for_socket_read(worker)

        # The full-screen UI uses the nonblocking publication path so a slow
        # transport teardown cannot freeze its event loop. Exercise that exact
        # boundary against the real TLS socket rather than only the synchronous
        # token helper used by lower-level provider tests.
        token.cancel_nonblocking()
        worker.join(timeout=1)
        cancelled_in_time = not worker.is_alive()
    finally:
        server.close()
        worker.join(timeout=2)

    assert cancelled_in_time
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], KeyboardInterrupt)
    assert "cancelled_by_user" in str(outcome[0])
    assert token._abort is None  # noqa: SLF001
