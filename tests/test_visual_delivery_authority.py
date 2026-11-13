from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMError, LLMResponse, ToolCall
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.surface.types import ApprovalDecision
from alysis_code.swarm_write_guard import SwarmWriteScopeGuard


def _cfg() -> AppConfig:
    cfg = AppConfig(model="visual-fixture", stream=False, routing_mode="code_only")
    cfg.web_search_mode = "off"
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "visual-fixture": {
                    "supports_vision": True,
                    "context_window_tokens": 128000,
                    "max_output_tokens": 1024,
                }
            }
        }
    }
    return cfg


class _Surface(NoopSurface):
    def __init__(self, *, allow=True, cancel_after_decode=False):
        self.allow = allow
        self.cancel_after_decode = cancel_after_decode
        self.approvals = []
        self.completed = []

    def request_approval(self, request):
        self.approvals.append(request)
        return ApprovalDecision(allow=self.allow, allow_for_session=False)

    def emit_tool_call_completed(self, call_id, success, result_preview, **kwargs):
        self.completed.append(result_preview)
        if self.cancel_after_decode and call_id == "view-local":
            raise KeyboardInterrupt("cancelled after decode")


def _image_parts(messages):
    return [
        part
        for message in messages
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
        if part.get("type") == "image_url"
    ]


class _Client:
    model = "visual-fixture"
    temperature = 0.2

    def __init__(self, *, path="diagram.png", failure=None):
        self.path = path
        self.failure = failure
        self.requests = []
        self.image_counts = []
        self.image_parts = []
        self.image_urls = []
        self.stream_flags = []

    def chat(self, *, messages, **kwargs):
        # Keep the actual request/nested references to verify host cleanup, not
        # merely that a later request was constructed without an image.
        self.requests.append(messages)
        parts = _image_parts(messages)
        self.image_counts.append(len(parts))
        self.image_parts.extend(parts)
        self.image_urls.extend(part["image_url"]["url"] for part in parts)
        self.stream_flags.append(kwargs.get("stream"))
        if len(self.requests) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="view-local", name="asset_view", arguments={"path": self.path}),
                    ToolCall(id="status-in-batch", name="capability_status", arguments={}),
                ],
                raw={},
            )
        if len(self.requests) == 2:
            if self.failure is not None:
                if isinstance(self.failure, LLMError) and self.image_urls:
                    self.failure.args = (str(self.failure) + " request=" + self.image_urls[-1],)
                raise self.failure
            return LLMResponse(
                content="Inspected the image.",
                tool_calls=[ToolCall(id="status-next", name="capability_status", arguments={})],
                # A provider echo must not smuggle the original pixels into logs.
                raw={"echo": self.image_urls[-1]}
                if self.path.startswith(".ssh/") and self.image_urls
                else {},
            )
        return LLMResponse(content="The visual inspection is complete.", tool_calls=[], raw={})


def _session(tmp_path, *, guard, surface=None, stream=False):
    dispatch_guard = (
        SwarmWriteScopeGuard(worktree_root=tmp_path, allowed_patterns=[])
        if guard == "swarm"
        else None
    )
    cfg = _cfg()
    cfg.stream = stream
    return create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
        tool_dispatch_guard=dispatch_guard,
        surface=surface,
        crash_diagnostic_log_path=tmp_path / "diagnostics.jsonl",
    )


def _assert_clean_logs(session, client):
    log = session.store.path.read_text(encoding="utf-8")
    diagnostics = session.crash_diagnostics.path.read_text(encoding="utf-8")
    for saved in (log, diagnostics):
        assert "data:image/png;base64," not in saved
        for url in client.image_urls:
            assert url not in saved
            assert url.partition(",")[2] not in saved
    return log


@pytest.mark.parametrize("guard", ["swarm"])
def test_real_dispatch_guards_preserve_visual_pixels_and_tool_batch_order(tmp_path, guard):
    Image.new("RGB", (4, 4), "green").save(tmp_path / "diagram.png")
    session = _session(tmp_path, guard=guard)
    client = _Client()
    session.client = client
    try:
        assert session.run_turn("Inspect diagram.png as visual evidence.") == 0
        assert client.image_counts == [0, 1, 1]
        rendered = Image.open(io.BytesIO(base64.b64decode(client.image_urls[0].split(",")[1])))
        assert rendered.getpixel((0, 0)) == (0, 128, 0)
        messages = client.requests[1]
        view_index = next(
            i for i, m in enumerate(messages) if m.get("tool_call_id") == "view-local"
        )
        assert messages[view_index + 1]["tool_call_id"] == "status-in-batch"
        assert isinstance(messages[view_index + 2]["content"], list)
        assert len(_image_parts(session.messages)) == 1
        log = _assert_clean_logs(session, client)
        assert "asset_visual_delivered" in log
        assert session.tools["asset_view"].visual_delivery is not None
        assert "visual_delivery" not in json.dumps(session.tools["asset_view"].as_openai_tool())
    finally:
        session.close()


