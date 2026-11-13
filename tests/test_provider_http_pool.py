from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic

import httpx
import pytest

from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.provider_limits import ProviderRetrySettings


@pytest.fixture
def provider():
    requests = []
    waiting = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = payload["messages"][-1]["content"]
            requests.append((self.client_address[1], self.headers.get("Authorization"), payload))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.flush()
            if prompt == "hold":
                waiting.set()
                release.wait(5)
                return
            data = (
                b'data: {"choices":[{"index":0,"delta":{"content":"All good."}}]}\n\n'
                b"data: [DONE]\n\n"
            )
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()
            if prompt == "unfinished framing":
                release.wait(5)
                return
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = OpenAICompatClient(
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key="first-account",
        model="test-model",
        timeout_s=5,
        provider_retry_settings=ProviderRetrySettings(max_retries=0),
    )
    try:
        yield client, requests, waiting
    finally:
        release.set()
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def ask(client, prompt="hello", token=None):
    return client.chat(
        messages=[{"role": "user", "content": prompt}],
        stream=True,
        cancellation_token=token,
    ).content


def test_real_http1_reuses_connection_after_sse_done_and_updates_timeout(provider):
    client, requests, _ = provider
    assert ask(client) == "All good."
    client.timeout_s = 3
    assert ask(client, token=InteractiveCancellationToken()) == "All good."
    assert ask(client) == "All good."
    assert len(requests) == 3
    assert len({port for port, _, _ in requests}) == 1


def test_account_and_header_changes_retire_the_previous_connection(provider):
    client, requests, _ = provider
    ask(client)
    client.api_key = "second-account"
    ask(client)
    client.extra_headers = {"x-tenant": "different-tenant"}
    ask(client)
    assert len({port for port, _, _ in requests}) == 3
    assert [auth for _, auth, _ in requests] == [
        "Bearer first-account",
        "Bearer second-account",
        "Bearer second-account",
    ]


def test_incomplete_http_framing_preserves_answer_without_waiting_or_replaying(provider):
    client, requests, _ = provider
    start = monotonic()
    assert ask(client, "unfinished framing") == "All good."
    assert monotonic() - start < 1
    assert len(requests) == 1
    assert ask(client) == "All good."
    assert requests[0][0] != requests[1][0]


def test_cancelling_a_reused_connection_does_not_cancel_another_request(provider):
    client, requests, waiting = provider
    assert ask(client) == "All good."
    token = InteractiveCancellationToken()
    with ThreadPoolExecutor(max_workers=2) as executor:
        blocked = executor.submit(ask, client, "hold", token)
        assert waiting.wait(2)
        other = executor.submit(ask, client)
        assert other.result(timeout=2) == "All good."
        start = monotonic()
        token.cancel()
        with pytest.raises(KeyboardInterrupt):
            blocked.result(timeout=1)
        assert monotonic() - start < 1
    assert requests[0][0] == requests[1][0]
    assert requests[2][0] != requests[1][0]
    assert ask(client) == "All good."
    assert requests[-1][0] == requests[2][0]


def test_session_cleanup_releases_connections_but_allows_borrowed_client_reuse(provider):
    client, requests, _ = provider
    ask(client)
    client.close_http_connections()
    ask(client)
    assert requests[0][0] != requests[1][0]


def test_failed_attempt_is_not_returned_to_the_idle_pool():
    from alysis_code.llm.http_pool import ProviderHTTPPool

    pool = ProviderHTTPPool()
    try:
        with pytest.raises(httpx.ConnectTimeout):
            with pool.lease(scope="account", timeout=httpx.Timeout(1)) as first:
                raise httpx.ConnectTimeout("connect timed out")
        assert first.is_closed
        with pool.lease(scope="account", timeout=httpx.Timeout(1)) as second:
            assert second is not first
            # Closing while leased must also retire it upon return.
            pool.clear()
        assert second.is_closed
    finally:
        pool.close()
