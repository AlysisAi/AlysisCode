"""Desktop controls exercise the actual bridge, broker and checkpoint ledger offline."""

from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest
from test_ide_stdio_bridge import _isolate_alysis_state, _send_bridge_request, _wait_for_line

from alysis_code.cli_impl.commands.chat_resume_helpers import _load_chat_resume_messages
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.change_ledger import ChangeLedger, ChangeLedgerError
from alysis_code.ide.stdio_bridge import StdioBridge
from alysis_code.session_store import SessionStore
from alysis_code.user_questions import UserQuestionBroker

QUESTIONS = {
    "questions": [
        {
            "id": "direction",
            "question": "Ποια κατεύθυνση;",
            "kind": "single",
            "options": ["Εδώ", "Εκεί"],
        }
    ]
}


def test_live_questions_are_job_bound_and_answers_survive_worker_collection(tmp_path, monkeypatch):
    _isolate_alysis_state(tmp_path, monkeypatch)
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    out = io.StringIO()
    asked, collected, finish = threading.Event(), threading.Event(), threading.Event()
    received = []

    class Session:
        def __init__(self, **kwargs):
            self.cfg, self.surface, self.messages = kwargs["cfg"], kwargs["surface"], []

        def run_turn(self, message):
            broker = self.surface.user_questions
            pending = broker.request(QUESTIONS)
            asked.set()
            received.append(broker.collect(pending["request_id"]))
            collected.set()
            finish.wait(3)
            return 0

        def close(self):
            finish.set()

    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: Session(**kwargs))

    def send(method, params):
        return _send_bridge_request(bridge, out, method, params)

    try:
        created = send(
            "session.create",
            {"workspace": str(tmp_path), "mode": "readonly", "user_questions": True},
        )
        assert created["ok"], created
        session_id = created["result"]["session_id"]
        sent = send("chat.send", {"session_id": session_id, "message": "Choose a direction."})
        job_id = sent["result"]["job_id"]
        assert asked.wait(3)
        owner = {"session_id": session_id, "job_id": job_id}
        current = send("session.userQuestions.get", owner)["result"]["questions"][0]
        assert current["status"] == "pending"
        assert current["questions"] == QUESTIONS["questions"]
        wrong = send(
            "session.userQuestions.touch",
            {**owner, "job_id": "another-job", "request_id": current["request_id"]},
        )
        assert wrong["ok"] is False
        busy = send("session.compact", {"session_id": session_id})
        assert busy["error"]["code"] == "session_busy"
        answers = [{"id": "direction", "selected": ["Εκεί"], "text": "日本語の説明"}]
        answer = send(
            "session.userQuestions.answer",
            {**owner, "request_id": current["request_id"], "answers": answers},
        )
        assert answer["ok"], answer
        assert collected.wait(3)
        assert received[0]["answers"] == answers
        assert (
            send("session.userQuestions.get", owner)["result"]["questions"][0]["status"]
            == "answered"
        )
        assert (
            send(
                "session.userQuestions.answer",
                {**owner, "request_id": current["request_id"], "answers": answers},
            )["ok"]
            is False
        )
        finish.set()
        _wait_for_line(out, lambda line: "job_completed" in str(line))
        send("session.status", {"session_id": session_id})
        assert (
            send("session.userQuestions.get", owner)["result"]["questions"][0]["status"]
            == "answered"
        )
        assert (
            send("session.userQuestions.touch", {**owner, "request_id": current["request_id"]})[
                "ok"
            ]
            is False
        )
    finally:
        finish.set()
        bridge.close()


