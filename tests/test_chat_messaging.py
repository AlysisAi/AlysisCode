from __future__ import annotations

import json
import multiprocessing
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from alysis_code.chat_messaging import ChatAlreadyOwned, ChatMessageStore, ChatMessagingError
from alysis_code.chat_messaging_runtime import claim_chat_owner


def mailbox(tmp_path):
    store = ChatMessageStore(tmp_path / "sessions")
    for name, folder in (("alpha", "project"), ("beta", "project"), ("private", "other")):
        store.register(name, tmp_path / folder, title=name)
    return store


def test_link_is_explicit_and_discovery_respects_project_scope(tmp_path):
    store = mailbox(tmp_path)
    assert [c["id"] for c in store.list_chats("alpha")] == ["beta"]
    with pytest.raises(ChatMessagingError, match="link"):
        store.send("alpha", "beta", "Hello", idempotency_key="first")
    store.link("alpha", "private")
    assert {c["id"] for c in store.list_chats("alpha")} == {"beta", "private"}
    store.send("alpha", "private", "Γειά σου 世界 مرحبا", idempotency_key="first")
    assert store.inbox("private")[0]["content"] == "Γειά σου 世界 مرحبا"


def test_idempotency_and_reply_provenance(tmp_path):
    store = mailbox(tmp_path)
    store.link("alpha", "beta")
    first = store.send("alpha", "beta", "Review the API", idempotency_key="first")
    assert (
        store.send("alpha", "beta", "Review the API", idempotency_key="first")["id"] == first["id"]
    )
    with pytest.raises(ChatMessagingError, match="Idempotency"):
        store.send("alpha", "beta", "Different", idempotency_key="first")
    with pytest.raises(ChatMessagingError, match="received"):
        store.send("alpha", "beta", "Spoofed reply", idempotency_key="bad", reply_to=first["id"])
    answer = store.send(
        "beta",
        "alpha",
        "Use version 2",
        idempotency_key="answer",
        reply_to=first["id"],
        request_reply=False,
    )
    assert answer["exchange_id"] == first["exchange_id"]
    assert store.status("alpha", first["id"])["replies"][0]["id"] == answer["id"]
    with pytest.raises(ChatMessagingError):
        store.status("private", first["id"])


