from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from alysis_code import alysis_cloud
from alysis_code import feedback_delivery as delivery
from alysis_code.cli_impl.tui.report_flow import ReportFlow
from alysis_code.config import AppConfig
from alysis_code.feedback_report import FeedbackReportError


@pytest.fixture(autouse=True)
def private_test_endpoint(monkeypatch):
    monkeypatch.setattr(alysis_cloud, "feedback_url", lambda: "https://feedback.invalid/feedback")
    monkeypatch.setattr(
        delivery,
        "load_persisted_profile_keys",
        lambda: {"alysis": "slk_feedback_local_fixture_only"},
    )


def prepared(tmp_path, **kwargs):
    return delivery.prepare_feedback(
        workspace_root=tmp_path,
        feedback_text="Synthetic feedback",
        include_diagnostics=False,
        **kwargs,
    )


def test_diagnostics_are_opt_in_and_preview_matches_exact_payload(tmp_path):
    session = SimpleNamespace(messages=[{"role": "user", "content": "PRIVATE CONVERSATION"}])
    report = prepared(tmp_path, active_session=session)
    assert "PRIVATE CONVERSATION" not in report.preview()
    assert json.loads(report.body)["diagnostics"] is None
    assert json.loads(report.preview()) == json.loads(report.body)
    assert "Synthetic feedback" not in repr(report)


def test_diagnostics_redact_secrets_and_omit_images_and_provider_state(tmp_path):
    session = SimpleNamespace(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "password=synthetic123 slk_example_secret_device_token",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,PRIVATEIMAGE"},
                    },
                ],
                "provider_metadata": {"thinking": "private reasoning"},
                "api_key": "other-secret",
            }
        ]
    )
    report = delivery.prepare_feedback(
        workspace_root=tmp_path,
        feedback_text="password=synthetic123",
        include_diagnostics=True,
        active_session=session,
    )
    for secret in [
        "synthetic123",
        "slk_example",
        "PRIVATEIMAGE",
        "private reasoning",
        "other-secret",
    ]:
        assert secret not in report.preview()
    session.messages[0]["content"] = "changed after review"
    assert "changed after review" not in report.preview()


def test_diagnostics_bounded_to_recent_messages_and_large_messages_omitted(tmp_path):
    session = SimpleNamespace(
        messages=[{"role": "user", "content": "x" * 40000} for _ in range(100)]
    )
    report = delivery.prepare_feedback(
        workspace_root=tmp_path,
        feedback_text="repro",
        include_diagnostics=True,
        active_session=session,
    )
    data = json.loads(report.body)["diagnostics"]
    assert data["omitted_messages"] == 80 and len(data["messages"]) == 20
    assert "Message exceeds" in report.preview() and len(report.body) < delivery.MAX_REQUEST_BYTES


@pytest.mark.parametrize("text", ["", " ", "x" * 8001])
def test_send_requires_bounded_description(tmp_path, text):
    with pytest.raises(FeedbackReportError):
        delivery.prepare_feedback(
            workspace_root=tmp_path, feedback_text=text, include_diagnostics=False
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://feedback.example/feedback",
        "ftp://localhost/feedback",
        "https://user:pass@example.com/feedback",
        "https://example.com/feedback?token=x",
    ],
)
def test_unsafe_endpoints_rejected(tmp_path, monkeypatch, url):
    monkeypatch.setattr(alysis_cloud, "feedback_url", lambda: url)
    with pytest.raises(FeedbackReportError):
        prepared(tmp_path)


def transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        delivery.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_transport_sends_approved_bytes_and_validates_receipt(tmp_path, monkeypatch):
    report = prepared(tmp_path)

    def handle(req):
        assert req.content == report.body
        assert req.headers["Authorization"] == "Bearer slk_feedback_local_fixture_only"
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "report_id": report.report_id,
                "created_at": "2026-09-09T12:00:00Z",
            },
        )

    transport(monkeypatch, handle)
    assert delivery.has_alysis_sign_in() is True
    receipt = delivery.send_feedback(report)
    assert receipt.report_id == report.report_id and receipt.anonymous is False


