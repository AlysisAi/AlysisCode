"""Rooms over HTTP: the capability, the routes, their refusals, and two owls answering in order."""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotInput
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS
from alysis_code.bots.room_routing import INSTRUCTIONS as ROUTING_INSTRUCTIONS
from alysis_code.llm.types import LLMResponse, LLMUsage


class RoomModel:
    """Answers as the owl whose turn it is, and records what each turn read of the room."""

    __test__ = False
    model = "provider-fixture"
    supports_tool_calling = True

    def __init__(self):
        self.reads: list[list[dict]] = []
        self.routes: list[dict] = []

    def chat(self, **kwargs):
        messages = kwargs["messages"]
        if messages[0]["content"] == ROUTING_INSTRUCTIONS:
            context = json.loads(messages[1]["content"])
            self.routes.append(context)
            return LLMResponse(
                json.dumps({"member_id": context["members"][1]["id"], "confident": True}),
                [],
                {},
                usage=LLMUsage(8, 5, 13),
            )
        if messages[0]["content"] == REVIEW_INSTRUCTIONS:
            return LLMResponse(
                '{"decision":"ready","reason":"Scripted fixture reply."}',
                [],
                {},
                usage=LLMUsage(3, 2, 5),
            )
        self.reads.append(messages)
        note = next(
            m["content"] for m in messages if str(m["content"]).startswith("This turn is in a room")
        )
        name = note.split("You are ", 1)[1].split(",", 1)[0].strip('"')
        return LLMResponse(f"{name}: εντάξει / 了解", [], {}, usage=LLMUsage(10, 5, 15))


