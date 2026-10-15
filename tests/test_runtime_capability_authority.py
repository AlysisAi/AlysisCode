from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from alysis_code.agent.tools_assembly import build_tools
from alysis_code.agent_loop import create_session
from alysis_code.assets.local_tools import LocalAssetViewer
from alysis_code.config import AppConfig, ConfigError, clone_cfg
from alysis_code.execution_deadline import DeadlineExhausted, ExecutionDeadline
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.profiles import (
    ProfileSpec,
    add_profile,
    apply_runtime_base_url_override,
    get_active_profile,
    set_active_profile,
)
from alysis_code.session_store import SessionStore
from alysis_code.tools.web_search import WebSearchError


def _visual_cfg(*, supports_vision=True):
    cfg = AppConfig(
        model="visual-fixture", stream=False, routing_mode="code_only", web_search_mode="off"
    )
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "visual-fixture": {
                    "supports_vision": supports_vision,
                    "context_window_tokens": 128000,
                    "max_output_tokens": 1024,
                }
            }
        }
    }
    return cfg


def _tools(tmp_path, cfg, *, key="local-test-key", mode="auto", depth=0):
    store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path / "sessions",
        session_id="capability",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    tools = build_tools(
        root=tmp_path,
        console=None,
        store=store,
        mode=mode,
        yes=True,
        cfg=cfg,
        api_key=key,
        subagents_enabled=False,
        subagent_depth=depth,
    )
    return tools, store


def test_launch_endpoint_override_reaches_transient_profile_without_mutating_saved_config():
    saved = AppConfig(model="local-model")
    get_active_profile(saved)
    effective = clone_cfg(saved)
    apply_runtime_base_url_override(effective, "http://127.0.0.1:12345/v1")
    client = make_llm_client(cfg=effective, api_key="local-key", model=effective.model)
    assert (
        get_active_profile(effective).base_url == effective.base_url == "http://127.0.0.1:12345/v1"
    )
    assert client.base_url.rstrip("/") == effective.base_url
    assert get_active_profile(saved).base_url == "https://api.openai.com/v1"
    assert get_active_profile(effective).protocol == "openai_compat"


def test_document_runtime_is_discoverable_without_authorizing_shell_execution(tmp_path):
    tools, store = _tools(tmp_path, _visual_cfg(), mode="readonly", depth=1)
    try:
        status = tools["capability_status"].run({})["pdf"]
        assert status["state"] in {"ready", "unavailable"}
        if status["state"] == "ready":
            assert "alysis_code.documents" in status["command"]
            assert "permissions" in status["guidance"]
        office = tools["capability_status"].run({})["office"]
        assert "alysis_code.office" in office["command"]
        assert set(office["formats"]) == {"docx", "xlsx", "pptx"}
        assert "permissions" in office["guidance"]
        assert "shell_run" not in tools
    finally:
        store.close()


def test_launch_override_cannot_redirect_adapter_owned_subscription_endpoint():
    cfg = AppConfig(model="subscription-model")
    add_profile(
        cfg,
        ProfileSpec(
            name="subscription",
            protocol="openai_responses",
            base_url="https://chatgpt.com/backend-api/codex",
            auth_provider="openai-codex",
            default_model="subscription-model",
        ),
    )
    set_active_profile(cfg, "subscription")
    original = cfg.base_url
    with pytest.raises(ConfigError, match="owned by their provider"):
        apply_runtime_base_url_override(cfg, "http://127.0.0.1:12345/v1")
    assert cfg.base_url == original
    assert get_active_profile(cfg).base_url == original