def test_concurrent_retry_persists_one_message(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = mailbox(tmp_path)
    store.link("alpha", "beta")
    with ThreadPoolExecutor(max_workers=6) as workers:
        messages = list(
            workers.map(
                lambda _: store.send("alpha", "beta", "same", idempotency_key="retry"), range(18)
            )
        )
    assert len({m["id"] for m in messages}) == 1
    assert len(ChatMessageStore(tmp_path / "sessions").inbox("beta")) == 1


def test_revocation_and_restart_do_not_reexecute_started_work(tmp_path):
    store = mailbox(tmp_path)
    store.link("alpha", "beta")
    first = store.send("alpha", "beta", "first", idempotency_key="one")
    assert store.claim("beta", first["id"]) is not None
    assert store.claim("beta", first["id"]) is None
    reopened = ChatMessageStore(tmp_path / "sessions")
    reopened.recover("beta")
    assert reopened.status("alpha", first["id"])["error"] == "interrupted_indeterminate"
    second = store.send("alpha", "beta", "second", idempotency_key="two")
    store.link("alpha", "beta", enabled=False)
    assert store.status("alpha", second["id"])["state"] == "cancelled"
    assert not store.inbox("beta", pending=True)


def test_exchange_allowance_and_close(tmp_path):
    store = mailbox(tmp_path)
    store.link("alpha", "beta")
    previous = None
    for i in range(32):
        a, b = ("alpha", "beta") if i % 2 == 0 else ("beta", "alpha")
        previous = store.send(
            a,
            b,
            str(i),
            idempotency_key="message_" + str(i),
            reply_to=previous["id"] if previous else None,
        )
    with pytest.raises(ChatMessagingError, match="allowance"):
        store.send("alpha", "beta", "overflow", idempotency_key="overflow", reply_to=previous["id"])
    store.close_exchange("alpha", previous["exchange_id"])
    assert not store.inbox("beta", pending=True)
    with pytest.raises(ChatMessagingError, match="unavailable"):
        store.close_exchange("private", previous["exchange_id"])


def test_owner_filter_and_private_database(tmp_path, monkeypatch):
    store = mailbox(tmp_path)
    monkeypatch.setattr("alysis_code.chat_messaging.local_session_owner", lambda: "foreign@host")
    assert store.list_chats() == []
    with pytest.raises(ChatMessagingError, match="owner"):
        store.inbox("alpha")
    if __import__("os").name != "nt":
        assert store.directory.stat().st_mode & 0o777 == 0o700
        assert store.path.stat().st_mode & 0o777 == 0o600


def test_session_ownership_and_alias(tmp_path):
    store = mailbox(tmp_path)
    handle = claim_chat_owner(store, "alpha")
    try:
        with pytest.raises(ChatAlreadyOwned):
            claim_chat_owner(store, "alpha")
    finally:
        handle.close()
    handle = claim_chat_owner(store, "alpha")
    handle.close()
    store.alias("new_log", "alpha")
    assert ChatMessageStore(tmp_path / "sessions").identity("new_log") == "alpha"


def test_message_validation_and_secret_redaction(tmp_path, monkeypatch):
    store = mailbox(tmp_path)
    store.link("alpha", "beta")
    with pytest.raises(ChatMessagingError):
        store.send("alpha", "beta", "x" * 16001, idempotency_key="oversize")
    with pytest.raises(ChatMessagingError):
        store.send("alpha", "../private", "x", idempotency_key="path")
    monkeypatch.setenv("ALYSIS_API_KEY", "test_only_secret_canary_7281943")
    store.send("alpha", "beta", "test_only_secret_canary_7281943", idempotency_key="redact")
    assert "test_only_secret_canary_7281943" not in store.inbox("beta")[0]["content"]


class RecordingClient:
    temperature = 0.2

    def __init__(self, model):
        self.model = model
        self.requests = []

    def chat(self, *, messages, **kwargs):
        from alysis_code.llm.types import LLMResponse

        self.requests.append(json.loads(json.dumps(messages)))
        return LLMResponse(content="Answer from " + self.model, tool_calls=[], raw={})


class ReplyingClient(RecordingClient):
    """Script the recipient's explicit model tool call, never engine forwarding."""

    def chat(self, *, messages, **kwargs):
        from alysis_code.llm.types import LLMResponse, ToolCall

        response = super().chat(messages=messages, **kwargs)
        for index in range(len(messages) - 1, -1, -1):
            item = messages[index]
            if item.get("role") != "tool" or not item.get("tool_call_id", "").startswith("peer_"):
                continue
            incoming = json.loads(item["content"])["messages"][0]
            already_sent = any(
                call["function"]["name"] == "send_chat_message"
                for message in messages[index + 1 :]
                for call in message.get("tool_calls", [])
            )
            if incoming["request_reply"] and not already_sent:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="reply_" + incoming["id"],
                            name="send_chat_message",
                            arguments={
                                "recipient": incoming["sender"],
                                "content": response.content,
                                "reply_to": incoming["id"],
                                "request_reply": False,
                                "idempotency_key": "reply_" + incoming["id"],
                            },
                        )
                    ],
                    raw={},
                )
            break
        return response


def create_scripted_session(client, **kwargs):
    """Use the existing client factory seam before any receiving worker starts."""
    from alysis_code.agent.session import create_session

    kwargs["api_key_override"] = "test-key"
    tools = kwargs.pop("tools_override", None)
    with ExitStack() as patches:
        patches.enter_context(
            patch("alysis_code.agent.session._make_session_llm_client", return_value=client)
        )
        if tools is not None:
            patches.enter_context(
                patch("alysis_code.agent.session.build_tools", return_value=tools)
            )
        return create_session(**kwargs)


def make_session(root, session_id, client, **overrides):
    from alysis_code.config import AppConfig
    from alysis_code.runtime_kind import RuntimeKind
    from alysis_code.surface import NoopSurface

    kwargs = dict(
        cfg=AppConfig(
            model=client.model,
            chat_messaging_enabled=True,
            stream=False,
            routing_mode="code_only",
            skills_enabled=False,
        ),
        root=root,
        mode="readonly",
        yes=False,
        max_steps=4,
        no_log=False,
        surface=NoopSurface(),
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        non_interactive=True,
        verification_enabled=False,
        subagents_enabled=False,
        session_log_dir_override=root / "sessions",
        session_id_override=session_id,
    )
    kwargs.update(overrides)
    return create_scripted_session(client, **kwargs)


