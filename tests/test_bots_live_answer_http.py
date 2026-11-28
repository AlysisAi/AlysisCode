"""A real worker and its authenticated feed, with a provider paused between actual text deltas."""

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from alysis_code.bots.answer_sections import context_at_start
from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.clarifications import AskQuestion
from alysis_code.bots.models import BotInput, NoteInput, TaskInput
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall


class WritingModel:
    model = "checked-stream"
    supports_tool_calling = True

    def __init__(self):
        self.paused = threading.Event()
        self.resume = threading.Event()
        self.published = False
        self.checks = []
        self.on_check = lambda _: None

    def chat(self, **kw):
        messages = kw["messages"]
        content, calls = "", []
        if messages[0]["content"] == REVIEW_INSTRUCTIONS:
            review = json.loads(messages[1]["content"])
            draft = review["draft_reply"]
            self.checks.append((review["reply_kind"], draft))
            self.on_check(draft)
            bad = "DO-NOT-PUBLISH" in draft
            content = json.dumps(
                {
                    "decision": "repair" if bad else "ready",
                    "reason": "Unsupported." if bad else "Evidence confirmed.",
                }
            )
        elif kw.get("on_text_delta"):
            assert kw["stream"] and not kw["tools"]
            for delta in ("The second ", "checked paragraph.", "\n\n", "Private unfinished tail"):
                kw["on_text_delta"](delta)
            self.paused.set()
            assert self.resume.wait(20)
            content = "The second checked paragraph.\n\nThe final conclusion."
        elif not self.published:
            assert any(s["function"]["name"] == "publish_reply_section" for s in kw["tools"])
            bad_seen = any("DO-NOT-PUBLISH" in text for _, text in self.checks)
            text = "First checked paragraph." if bad_seen else "DO-NOT-PUBLISH"
            self.published = bad_seen
            calls = [
                ToolCall(
                    "section1" if bad_seen else "badsection",
                    "publish_reply_section",
                    {"number": 1, "text": text},
                )
            ]
        else:
            content = "The final conclusion."
        return LLMResponse(content, calls, {}, usage=LLMUsage(4, 3, 7))


@pytest.mark.parametrize("room", [False, True])
def test_only_checked_sections_arrive_in_the_feed_before_the_final_response(tmp_path, room):
    settings = Settings(
        tmp_path / "owls.db", "a" * 40, "example", "https://example.invalid", "controlled"
    )
    model = WritingModel()
    app = create_app(settings, client_factory=lambda _: model)
    store = app.state.store
    owls = [store.save_bot(settings.owner, BotInput(name=name)) for name in ("Scout", "Atlas")]
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        if room:
            chat = client.post(
                "/v1/rooms", json={"name": "Ops", "bot_ids": [b["id"] for b in owls]}
            ).json()
            path = f"/v1/rooms/{chat['id']}/messages"
        else:
            chat = client.post(f"/v1/bots/{owls[0]['id']}/chats", json={}).json()
            path = f"/v1/chats/{chat['id']}/tasks"
        sent = client.post(
            path, json={"request_id": "example_sections", "message": "Write a three-part answer."}
        ).json()
        task = sent["tasks"][0] if room else sent
        try:
            assert model.paused.wait(12), (
                model.checks,
                client.get(f"/v1/tasks/{task['id']}").json().get("error"),
            )
            feed = f"/v1/tasks/{task['id']}/events"
            events = client.post(feed, json={"after": 0, "wait": 0}).json()["events"]
            sections = [e for e in events if e["kind"] == "reply_section"]
            assert [e["data"]["text"] for e in sections] == [
                "First checked paragraph.",
                "The second checked paragraph.",
            ]
            assert "DO-NOT-PUBLISH" not in json.dumps(
                events
            ) and "Private unfinished" not in json.dumps(events)
            assert not client.get(f"/v1/tasks/{task['id']}").json()["output"]
            replay = client.post(feed, json={"after": sections[0]["seq"], "wait": 0}).json()
            assert [e["seq"] for e in replay["events"] if e["kind"] == "reply_section"] == [
                sections[1]["seq"]
            ]
        finally:
            model.resume.set()
        limit = time.monotonic() + 5
        while time.monotonic() < limit:
            result = client.get(f"/v1/tasks/{task['id']}").json()
            if result["status"] == "completed":
                break
            time.sleep(0.03)
        assert (
            result["output"]
            == "First checked paragraph.\n\nThe second checked paragraph.\n\nThe final conclusion."
        )