def test_web_readiness_uses_real_configured_endpoint_auth_and_session_observation(
    tmp_path, monkeypatch
):
    for key in (
        "ALYSIS_WEB_SEARCH_API_KEY",
        "ALYSIS_WEB_SEARCH_BASE_URL",
        "ALYSIS_WEB_SEARCH_PROVIDER",
        "ALYSIS_WEB_SEARCH_ADAPTER",
    ):
        monkeypatch.delenv(key, raising=False)
    received = []
    response_status = [200]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(response_status[0])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = (
                {"error": {"message": "test authentication rejected"}}
                if response_status[0] != 200
                else {
                    "id": "response-local",
                    "model": "local-search-model",
                    "output": [
                        {
                            "type": "web_search_call",
                            "id": "search-local",
                            "status": "completed",
                            "action": {
                                "type": "search",
                                "query": "documentation",
                                "sources": [{"url": "https://example.test/docs", "title": "Docs"}],
                            },
                        },
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Documented behavior",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "url": "https://example.test/docs",
                                            "title": "Docs",
                                            "start_index": 0,
                                            "end_index": 10,
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            )
            self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = AppConfig(
        model="local-search-model",
        web_search_mode="native",
        web_search_adapter="openai_responses",
        web_search_base_url=f"http://127.0.0.1:{server.server_port}/v1",
    )
    tools, store = _tools(tmp_path, cfg)
    try:
        before = tools["capability_status"].run({})["web_search"]
        assert before["state"] == "degraded"
        assert before["basis"] == "configured_but_unprobed"
        assert received == []
        result = tools["web_search"].run({"query": "documentation"})
        assert result["capability_readiness"]["state"] == "ready"
        assert received[0][:2] == ("/v1/responses", "Bearer local-test-key")
        assert tools["capability_status"].run({})["web_search"]["state"] == "ready"
        response_status[0] = 401
        with pytest.raises(WebSearchError):
            tools["web_search"].run({"query": "documentation again"})
        after = tools["capability_status"].run({})["web_search"]
        assert after["state"] == "unavailable"
        assert "local-test-key" not in json.dumps(after)
        cfg.web_search_model = "different-search-model"
        assert tools["capability_status"].run({})["web_search"]["state"] == "degraded"
        independent, other_store = _tools(tmp_path, cfg)
        try:
            assert (
                independent["capability_status"].run({})["web_search"]["basis"]
                == "configured_but_unprobed"
            )
        finally:
            other_store.close()
    finally:
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_generic_compatible_route_and_readonly_child_do_not_claim_native_web_readiness(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_ADAPTER", "auto")
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_MODE", "native")
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_BASE_URL", raising=False)
    cfg = AppConfig(
        model="arbitrary-model",
        base_url="https://compatible.example.test/v1",
        web_search_mode="native",
    )
    tools, store = _tools(tmp_path, cfg, mode="readonly", depth=1)
    try:
        assert "web_search" not in tools
        assert tools["capability_status"].run({})["web_search"]["state"] == "unavailable"
    finally:
        store.close()


def test_image_view_crop_delivers_pixels_through_sidechannel_without_logging_bytes(tmp_path):
    image = Image.new("RGB", (12, 8), "red")
    image.paste("blue", (6, 0, 12, 8))
    source = tmp_path / "diagram.png"
    image.save(source)
    viewer = LocalAssetViewer(root=tmp_path, cfg=_visual_cfg())
    result = viewer.view({"path": "diagram.png", "crop": [6, 0, 12, 8]})
    assert result["rendered_dimensions"] == [6, 8]
    assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "base64" not in json.dumps(result)
    message = viewer.take_visual_message(result["visual_delivery_id"])
    encoded = message["content"][0]["image_url"]["url"].split(",", 1)[1]
    decoded = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert decoded.getpixel((2, 2)) == (0, 0, 255)
    assert "untrusted data" in message["content"][1]["text"]
    assert viewer.take_visual_message(result["visual_delivery_id"]) is None


@pytest.mark.parametrize(
    "args",
    [
        {"path": "../outside.png"},
        {"path": "artifact:foreign"},
        {"path": "diagram.png", "crop": [-1, 0, 2, 2]},
        {"path": "diagram.png", "timestamp_s": 0},
    ],
)
def test_image_view_rejects_invalid_or_unauthorized_inputs(tmp_path, args):
    Image.new("RGB", (4, 4)).save(tmp_path / "diagram.png")
    viewer = LocalAssetViewer(root=tmp_path, cfg=_visual_cfg())
    result = viewer.view(args)
    assert result["error_code"] == "asset_view_failed"
    assert result["visual_delivered"] is False
    assert viewer._pending == {}


def test_unknown_or_nonvisual_model_is_not_sent_visual_payload(tmp_path):
    Image.new("RGB", (4, 4)).save(tmp_path / "diagram.png")
    viewer = LocalAssetViewer(root=tmp_path, cfg=_visual_cfg(supports_vision=False))
    assert viewer.view({"path": "diagram.png"})["status"] == "tool_unavailable"
    assert viewer._pending == {}


def test_expired_deadline_prevents_visual_decode(tmp_path, monkeypatch):
    Image.new("RGB", (4, 4)).save(tmp_path / "diagram.png")
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0, deadline_monotonic=1, clock=lambda: 2
    )
    viewer = LocalAssetViewer(root=tmp_path, cfg=_visual_cfg(), execution_deadline=deadline)

    def forbidden_decode(*_args, **_kwargs):
        raise AssertionError("No visual decode is authorized after expiry")

    monkeypatch.setattr(Image, "open", forbidden_decode)
    with pytest.raises(DeadlineExhausted):
        viewer.view({"path": "diagram.png"})
    assert viewer._pending == {}


def test_source_mutation_during_visual_render_cannot_publish_wrong_identity(tmp_path, monkeypatch):
    source = tmp_path / "diagram.png"
    Image.new("RGB", (4, 4)).save(source)
    original_save = Image.Image.save

    def mutate_after_render(image, destination, *args, **kwargs):
        original_save(image, destination, *args, **kwargs)
        source.write_bytes(b"changed during render")

    monkeypatch.setattr(Image.Image, "save", mutate_after_render)
    viewer = LocalAssetViewer(root=tmp_path, cfg=_visual_cfg())
    result = viewer.view({"path": "diagram.png"})
    assert "changed during inspection" in result["error"]
    assert viewer._pending == {}


def test_video_frame_reports_source_identity_and_presentation_time(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for real video qualification")
    source = tmp_path / "sample.mp4"
    made = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=4:duration=2",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        capture_output=True,
        timeout=15,
    )
    assert made.returncode == 0, made.stderr.decode(errors="replace")
    viewer = LocalAssetViewer(root=tmp_path, cfg=_visual_cfg())
    result = viewer.view({"path": "sample.mp4", "timestamp_s": 0.75})
    assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result["frame_timestamp_s"] == pytest.approx(0.75, abs=0.25)
    assert result["rendered_dimensions"] == [64, 48]
    assert viewer.take_visual_message(result["visual_delivery_id"])
    cropped = viewer.view({"path": "sample.mp4", "timestamp_s": 0.75, "crop": [16, 8, 48, 40]})
    assert cropped["source_dimensions"] == [64, 48]
    assert cropped["rendered_dimensions"] == [32, 32]


class _VisualClient:
    model = "visual-fixture"
    temperature = 0.2

    def __init__(self):
        self.calls = []

    def chat(self, *, messages, **kwargs):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="view-local", name="asset_view", arguments={"path": "diagram.png"})
                ],
                raw={},
            )
        return LLMResponse(content="The visual evidence was inspected.", tool_calls=[], raw={})