def wait_until(predicate, timeout=12):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        threading.Event().wait(0.02)
    assert predicate(), "Timed out waiting for engine delivery"


def test_real_sessions_exchange_and_preserve_peer_provenance(tmp_path):
    from alysis_code.cli_impl.commands.chat_resume_helpers import _load_chat_resume_messages
    from alysis_code.session_store import read_session_events

    a_client, b_client = RecordingClient("model-a"), ReplyingClient("model-b")
    a = make_session(tmp_path, "alpha", a_client)
    b = make_session(tmp_path, "beta", b_client)
    try:
        a.messages.append({"role": "assistant", "content": "A-only retained context"})
        b.messages.append({"role": "assistant", "content": "B-only retained context"})
        store = a.chat_endpoint.store
        store.link("alpha", "beta")
        result = a.tools["send_chat_message"].run(
            {"recipient": "beta", "content": "Εξέτασε το API", "idempotency_key": "request"}
        )
        wait_until(lambda: bool(a_client.requests))
        assert b_client.requests
        assert "B-only retained context" in json.dumps(b_client.requests[0])
        assert "A-only retained context" not in json.dumps(b_client.requests[0])
        assert "Εξέτασε το API" in json.dumps(b_client.requests[0], ensure_ascii=False)
        assert (
            store.status("alpha", result["message_id"])["replies"][0]["content"]
            == "Answer from model-b"
        )
        time.sleep(0.35)
        assert len(a_client.requests) == 1
        assert len(b_client.requests) == 2
        reply = store.status("alpha", result["message_id"])["replies"][0]
        recorded_call = next(
            call
            for item in b_client.requests[1]
            for call in item.get("tool_calls", [])
            if call["function"]["name"] == "send_chat_message"
        )
        assert (
            json.loads(recorded_call["function"]["arguments"])["reply_to"] == result["message_id"]
        )
        recorded_result = next(
            item
            for item in b_client.requests[1]
            if item.get("role") == "tool" and item.get("tool_call_id") == recorded_call["id"]
        )
        assert json.loads(recorded_result["content"])["message_id"] == reply["id"]
        events = list(read_session_events(b.store.path))
        assert any(e["type"] == "chat_message_received" for e in events)
        received = next(e["payload"] for e in events if e["type"] == "chat_message_received")
        assert received["content"] == "Εξέτασε το API"
        assert (received["sender"], received["recipient"]) == ("alpha", "beta")
        assert not any(e["type"] == "user_message" for e in events)
        restored = _load_chat_resume_messages(b.store.path)
        assert any(m["role"] == "tool" and "Εξέτασε το API" in m["content"] for m in restored)
    finally:
        b.close()
        a.close()


def test_real_engine_exchange_updates_both_terminal_surfaces(tmp_path):
    from alysis_code.cli_impl.tui.surface import TuiSurface
    from alysis_code.cli_impl.tui.transcript import TuiTranscript

    transcripts = [TuiTranscript(), TuiTranscript()]
    a = make_session(
        tmp_path, "alpha", RecordingClient("model-a"), surface=TuiSurface(transcripts[0])
    )
    b = make_session(
        tmp_path, "beta", ReplyingClient("model-b"), surface=TuiSurface(transcripts[1])
    )
    try:
        store = a.chat_endpoint.store
        store.link("alpha", "beta")
        result = a.tools["send_chat_message"].run(
            {"recipient": "beta", "content": "Εξέτασε το API", "idempotency_key": "terminal"}
        )

        def cards(transcript):
            return [
                json.loads(text)
                for role, text in transcript.snapshot()[0]
                if role == "peer_message"
            ]

        wait_until(
            lambda: all(
                len(cards(t)) == 2 and all(c["state"] == "delivered" for c in cards(t))
                for t in transcripts
            )
        )
        sent, received = cards(transcripts[0])
        assert sent["message_id"] == result["message_id"]
        assert sent["direction"] == "sent" and sent["content"] == "Εξέτασε το API"
        assert received["direction"] == "received" and received["content"] == "Answer from model-b"
        assert [c["direction"] for c in cards(transcripts[1])] == ["received", "sent"]
        assert not any(role == "user" for t in transcripts for role, _ in t.entries)
    finally:
        b.close()
        a.close()