@pytest.mark.parametrize(
    "enabled,limit,room", [(False, None, False), (True, 2, False), (True, 2, True)]
)
def test_controlled_turn_preserves_review_preference_and_allowance(tmp_path, enabled, limit, room):
    class ShortWriter:
        model = "short"
        supports_tool_calling = True
        reviews = 0

        def chat(self, **kw):
            if kw["messages"][0]["content"] == REVIEW_INSTRUCTIONS:
                self.reviews += 1
                content = '{"decision":"ready","reason":"Checked."}'
            else:
                assert all(
                    s["function"]["name"] != "publish_reply_section" for s in kw.get("tools") or []
                )
                assert not kw.get("on_text_delta")
                content = "A complete answer."
            return LLMResponse(content, [], {}, usage=LLMUsage(4, 3, 7))

    settings = Settings(
        tmp_path / "owls.db",
        "a" * 40,
        "example",
        "https://example.invalid",
        "controlled",
        max_calls=limit,
    )
    writer = ShortWriter()
    app = create_app(settings, client_factory=lambda _: writer)
    owl = app.state.store.save_bot(settings.owner, BotInput(name="Scout"))
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.put("/v1/settings/reply-review", json={"enabled": enabled}).is_success
        data = {"request_id": "short_reply", "message": "Write a complete answer."}
        if room:
            other = app.state.store.save_bot(settings.owner, BotInput(name="Iona"))
            chat = client.post(
                "/v1/rooms", json={"name": "Library", "bot_ids": [owl["id"], other["id"]]}
            ).json()
            task = client.post(
                f"/v1/rooms/{chat['id']}/messages", json={**data, "expertise": True}
            ).json()["tasks"][0]
        else:
            chat = client.post(f"/v1/bots/{owl['id']}/chats", json={}).json()
            task = client.post(f"/v1/chats/{chat['id']}/tasks", json=data).json()
        result = await_result(client, task)
        assert result["output"] == "A complete answer."
        assert writer.reviews == int(enabled)
        assert result["calls"] == 1 + int(enabled)
        assert not [e for e in result["events"] if e["kind"] == "reply_section"]


def await_result(client, task):
    timeout = time.monotonic() + 12
    while time.monotonic() < timeout:
        result = client.get(f"/v1/tasks/{task['id']}").json()
        if result["status"] in ("completed", "failed", "interrupted"):
            return result
        time.sleep(0.03)
    pytest.fail("The Owl did not finish its reply.")


@pytest.mark.parametrize("change", ["note", "clarification"])
def test_context_change_during_stream_review_withdraws_stale_sections(tmp_path, change):
    settings = Settings(
        tmp_path / "owls.db", "a" * 40, "example", "https://example.invalid", "controlled"
    )
    writer = WritingModel()
    app = create_app(settings, client_factory=lambda _: writer)
    store = app.state.store
    owl = store.save_bot(settings.owner, BotInput(name="Scout", memory_enabled=True))
    sent = threading.Event()

    def correct(draft):
        if draft == "The second checked paragraph.":
            assert sent.wait(5)
            if change == "clarification":
                app.state.runtime.clarifications.ask(
                    task, AskQuestion(question="Does the new date apply?")
                )
                return
            store.save_note(
                settings.owner,
                owl["id"],
                NoteInput(title="Correction", content="The person revised the brief."),
            )

    writer.on_check = correct
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        chat = client.post(f"/v1/bots/{owl['id']}/chats", json={}).json()
        task = client.post(
            f"/v1/chats/{chat['id']}/tasks",
            json={"request_id": "correct_reply", "message": "Write a three-part answer."},
        ).json()
        sent.set()
        try:
            assert writer.paused.wait(12)
            events = client.get(f"/v1/tasks/{task['id']}").json()["events"]
            public = [e for e in events if e["kind"] in ("reply_section", "reply_sections_reset")]
            assert [e["kind"] for e in public] == ["reply_section", "reply_sections_reset"]
            assert public[0]["data"]["text"] == "First checked paragraph."
        finally:
            writer.resume.set()
        result = await_result(client, task)
        assert result["output"] == "The final conclusion."