@pytest.mark.parametrize("batched", [False, True])
def test_real_normal_session_exposes_and_delivers_local_visual_evidence(tmp_path, batched):
    Image.new("RGB", (4, 4), "green").save(tmp_path / "diagram.png")
    session = create_session(
        cfg=_visual_cfg(),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
    )
    client = _VisualClient()
    if batched:
        original_chat = client.chat

        def chat_with_batch(**kwargs):
            response = original_chat(**kwargs)
            if response.tool_calls:
                response.tool_calls.append(
                    ToolCall(id="capabilities-in-batch", name="capability_status", arguments={})
                )
            return response

        client.chat = chat_with_batch
    session.client = client
    try:
        assert session.run_turn("Inspect diagram.png as visual evidence.") == 0
        parts = [
            part
            for message in client.calls[1]
            for part in (message.get("content") if isinstance(message.get("content"), list) else [])
        ]
        assert any(part.get("type") == "image_url" for part in parts)
        assert any("untrusted data" in part.get("text", "") for part in parts)
        if batched:
            observed = client.calls[1]
            view_result = next(
                i
                for i, message in enumerate(observed)
                if message.get("tool_call_id") == "view-local"
            )
            second_result = next(
                i
                for i, message in enumerate(observed)
                if message.get("tool_call_id") == "capabilities-in-batch"
            )
            assert observed[view_result + 1]["tool_call_id"] == "capabilities-in-batch"
            assert isinstance(observed[second_result + 1]["content"], list)
        log_path = session.store.path
    finally:
        session.close()
    log = log_path.read_text(encoding="utf-8")
    assert "asset_visual_delivered" in log
    assert "data:image/png;base64" not in log