def test_pending_wait_is_successful_tool_activity_and_real_delivery_errors_remain(tmp_path):
    from alysis_code.cli_impl.tui.surface import TuiSurface
    from alysis_code.cli_impl.tui.transcript import TuiTranscript
    from alysis_code.llm.types import LLMResponse, ToolCall

    class WaitingClient(RecordingClient):
        message_id = None

        def chat(self, *, messages, **kwargs):
            response = super().chat(messages=messages, **kwargs)
            if len(self.requests) == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="wait",
                            name="wait_for_chat_reply",
                            arguments={"message_id": self.message_id},
                        )
                    ],
                    raw={},
                )
            return response

    transcript = TuiTranscript()
    client = WaitingClient("model-a")
    session = make_session(tmp_path, "alpha", client, surface=TuiSurface(transcript))
    try:
        store = session.chat_endpoint.store
        store.register("beta", tmp_path)
        store.link("alpha", "beta")
        sent = session.tools["send_chat_message"].run(
            {"recipient": "beta", "content": "A pending request", "idempotency_key": "wait"}
        )
        client.message_id = sent["message_id"]
        assert session.run_turn("Check the current reply status once.") == 0
        result = next(
            json.loads(item["content"])
            for item in session.messages
            if item.get("role") == "tool" and item.get("tool_call_id") == "wait"
        )
        assert result["waiting"] is True and result["state"] == "queued" and "error" not in result
        assert any(
            role == "trace" and "Wait For Chat Reply" in text for role, text in transcript.entries
        )
        assert not any(role == "error" for role, _ in transcript.entries)
        store.claim("beta", client.message_id)
        processing = session.tools["wait_for_chat_reply"].run({"message_id": client.message_id})
        assert processing["state"] == "processing" and "error" not in processing
        store.settle("beta", client.message_id, "failed", "synthetic_delivery_failure")
        failed = session.tools["wait_for_chat_reply"].run({"message_id": client.message_id})
        assert failed["state"] == "failed" and failed["error"] == "synthetic_delivery_failure"
    finally:
        session.close()


def _process_recipient(root, ready, finish):
    session = make_session(Path(root), "beta", ReplyingClient("other-process-model"))
    ready.set()
    try:
        finish.wait(15)
    finally:
        session.close()


def test_independent_engine_processes_can_exchange(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, finish = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_process_recipient, args=(str(tmp_path), ready, finish))
    process.start()
    try:
        assert ready.wait(15)
        session = make_session(tmp_path, "alpha", RecordingClient("sender-model"))
        try:
            store = session.chat_endpoint.store
            store.link("alpha", "beta")
            sent = session.tools["send_chat_message"].run(
                {
                    "recipient": "beta",
                    "content": "Cross-process question",
                    "idempotency_key": "cross_process",
                }
            )
            wait_until(lambda: bool(store.status("alpha", sent["message_id"])["replies"]))
            assert (
                store.status("alpha", sent["message_id"])["replies"][0]["content"]
                == "Answer from other-process-model"
            )
        finally:
            session.close()
    finally:
        finish.set()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_feature_is_opt_in_and_config_setter_returns_config(tmp_path):
    from alysis_code.config import AppConfig, set_config_value

    cfg = AppConfig(model="test", stream=False)
    assert set_config_value(cfg, "chat_messaging_enabled", "true") is cfg
    assert cfg.chat_messaging_enabled
    cfg.chat_messaging_enabled = False
    session = make_session(tmp_path, "disabled", RecordingClient("disabled"), cfg=cfg)
    try:
        assert not hasattr(session, "chat_endpoint")
        assert "send_chat_message" not in session.tools
    finally:
        session.close()