@pytest.mark.parametrize("key", ["password", "refreshToken", "AWS_SECRET_ACCESS_KEY"])
def test_nested_json_credentials_never_reach_feedback_transport(tmp_path, monkeypatch, key):
    secret = "synthetic-secret-not-in-environment-49183"
    arguments = json.dumps(
        {"path": "config.json", "content": json.dumps({key: secret, "enabled": True})}
    )
    session = SimpleNamespace(
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fs_write", "arguments": arguments},
                    }
                ],
            }
        ]
    )
    report = delivery.prepare_feedback(
        workspace_root=tmp_path,
        feedback_text="Synthetic reproduction",
        include_diagnostics=True,
        active_session=session,
    )
    sent = []

    def handle(request):
        sent.append(request.content)
        assert secret.encode() not in request.content
        assert request.content == report.body
        payload = json.loads(request.content)
        function = payload["diagnostics"]["messages"][0]["tool_calls"][0]["function"]
        clean_arguments = json.loads(function["arguments"])
        assert clean_arguments["path"] == "config.json"
        assert json.loads(clean_arguments["content"]) == {key: "[REDACTED]", "enabled": True}
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "report_id": report.report_id,
                "created_at": "2026-09-10T00:00:00Z",
            },
        )

    transport(monkeypatch, handle)
    assert delivery.send_feedback(report).report_id == report.report_id
    assert len(sent) == 1
    assert secret not in report.preview()
    assert json.loads(report.preview()) == json.loads(sent[0])
    assert session.messages[0]["tool_calls"][0]["function"]["arguments"] == arguments


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 404, 409, 413, 429, 500, 503])
def test_failure_and_redirect_never_report_success_or_expose_server_content(
    tmp_path, monkeypatch, status
):
    calls = []

    def handle(req):
        calls.append(req.url)
        return httpx.Response(
            status, headers={"Location": "https://untrusted.invalid"}, text="server-password=secret"
        )

    transport(monkeypatch, handle)
    with pytest.raises(FeedbackReportError) as error:
        delivery.send_feedback(prepared(tmp_path))
    assert "server-password" not in str(error.value) and len(calls) == 1


@pytest.mark.parametrize(
    "data", [{}, {"accepted": True, "report_id": "wrong", "created_at": "2026-09-09"}, "x" * 20000]
)
def test_invalid_receipt_requires_safe_retry(tmp_path, monkeypatch, data):
    transport(monkeypatch, lambda req: httpx.Response(200, json=data))
    with pytest.raises(FeedbackReportError, match="could not be confirmed"):
        delivery.send_feedback(prepared(tmp_path))


def test_without_alysis_login_sends_anonymously_and_never_another_provider_key(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(delivery, "load_persisted_profile_keys", lambda: {"openai": "sk-other"})
    report = prepared(tmp_path)

    def handle(req):
        assert "authorization" not in req.headers
        assert b"sk-other" not in req.content and req.content == report.body
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "report_id": report.report_id,
                "created_at": "2026-10-09T09:00:00Z",
            },
        )

    transport(monkeypatch, handle)
    assert delivery.has_alysis_sign_in() is False
    receipt = delivery.send_feedback(report)
    assert receipt.report_id == report.report_id and receipt.anonymous is True


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        ({"alysis": "slk_feedback_local_fixture_only"}, "sign-in expired"),
        ({}, "only accepts feedback from signed-in users"),
        ({"alysis": None}, "only accepts feedback from signed-in users"),
    ],
)
def test_unauthorized_message_depends_on_whether_a_key_was_sent(
    tmp_path, monkeypatch, keys, message
):
    monkeypatch.setattr(delivery, "load_persisted_profile_keys", lambda: keys)
    transport(monkeypatch, lambda req: httpx.Response(401, json={"error": {"code": "invalid_key"}}))
    with pytest.raises(FeedbackReportError, match=message):
        delivery.send_feedback(prepared(tmp_path))


def finish(flow):
    flow.perform_busy()
    flow.apply_busy_outcome()


def test_flow_default_action_sends_and_failed_send_retries_same_id(tmp_path):
    attempts = []

    def sender(report):
        attempts.append(report)
        if len(attempts) == 1:
            raise FeedbackReportError("Connection dropped")
        return delivery.FeedbackReceipt(report.report_id, "2026-09-09T12:00:00Z")

    flow = ReportFlow(
        workspace_root=tmp_path, cfg=AppConfig(), feedback_text="repro", sender=sender
    )
    flow.choose("prepare_send")
    finish(flow)
    assert flow.stage == "confirm_send" and flow.screen().rows[0].value == "send"
    flow.back()
    assert attempts == [] and flow.stage == "review"
    flow.choose("prepare_send")
    finish(flow)
    flow.choose_current()
    finish(flow)
    assert flow.stage == "confirm_send" and flow.receipt is None
    flow.choose_current()
    finish(flow)
    assert flow.stage == "complete" and flow.receipt.report_id == attempts[0].report_id
    assert attempts[0] is attempts[1]


