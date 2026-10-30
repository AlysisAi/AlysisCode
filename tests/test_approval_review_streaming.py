"""Real HTTP framing for review progress and complete-verdict enforcement."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from alysis_code.agent.approval_review import ApprovalReviewer
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalRequest


@pytest.mark.parametrize("finish_stream", [True, False])
def test_review_progress_survives_idle_timeout_but_partial_allow_cannot_authorize(
    tmp_path, finish_stream
):
    release = threading.Event()
    requests = []
    assessment = json.dumps(
        {
            "decision": "allow",
            "risk": "low",
            "user_authorization": "high",
            "reason": "Authorized test.",
        }
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            try:
                if not body.get("stream"):
                    # A buffered response exceeds the client's idle timeout.
                    release.wait(0.6)
                    self.send_response(200)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if not finish_stream:
                    # Even syntactically valid early JSON is not a finished verdict.
                    event = {"choices": [{"delta": {"content": assessment}}]}
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()
                for _ in range(12 if finish_stream else 100):
                    if release.wait(0.05):
                        return
                    self.wfile.write(b'data: {"choices":[{"delta":{"reasoning_content":"."}}]}\n\n')
                    self.wfile.flush()
                event = {"choices": [{"delta": {"content": assessment}, "finish_reason": "stop"}]}
                self.wfile.write(("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = OpenAICompatClient(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="synthetic",
            model="test-model",
            timeout_s=0.3,
            provider_retry_settings=ProviderRetrySettings(max_retries=0),
        )
        store = SessionStore(
            enabled=False,
            sessions_dir=tmp_path,
            session_id="stream-review",
            cwd=str(tmp_path),
            repo_root=str(tmp_path),
        )
        store.append("user_message", {"content": "Run the local tests."})
        reviewer = ApprovalReviewer(timeout_seconds=3 if finish_stream else 0.8)
        reviewer.bind(SimpleNamespace(client=client, cfg=AppConfig(), store=store))
        started = time.monotonic()
        verdict = reviewer.review(
            ApprovalRequest(kind="shell_run", command="pytest", reason="Test", preview="pytest"),
            workspace_root=tmp_path,
        )
        assert len(requests) == 1
        assert time.monotonic() - started > client.timeout_s
        assert verdict.outcome == ("allow" if finish_stream else "ask_person")
        assert verdict.failure == ("" if finish_stream else "timed_out")
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