def test_busy_delivery_preserves_tool_call_result_adjacency(tmp_path):
    from alysis_code.agent.tool_definition import ToolDef
    from alysis_code.llm.types import LLMResponse, ToolCall

    entered, release = threading.Event(), threading.Event()

    class BusyClient(RecordingClient):
        def chat(self, *, messages, **kwargs):
            self.requests.append(json.loads(json.dumps(messages)))
            if len(self.requests) == 1:
                entered.set()
                assert release.wait(5)
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="probe", name="probe", arguments={})],
                    raw={},
                )
            return LLMResponse(content="Done", tool_calls=[], raw={})

    a = make_session(tmp_path, "alpha", RecordingClient("a"))
    client = BusyClient("busy-model")
    b = make_session(tmp_path, "beta", client)
    b.tools["probe"] = ToolDef(
        "probe", "Test probe", {"type": "object", "properties": {}}, lambda _: {"ok": True}
    )
    b.tool_list = [tool.as_openai_tool() for tool in b.tools.values()]
    thread = threading.Thread(target=lambda: b.run_turn("Keep working on my original task"))
    try:
        a.chat_endpoint.store.link("alpha", "beta")
        thread.start()
        assert entered.wait(5)
        sent = a.tools["send_chat_message"].run(
            {
                "recipient": "beta",
                "content": "An update from A",
                "idempotency_key": "busy",
                "request_reply": False,
            }
        )
        assert a.chat_endpoint.store.status("alpha", sent["message_id"])["state"] == "queued"
        release.set()
        thread.join(8)
        assert not thread.is_alive()
        messages = client.requests[1]
        probe = next(
            i
            for i, m in enumerate(messages)
            if any(c.get("id") == "probe" for c in m.get("tool_calls", []))
        )
        assert messages[probe + 1]["role"] == "tool"
        assert messages[probe + 1]["tool_call_id"] == "probe"
        assert "An update from A" in json.dumps(messages)
        assert a.chat_endpoint.store.status("alpha", sent["message_id"])["state"] == "delivered"
        assert len(client.requests) == 2
    finally:
        release.set()
        thread.join(8)
        b.close()
        a.close()


def test_revoking_a_link_cancels_running_peer_without_late_reply(tmp_path):
    from alysis_code.llm.types import LLMResponse

    entered, cancelled = threading.Event(), threading.Event()

    class BlockingClient(RecordingClient):
        def chat(self, *, cancellation_token=None, **kwargs):
            entered.set()
            assert cancellation_token.wait_cancelled(5)
            cancelled.set()
            cancellation_token.throw_if_cancelled()
            return LLMResponse(content="Late answer", tool_calls=[], raw={})

    a = make_session(tmp_path, "alpha", RecordingClient("a"))
    b = make_session(tmp_path, "beta", BlockingClient("b"))
    try:
        store = a.chat_endpoint.store
        store.link("alpha", "beta")
        sent = store.send("alpha", "beta", "Read the API", idempotency_key="cancel")
        assert entered.wait(5)
        store.link("alpha", "beta", enabled=False)
        assert cancelled.wait(5)
        wait_until(lambda: store.status("alpha", sent["id"])["state"] == "cancelled")
        assert store.status("alpha", sent["id"])["replies"] == []
        assert b.chat_endpoint.worker.is_alive()
    finally:
        b.close()
        a.close()


def test_saved_chat_activation_restores_own_history_and_model(tmp_path, monkeypatch):
    from alysis_code.chat_messaging_service import restore_chat

    a = make_session(tmp_path, "alpha", RecordingClient("a"))
    b = make_session(tmp_path, "beta", RecordingClient("saved-model"))
    cfg = b.cfg.model_copy(deep=True)
    b.store.append("user_message", {"content": "Retained recipient task"})
    b.close()
    restored = None
    try:
        monkeypatch.setattr("alysis_code.chat_messaging_service.load_config", lambda: cfg)
        a.chat_endpoint.store.link("alpha", "beta")
        sent = a.chat_endpoint.store.send(
            "alpha", "beta", "Question after closing", idempotency_key="offline"
        )
        client = ReplyingClient("saved-model")

        def factory(**kwargs):
            return create_scripted_session(
                client,
                **kwargs,
                verification_enabled=False,
                subagents_enabled=False,
            )

        restored = restore_chat("beta", tmp_path / "sessions", create_session_fn=factory)
        wait_until(lambda: bool(a.chat_endpoint.store.status("alpha", sent["id"])["replies"]))
        assert "Retained recipient task" in json.dumps(client.requests[0])
        assert restored.cfg.model == "saved-model"
        assert restored.mode == "readonly"
        with pytest.raises(ChatAlreadyOwned):
            restore_chat("beta", tmp_path / "sessions", create_session_fn=factory)
    finally:
        if restored:
            restored.close()
        a.close()