def test_keyboard_failed_delivery_keeps_description_and_local_fallback(tmp_path, monkeypatch):
    from test_tui_report import _run_ui, _wait

    def sender(report):
        raise FeedbackReportError("Sign in with `alysis login`, then retry.")

    def driver(pipe, captured):
        pipe.send_text("/report preserve this feedback\r")
        _wait(lambda: bool(captured["flows"]))
        flow = captured["flows"][0]
        pipe.send_text("1")
        _wait(lambda: flow.stage == "confirm_send")
        report_id = flow.prepared.report_id
        pipe.send_text("\r")
        _wait(lambda: "alysis login" in flow.status)
        assert flow.stage == "confirm_send" and flow.receipt is None
        assert flow.prepared.report_id == report_id
        pipe.send_text("\x1b")
        _wait(lambda: flow.stage == "review")
        pipe.send_text("4")
        _wait(lambda: flow.stage == "complete")
        assert flow.result.zip_path.exists() and flow.feedback == "preserve this feedback"
        assert flow.receipt is None
        pipe.send_text("\r")
        _wait(lambda: flow.stage == "done")
        pipe.send_text("/exit\r")

    _, transcript, _ = _run_ui(tmp_path, monkeypatch, driver, sender=sender)
    assert not any("Feedback sent privately" in text for _, text in transcript)


def test_changing_consent_creates_a_new_reviewed_payload(tmp_path):
    flow = ReportFlow(workspace_root=tmp_path, cfg=AppConfig(), feedback_text="repro")
    flow.choose("prepare_send")
    finish(flow)
    previous = flow.prepared
    flow.confirm(False)
    flow.choose("toggle_diagnostics")
    flow.choose("prepare_send")
    finish(flow)
    assert flow.prepared.report_id != previous.report_id
    assert json.loads(flow.prepared.body)["diagnostics"] is not None
    flow.confirm(False)
    flow.choose("edit")
    flow.submit_input("changed description")
    flow.choose("prepare_send")
    finish(flow)
    assert json.loads(flow.prepared.body)["description"] == "changed description"


def test_full_feedback_view_preserves_payload_and_enter_returns_without_sending(tmp_path):
    description = "Feedback line\n" * 100 + "Final detail"
    flow = ReportFlow(workspace_root=tmp_path, cfg=AppConfig(), feedback_text=description)
    flow.choose("prepare_send")
    finish(flow)
    original = flow.prepared
    screen = flow.screen()
    assert len(screen.subtitle) < 500
    assert "feedback_details" in [row.value for row in screen.rows]
    assert "diagnostics" not in [row.value for row in screen.rows]
    flow.choose("feedback_details")
    assert "\n".join(text for text, _ in flow.screen().lines) == description
    flow.confirm(flow.screen().confirm_default)
    assert flow.stage == "confirm_send" and flow.prepared is original and flow.receipt is None


def test_compact_send_screen_enter_submits_without_letter_shortcuts(tmp_path, monkeypatch):
    from test_tui_report import _run_ui, _screen_text, _wait

    attempts = []
    rendered = []

    def sender(report):
        attempts.append(report)
        return delivery.FeedbackReceipt(report.report_id, "2026-09-09T12:00:00Z")

    def driver(pipe, captured):
        pipe.send_text("/report\r")
        _wait(lambda: bool(captured["flows"]))
        flow = captured["flows"][0]
        pipe.send_text("The input gets stuck.\r")
        _wait(lambda: flow.stage == "review")
        pipe.send_text("\r")
        _wait(lambda: flow.stage == "confirm_send")
        _wait(lambda: "Enter send" in _screen_text(captured))
        rendered.append(_screen_text(captured))
        assert "The input gets stuck." in rendered[0]
        assert "Send feedback" in rendered[0] and "Diagnostics off" in rendered[0]
        assert all(
            text not in rendered[0]
            for text in ["schema_version", "report_id", "client_version", "supabase", "Y send"]
        )
        assert attempts == []
        pipe.send_text("\r")
        _wait(lambda: flow.stage == "complete")
        assert len(attempts) == 1
        pipe.send_text("\r")
        _wait(lambda: flow.stage == "done")
        pipe.send_text("/exit\r")

    _run_ui(tmp_path, monkeypatch, driver, columns=40, rows=16, sender=sender)