def test_broker_presentation_does_not_consume_and_retains_fast_batches():
    now = [0.0]
    broker = UserQuestionBroker(clock=lambda: now[0])
    first = broker.request(QUESTIONS)["request_id"]
    assert broker.snapshot()["status"] == "pending"
    now[0] = 50
    assert broker.touch(first)
    now[0] = 61
    assert broker.snapshot()["status"] == "pending"
    now[0] = 111
    assert broker.snapshot()["status"] == "timed_out"
    result = broker.collect(first)
    assert result["answers"] == []
    assert "never grants permission" in result["guidance"]
    assert not broker.touch(first)
    second = broker.request(
        {
            "questions": [
                {"id": "notes", "question": "Anything else?", "kind": "text", "options": []}
            ]
        }
    )["request_id"]
    broker.submit(second, [{"id": "notes", "selected": [], "text": "Keep it small"}])
    assert [q["status"] for q in broker.snapshots()] == ["timed_out", "answered"]
    assert broker.collect(second)["status"] == "answered"
    broker.reset()
    assert broker.snapshots() == []


def test_compaction_retains_full_history_and_persists_for_restart(tmp_path, monkeypatch):
    _isolate_alysis_state(tmp_path, monkeypatch)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    retained = sessions / "retained.jsonl"
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"}
        for i in range(602)
    ]
    retained.write_text(
        "\n".join(
            json.dumps(
                {
                    "type": "user_message" if m["role"] == "user" else "assistant_message",
                    "payload": {"content": m["content"]},
                }
            )
            for m in history
        )
        + "\n"
    )
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", session_log_dir=str(sessions)),
    )
    created = []

    class Compactor:
        state = SimpleNamespace(history_chunk_index=0, pins=[])

        def compact_now(self, **kwargs):
            assert kwargs["focus"] == "Προτεραιότητες 日本語"
            assert (
                sum(m.get("content", "").startswith("message ") for m in kwargs["messages"]) == 602
            )
            return [{"role": "user", "content": "Summary of every message"}], True

    class Session:
        def __init__(self, **kwargs):
            self.cfg, self.messages, self.startup_messages = kwargs["cfg"], [], []
            self.pinned_prefix_len = 0
            self.client = SimpleNamespace(model="test-model")
            self.tool_list = []
            self.conversation_compactor = Compactor()
            self.store = SessionStore(
                sessions_dir=sessions,
                enabled=True,
                session_id=kwargs["session_id_override"],
                cwd=str(tmp_path),
                repo_root=None,
            )
            created.append(self)

        def close(self):
            self.store.close()

    out = io.StringIO()
    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: Session(**kwargs))

    def send(method, params):
        return _send_bridge_request(bridge, out, method, params)

    try:
        response = send(
            "session.create",
            {"workspace": str(tmp_path), "session_id": "retained", "mode": "readonly"},
        )
        assert response["ok"], response
        before = retained.read_bytes()
        resumed = send(
            "session.resume",
            {
                "session_id": "retained",
                "target_session_id": "retained",
                "full_history": True,
                "persist": False,
            },
        )
        assert resumed["ok"], resumed
        assert resumed["result"]["history_count"] == 602
        assert resumed["result"]["bounded"] is False
        assert retained.read_bytes() == before
        compact = send(
            "session.compact", {"session_id": "retained", "focus": "Προτεραιότητες 日本語"}
        )
        assert compact["ok"], compact
        assert compact["result"]["changed"] is True
        resumed_messages = _load_chat_resume_messages(retained)
        assert resumed_messages == [{"role": "user", "content": "Summary of every message"}]
    finally:
        bridge.close()


def test_redo_refuses_changed_review_atomically(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    file = workspace / "a.txt"
    file.write_text("before")
    ledger = ChangeLedger(workspace, storage_root=tmp_path / "ledger")
    ledger.ensure_baseline("chat")
    file.write_text("after")
    checkpoint = ledger.capture("chat", turn_id="turn", message="edit", paths=["a.txt"])
    undone = ledger.revert("chat", checkpoint.checkpoint_id)
    with pytest.raises(ChangeLedgerError, match="no longer current"):
        ledger.redo("chat", expected_checkpoint_id=checkpoint.checkpoint_id)
    assert file.read_text() == "before"
    ledger.redo("chat", expected_checkpoint_id=undone.checkpoint_id)
    assert file.read_text() == "after"