def test_desktop_bridge_schedules_peer_as_an_engine_job(tmp_path, monkeypatch):
    import io

    from alysis_code.config import AppConfig
    from alysis_code.ide import stdio_bridge
    from alysis_code.ide.prompt_queue import DurablePromptQueue
    from alysis_code.ide.stdio_bridge import StdioBridge

    output = io.StringIO()
    clients = {}
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", chat_messaging_enabled=True),
    )
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))

    def factory(**kwargs):
        client = ReplyingClient("bridge-model")
        clients[kwargs["session_id_override"]] = client
        kwargs["verification_enabled"] = False
        kwargs["subagents_enabled"] = False
        return create_scripted_session(client, **kwargs)

    bridge = StdioBridge(
        stdout=output,
        create_session_fn=factory,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    try:
        for name in ("alpha", "beta"):
            bridge.process_line(
                json.dumps(
                    {
                        "protocol_version": "1",
                        "id": name,
                        "method": "session.create",
                        "params": {
                            "workspace": str(tmp_path),
                            "mode": "readonly",
                            "model": "bridge-model",
                            "session_id": name,
                            "session_dir": str(tmp_path / "sessions"),
                        },
                    }
                )
            )
        errors = [line for line in output.getvalue().splitlines() if '"error"' in line]
        assert not errors, errors
        a = bridge._sessions["alpha"].agent_session
        b = bridge._sessions["beta"].agent_session
        b.memory_scope = {"project_id": "e9b4a8d0-938a-4bfd-8a0c-60f836200321", "enabled": False}
        b.project_folders = [str(tmp_path)]
        store = a.chat_endpoint.store
        store.link("alpha", "beta")
        sent = a.tools["send_chat_message"].run(
            {"recipient": "beta", "content": "Bridge request", "idempotency_key": "bridge"}
        )
        wait_until(lambda: bool(store.status("alpha", sent["message_id"])["replies"]))
        assert b.memory_scope == {
            "project_id": "e9b4a8d0-938a-4bfd-8a0c-60f836200321",
            "enabled": False,
        }
        assert b.project_folders == [str(tmp_path)]
        assert any(job.session_id == "beta" for job in bridge._jobs.values())
        assert clients["beta"].requests
    finally:
        bridge.close()


def test_model_tool_calls_send_and_wait_then_resume_on_reply(tmp_path):
    from alysis_code.llm.types import LLMResponse, ToolCall

    release = threading.Event()

    class SenderClient(RecordingClient):
        def chat(self, *, messages, tools=None, **kwargs):
            self.requests.append(json.loads(json.dumps(messages)))
            if len(self.requests) == 1:
                names = {t["function"]["name"] for t in tools}
                assert {"send_chat_message", "wait_for_chat_reply"} <= names
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="send",
                            name="send_chat_message",
                            arguments={
                                "recipient": "beta",
                                "content": "Review my API",
                                "idempotency_key": "from_model",
                            },
                        )
                    ],
                    raw={},
                )
            if len(self.requests) == 2:
                sent = json.loads(
                    next(m["content"] for m in reversed(messages) if m.get("role") == "tool")
                )
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="wait",
                            name="wait_for_chat_reply",
                            arguments={"message_id": sent["message_id"]},
                        )
                    ],
                    raw={},
                )
            return LLMResponse(
                content="Waiting" if len(self.requests) == 3 else "Received the peer's answer",
                tool_calls=[],
                raw={},
            )

    class RecipientClient(ReplyingClient):
        def chat(self, **kwargs):
            assert release.wait(5)
            return super().chat(**kwargs)

    sender = SenderClient("sender")
    a = make_session(tmp_path, "alpha", sender)
    b = make_session(tmp_path, "beta", RecipientClient("reviewer"))
    try:
        a.chat_endpoint.store.link("alpha", "beta")
        assert a.run_turn("Coordinate with beta about my API") == 0
        assert len(sender.requests) == 3
        release.set()
        wait_until(lambda: len(sender.requests) == 4)
        assert "Answer from reviewer" in json.dumps(sender.requests[3])
    finally:
        release.set()
        b.close()
        a.close()