def prepared(tmp_path):
    settings = Settings(
        tmp_path / "rooms.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    model = RoomModel()
    app = create_app(settings, client_factory=lambda _: model)
    store = app.state.store
    owls = [
        store.save_bot(settings.owner, BotInput(name=name)) for name in ("Νεφέλη", "さくら", "Ben")
    ]
    return settings, app, owls, model


def wait_done(client, task_ids):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        states = [client.get(f"/v1/tasks/{task_id}").json()["status"] for task_id in task_ids]
        if all(state == "completed" for state in states):
            return
        time.sleep(0.02)
    raise AssertionError(f"The room's turns didn't finish: {states}")


def test_room_routes_by_model_and_carries_actual_file_revisions_to_the_selected_owl(tmp_path):
    settings, app, owls, model = prepared(tmp_path)
    first, second, outside = [o["id"] for o in owls]
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        room = client.post(
            "/v1/rooms", json={"name": "Dispatch", "bot_ids": [first, second]}
        ).json()
        file = client.post(
            f"/v1/bots/{first}/files",
            json={"name": "delivery.csv", "content": "town,count\nVolos,7"},
        ).json()
        data = {
            "message": "Ποιος μπορεί να ελέγξει το φορτίο;",
            "request_id": "room_route_file_1",
            "expertise": True,
            "files": [{"bot_id": first, "file_id": file["id"]}],
        }
        path = f"/v1/rooms/{room['id']}/messages"
        sent = client.post(path, json=data)
        assert sent.status_code == 202, sent.text
        task_id = sent.json()["tasks"][0]["id"]
        wait_done(client, [task_id])
        assert model.routes[0]["request"] == data["message"]
        assert [member["id"] for member in model.routes[0]["members"]] == [first, second]
        page = client.post(f"/v1/chats/{room['id']}/page", json={"limit": 20}).json()
        assert page["tasks"][0]["bot_id"] == second
        copy = page["messages"][0]["files"][0]["id"]
        assert copy != file["id"]
        assert (
            client.get(f"/v1/bots/{second}/files/{copy}").json()["content"] == "town,count\nVolos,7"
        )
        assert any(copy in str(m.get("content")) for messages in model.reads for m in messages)
        assert client.post(path, json=data).json()["tasks"][0]["id"] == task_id
        assert client.post(path, json={**data, "files": []}).status_code == 409
        assert (
            client.post(
                path,
                json={
                    **data,
                    "request_id": "crossroom_files",
                    "files": [{"bot_id": outside, "file_id": file["id"]}],
                },
            ).status_code
            == 409
        )
        explicit = client.post(
            path, json={**data, "request_id": "room_explicit_to", "to": first}
        ).json()["tasks"][0]
        wait_done(client, [explicit["id"]])
        assert client.get(f"/v1/tasks/{explicit['id']}").json()["bot_id"] == first
        assert len(model.routes) == 1


def test_status_names_rooms_and_the_routes_check_what_they_take(tmp_path):
    settings, app, owls, _ = prepared(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/rooms").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "owl_rooms" in client.get("/v1/status").json()["capabilities"]
        assert client.get("/v1/rooms").json() == []
        for bad in (
            {"name": "Μία", "bot_ids": [owls[0]["id"]]},
            {"name": "Διπλή", "bot_ids": [owls[0]["id"], owls[0]["id"]]},
            {"name": "", "bot_ids": [owls[0]["id"], owls[1]["id"]]},
            {"name": "Άλλο", "bot_ids": [owls[0]["id"], owls[1]["id"]], "extra": 1},
        ):
            assert client.post("/v1/rooms", json=bad).status_code == 422
        assert (
            client.post("/v1/rooms", json={"name": "Ξένη", "bot_ids": [owls[0]["id"], "f" * 32]})
        ).status_code == 404
        made = client.post(
            "/v1/rooms", json={"name": "Ταξίδι", "bot_ids": [owls[0]["id"], owls[1]["id"]]}
        )
        assert made.status_code == 201
        room = made.json()
        assert [owl["name"] for owl in room["owls"]] == ["Νεφέλη", "さくら"] and room["open"]
        assert client.get("/v1/rooms").json() == [room]
        assert client.get(f"/v1/rooms/{room['id']}").json() == room
        assert client.get(f"/v1/rooms/{'f' * 32}").status_code == 404
        assert client.patch(f"/v1/rooms/{room['id']}", json={}).status_code == 422
        renamed = client.patch(f"/v1/rooms/{room['id']}", json={"name": "旅行"}).json()
        assert renamed["name"] == "旅行"
        changed = client.patch(
            f"/v1/rooms/{room['id']}", json={"bot_ids": [owls[1]["id"], owls[2]["id"]]}
        ).json()
        assert [owl["name"] for owl in changed["owls"]] == ["さくら", "Ben"]
        path = f"/v1/rooms/{room['id']}/messages"
        for bad in (
            {"message": "", "request_id": "room_msg_1"},
            {"message": "γεια", "request_id": "short"},
            {"message": "γεια", "request_id": "room_msg_1", "extra": 1},
        ):
            assert client.post(path, json=bad).status_code == 422
        refused = client.post(
            path, json={"message": "γεια", "request_id": "room_msg_2", "to": owls[0]["id"]}
        )
        assert refused.status_code == 409
        # The room's chat takes the person's messages only through the room.
        assert (
            client.post(
                f"/v1/chats/{room['id']}/tasks",
                json={"message": "γεια", "request_id": "chat_msg_1"},
            ).status_code
            == 409
        )
        assert client.delete(f"/v1/rooms/{room['id']}").status_code == 204
        assert client.get("/v1/rooms").json() == []
        assert client.post(f"/v1/chats/{room['id']}/page", json={"limit": 10}).status_code == 404
        assert len(client.get("/v1/bots").json()) == 3


def test_two_owls_answer_a_message_in_order_and_the_room_reads_like_a_chat(tmp_path):
    settings, app, owls, model = prepared(tmp_path)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        room = client.post(
            "/v1/rooms",
            json={"name": "Σχέδια", "bot_ids": [owl["id"] for owl in owls]},
        ).json()
        sent = client.post(
            f"/v1/rooms/{room['id']}/messages",
            json={"message": "@さくら και @Νεφέλη, πού πάμε;", "request_id": "room_msg_3"},
        )
        assert sent.status_code == 202
        result = sent.json()
        assert result["to"] == [owls[1]["id"], owls[0]["id"]]
        wait_done(client, [task["id"] for task in result["tasks"]])
        page = client.post(f"/v1/chats/{room['id']}/page", json={"limit": 10}).json()
        assert [(m["role"], m["content"]) for m in page["messages"]] == [
            ("user", "@さくら και @Νεφέλη, πού πάμε;"),
            ("assistant", "さくら: εντάξει / 了解"),
            ("assistant", "Νεφέλη: εντάξει / 了解"),
        ]
        # The second owl read the first one's answer, labelled as that owl's words.
        second = next(
            m["content"] for m in model.reads[-1] if str(m["content"]).startswith("This turn")
        )
        assert '{"from":"owl","name":"さくら","said":"さくら: εντάξει / 了解"}' in second
        listed = client.get(f"/v1/rooms/{room['id']}").json()
        assert listed["unread"] and listed["latest_activity"]["bot_id"] == owls[0]["id"]
        read = client.post(
            f"/v1/chats/{room['id']}/read", json={"activity_seq": listed["activity_seq"]}
        )
        assert read.status_code == 200
        assert not client.get(f"/v1/rooms/{room['id']}").json()["unread"]
        # Each owl's own chat has none of it.
        for owl in owls:
            assert client.get(f"/v1/bots/{owl['id']}/chats").json() == []
        # Sent again with its request id: the same turns.
        again = client.post(
            f"/v1/rooms/{room['id']}/messages",
            json={"message": "@さくら και @Νεφέλη, πού πάμε;", "request_id": "room_msg_3"},
        ).json()
        assert [t["id"] for t in again["tasks"]] == [t["id"] for t in result["tasks"]]
        # A deleted owl leaves the room.
        assert client.delete(f"/v1/bots/{owls[2]['id']}").status_code == 204
        assert [o["name"] for o in client.get(f"/v1/rooms/{room['id']}").json()["owls"]] == [
            "Νεφέλη",
            "さくら",
        ]