def test_new_worker_continues_after_the_previously_published_section(tmp_path):
    class ResumingWriter:
        model = "resuming"
        supports_tool_calling = True

        def chat(self, **kw):
            if kw["messages"][0]["content"] == REVIEW_INSTRUCTIONS:
                content = '{"decision":"ready","reason":"Confirmed."}'
            else:
                assert any(
                    "already reached the person" in str(m.get("content"))
                    and "First checked result." in str(m.get("content"))
                    for m in kw["messages"]
                )
                content = "The remaining conclusion."
            return LLMResponse(content, [], {}, usage=LLMUsage(4, 3, 7))

    settings = Settings(
        tmp_path / "owls.db", "a" * 40, "example", "https://example.invalid", "controlled"
    )
    store = create_app(settings, client_factory=lambda _: ResumingWriter()).state.store
    owl = store.save_bot(settings.owner, BotInput(name="Scout"))
    chat = store.create_chat(settings.owner, owl["id"], "Review")
    queued = store.enqueue(
        settings.owner,
        chat["id"],
        TaskInput(message="Give me the full result.", request_id="restart_reply"),
    )
    active = store.claim(settings.owner)
    context, _, _ = context_at_start(store, active)
    store.event(
        settings.owner,
        active["id"],
        "reply_section",
        {"number": 1, "text": "First checked result.", "context": context},
    )
    restarted = create_app(settings, client_factory=lambda _: ResumingWriter())
    with TestClient(restarted) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        result = await_result(client, queued)
        assert result["output"] == "First checked result.\n\nThe remaining conclusion."
        assert len([e for e in result["events"] if e["kind"] == "reply_section"]) == 1


def serve_renderer_fixture():
    """Loopback synthetic service for the desktop repository's optional live renderer test."""
    import uvicorn

    with tempfile.TemporaryDirectory() as folder:
        settings = Settings(
            Path(folder) / "stream.db", "a" * 40, "example", "https://example.invalid", "controlled"
        )
        writers = {}

        def model(task_id):
            return writers.setdefault(task_id, WritingModel())

        app = create_app(settings, client_factory=model)
        store = app.state.store
        owls = [
            store.save_bot(settings.owner, BotInput(name=name))
            for name in ("Stream Scout", "Stream Atlas")
        ]
        from alysis_code.bots.rooms import RoomCreate

        room = app.state.runtime.rooms.create(
            RoomCreate(name="Stream room", bot_ids=[b["id"] for b in owls])
        )

        @app.get("/fixture/session")
        def session():
            return {"owl": owls[0], "owls": owls, "room": room}

        @app.post("/fixture/finish")
        def finish():
            for writer in list(writers.values()):
                writer.resume.set()
            return {}

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        config = uvicorn.Config(app, log_level="warning")
        server = uvicorn.Server(config)

        @app.post("/fixture/close")
        def close():
            server.should_exit = True
            return {}

        print(sock.getsockname()[1], flush=True)
        try:
            server.run(sockets=[sock])
        finally:
            sock.close()


if __name__ == "__main__":
    serve_renderer_fixture()