def test_rebinding_restored_chat_preserves_logical_address(tmp_path):
    from alysis_code.chat_messaging_runtime import rebind_chat_messaging

    a = make_session(tmp_path, "alpha", RecordingClient("a"))
    old = make_session(tmp_path, "beta", RecordingClient("b"))
    a.chat_endpoint.store.link("alpha", "beta")
    old.close()
    restored = make_session(tmp_path, "new_beta_log", ReplyingClient("restored"))
    try:
        rebind_chat_messaging(restored, "beta")
        sent = a.chat_endpoint.store.send(
            "alpha", "beta", "Still the same chat", idempotency_key="restored"
        )
        wait_until(lambda: bool(a.chat_endpoint.store.status("alpha", sent["id"])["replies"]))
        reply = a.chat_endpoint.store.status("alpha", sent["id"])["replies"][0]
        assert reply["sender"] == "beta"
        assert restored.store.session_id == "new_beta_log"
    finally:
        restored.close()
        a.close()


def test_engine_receiver_wakes_and_releases_saved_chat(tmp_path, monkeypatch):
    from alysis_code import chat_messaging_service as service

    a = make_session(tmp_path, "alpha", RecordingClient("a"))
    b = make_session(tmp_path, "beta", RecordingClient("saved"))
    cfg = b.cfg.model_copy(deep=True)
    b.close()
    monkeypatch.setattr(service, "load_config", lambda: cfg)
    original_restore = service.restore_chat

    def factory(**kwargs):
        return create_scripted_session(
            ReplyingClient(kwargs["cfg"].model),
            **kwargs,
            verification_enabled=False,
            subagents_enabled=False,
        )

    monkeypatch.setattr(
        service,
        "restore_chat",
        lambda name, path: original_restore(name, path, create_session_fn=factory),
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=service.serve_chats, args=(tmp_path / "sessions",), kwargs={"stop": stop}
    )
    store = a.chat_endpoint.store
    try:
        store.link("alpha", "beta")
        sent = store.send("alpha", "beta", "Wake the saved chat", idempotency_key="service")
        thread.start()
        wait_until(lambda: bool(store.status("alpha", sent["id"])["replies"]))

        def released():
            try:
                handle = claim_chat_owner(store, "beta")
            except ChatAlreadyOwned:
                return False
            handle.close()
            return True

        wait_until(released)
        assert a.chat_endpoint.worker.is_alive()
    finally:
        stop.set()
        thread.join(8)
        a.close()
    assert not thread.is_alive()


def test_peer_budget_enforced_even_when_chat_policy_is_autonomous(tmp_path):
    from alysis_code.session_store import read_session_events

    a = make_session(tmp_path, "alpha", RecordingClient("a"))
    b = make_session(
        tmp_path, "beta", RecordingClient("b"), enable_chat_turn_step_budget=True, max_steps=3
    )
    try:
        store = a.chat_endpoint.store
        store.link("alpha", "beta")
        sent = store.send("alpha", "beta", "Bounded question", idempotency_key="budget")
        wait_until(lambda: store.status("alpha", sent["id"])["state"] == "delivered")
        resolutions = [
            e["payload"]
            for e in read_session_events(b.store.path)
            if e["type"] == "turn_step_budget_resolved"
        ]
        assert resolutions[-1]["resolved_max_steps"] == 3
        assert b.chat_turn_fixed_override is None
    finally:
        b.close()
        a.close()