@pytest.mark.parametrize("guard", [None, "swarm"])
def test_approved_sensitive_pixels_are_visible_to_one_request_then_scrubbed(tmp_path, guard):
    (tmp_path / ".ssh").mkdir()
    Image.new("RGB", (4, 4), "green").save(tmp_path / ".ssh/diagram.png")
    surface = _Surface()
    session = _session(tmp_path, guard=guard, surface=surface)
    client = _Client(path=".ssh/diagram.png")
    session.client = client
    try:
        assert session.run_turn("Inspect .ssh/diagram.png as visual evidence.") == 0
        assert len(surface.approvals) == 1
        assert surface.approvals[0].metadata["mandatory_explicit_approval"] is True
        assert client.image_counts == [0, 1, 0]
        assert client.stream_flags[1] is False
        assert all(not part for part in client.image_parts)
        assert not _image_parts(session.messages)
        assert "data:image/png;base64," not in json.dumps(client.requests)
        assert "data:image/png;base64," not in json.dumps(surface.completed)
        log = _assert_clean_logs(session, client)
        receipts = [
            json.loads(line) for line in log.splitlines() if '"asset_visual_delivered"' in line
        ]
        assert len(receipts) == 1
        assert "one_authorized_request" in json.dumps(receipts)
        channel = session.tools["asset_view"].visual_delivery
        assert not channel._pending
        assert not channel._sensitive_inflight
    finally:
        session.close()


@pytest.mark.parametrize("failure", ["provider", "cancel"])
def test_sensitive_pixels_are_scrubbed_after_provider_failure_or_cancellation(tmp_path, failure):
    (tmp_path / ".ssh").mkdir()
    Image.new("RGB", (4, 4), "green").save(tmp_path / ".ssh/diagram.png")
    session = _session(tmp_path, guard="swarm", surface=_Surface())
    error = (
        LLMError("LLM error 500: simulated failure")
        if failure == "provider"
        else KeyboardInterrupt()
    )
    client = _Client(path=".ssh/diagram.png", failure=error)
    session.client = client
    try:
        with pytest.raises(type(error)):
            session.run_turn("Inspect .ssh/diagram.png as visual evidence.")
        assert client.image_counts == [0, 1]
        assert all(not part for part in client.image_parts)
        assert "data:image/png;base64," not in json.dumps(client.requests)
        assert not _image_parts(session.messages)
        _assert_clean_logs(session, client)
        # Explicit continuation does not silently replay the already-consumed
        # approval or its image, including after the failed provider request.
        assert session.run_turn("Continue with the available context.") == 0
        assert client.image_counts[-1] == 0
        assert not session.tools["asset_view"].visual_delivery._sensitive_inflight
    finally:
        session.close()


def test_sensitive_pixels_are_discarded_when_cancelled_before_request(tmp_path):
    (tmp_path / ".ssh").mkdir()
    Image.new("RGB", (4, 4), "green").save(tmp_path / ".ssh/diagram.png")
    surface = _Surface(cancel_after_decode=True)
    session = _session(tmp_path, guard="swarm", surface=surface)
    client = _Client(path=".ssh/diagram.png")
    session.client = client
    try:
        with pytest.raises(KeyboardInterrupt, match="after decode"):
            session.run_turn("Inspect .ssh/diagram.png as visual evidence.")
        assert len(surface.approvals) == 1
        assert client.image_counts == [0]
        channel = session.tools["asset_view"].visual_delivery
        assert not channel._pending
        assert not channel._sensitive_inflight
        assert not _image_parts(session.messages)
        assert "asset_visual_delivered" not in _assert_clean_logs(session, client)
    finally:
        session.close()


def test_sensitive_pixels_are_not_replayed_in_provider_retry_request(tmp_path):
    (tmp_path / ".ssh").mkdir()
    Image.new("RGB", (4, 4), "green").save(tmp_path / ".ssh/diagram.png")
    session = _session(tmp_path, guard="swarm", surface=_Surface(), stream=True)
    client = _Client(
        path=".ssh/diagram.png", failure=LLMError("LLM error 400: stream not supported")
    )
    session.client = client
    try:
        assert session.run_turn("Inspect .ssh/diagram.png as visual evidence.") == 0
        assert client.image_counts == [0, 1, 0]
        assert client.stream_flags == [True, False, False]
        assert all(message.get("role") for message in client.requests[-1])
        assert not _image_parts(client.requests[1])
        assert not _image_parts(session.messages)
        assert "data:image/png;base64," not in json.dumps(client.requests)
        _assert_clean_logs(session, client)
    finally:
        session.close()


def test_denied_sensitive_read_cannot_deliver_or_retain_pixels(tmp_path):
    (tmp_path / ".ssh").mkdir()
    Image.new("RGB", (4, 4), "green").save(tmp_path / ".ssh/diagram.png")
    surface = _Surface(allow=False)
    session = _session(tmp_path, guard="swarm", surface=surface)
    client = _Client(path=".ssh/diagram.png")
    session.client = client
    try:
        assert session.run_turn("Inspect .ssh/diagram.png as visual evidence.") == 1
        assert len(surface.approvals) == 1
        assert client.image_counts == [0]
        assert not session.tools["asset_view"].visual_delivery._pending
        assert "asset_visual_delivered" not in _assert_clean_logs(session, client)
    finally:
        session.close()