def test_host_cli_links_and_revokes_without_model_authority(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from alysis_code.cli_impl.commands import chat_messaging as commands
    from alysis_code.config import AppConfig

    store = mailbox(tmp_path)
    monkeypatch.setattr(
        commands, "load_config", lambda: AppConfig(session_log_dir=str(tmp_path / "sessions"))
    )
    runner = CliRunner()
    assert runner.invoke(commands.messaging_app, ["link", "alpha", "beta"]).exit_code == 0
    sent = store.send("alpha", "beta", "Approved", idempotency_key="cli")
    assert runner.invoke(commands.messaging_app, ["unlink", "alpha", "beta"]).exit_code == 0
    assert store.status("alpha", sent["id"])["state"] == "cancelled"


@pytest.mark.parametrize(
    "overrides",
    [{"no_log": True}, {"one_shot_execution": True}, {"subagent_depth": 1}, {"tools_override": {}}],
)
def test_narrow_or_ephemeral_sessions_do_not_acquire_mailbox(tmp_path, overrides):
    session = make_session(tmp_path, "narrow", RecordingClient("narrow"), **overrides)
    try:
        assert not hasattr(session, "chat_endpoint")
        assert "send_chat_message" not in session.tools
    finally:
        session.close()


@pytest.mark.parametrize(
    "final_text",
    [
        "I decline to contact the other chat. Keep this private.",
        "Δεν επιτρέπω την προώθηση αυτής της απάντησης.",
        "この回答を他のチャットに転送しないでください。",
        "An ordinary final answer is local unless I explicitly send it.",
    ],
)
def test_peer_final_without_explicit_send_is_never_forwarded(tmp_path, final_text):
    from alysis_code.llm.types import LLMResponse
    from alysis_code.session_store import read_session_events

    class FinalOnlyClient(RecordingClient):
        def chat(self, *, messages, **kwargs):
            self.requests.append(json.loads(json.dumps(messages)))
            return LLMResponse(content=final_text, tool_calls=[], raw={})

    a = make_session(tmp_path, "alpha", RecordingClient("a"))
    b = make_session(tmp_path, "beta", FinalOnlyClient("b"))
    try:
        store = a.chat_endpoint.store
        store.link("alpha", "beta")
        original_history = json.loads(json.dumps(a.messages))
        sent = store.send("alpha", "beta", "Please answer", idempotency_key="decline")
        wait_until(lambda: store.status("alpha", sent["id"])["state"] == "delivered")
        assert store.status("alpha", sent["id"])["replies"] == []
        assert store.inbox("alpha") == []
        assert a.messages == original_history
        events = list(read_session_events(b.store.path))
        assert any(e["type"] == "final" and e["payload"]["content"] == final_text for e in events)
        assert not any(e["type"] == "chat_message_sent" for e in events)
    finally:
        b.close()
        a.close()


@pytest.mark.parametrize("outcome", ["delivered", "failed", "cancelled"])
def test_engine_audits_original_messages_and_publishes_actual_delivery_receipts(tmp_path, outcome):
    from alysis_code.session_store import read_session_events

    a = make_session(tmp_path, "alpha", RecordingClient("a"))
    try:
        store = a.chat_endpoint.store
        store.register("beta", tmp_path)
        store.register("unrelated", tmp_path)
        store.link("alpha", "beta")
        text = "The actual original message Καλημέρα 雲\n<literal peer text>"
        sent = a.tools["send_chat_message"].run(
            {
                "recipient": "beta",
                "content": text,
                "idempotency_key": "visible",
            }
        )
        message_id = sent["message_id"]
        event = next(
            e for e in read_session_events(a.store.path) if e["type"] == "chat_message_sent"
        )
        assert event["payload"]["content"] == text
        assert event["payload"]["sender"] == "alpha"
        assert event["payload"]["recipient"] == "beta"
        assert store.message_states("unrelated") == []
        claimed = store.claim("beta", message_id)
        assert claimed["state"] == "processing"

        def has_receipt(state):
            return any(
                e["type"] == "chat_message_state"
                and e["payload"]["message_id"] == message_id
                and e["payload"]["state"] == state
                for e in read_session_events(a.store.path)
            )

        wait_until(lambda: has_receipt("processing"))
        store.settle("beta", message_id, outcome)
        wait_until(lambda: has_receipt(outcome))
        before = sum(e["type"] == "chat_message_state" for e in read_session_events(a.store.path))
        time.sleep(0.45)
        after = sum(e["type"] == "chat_message_state" for e in read_session_events(a.store.path))
        assert before == after
    finally:
        a.close()
