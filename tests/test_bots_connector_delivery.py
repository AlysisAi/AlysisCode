"""Returned content through the normal Owl/desktop dispatch, with a local synthetic plugin."""

import base64
import io
import json

import pytest
from PIL import Image
from test_bots_browser_images import png_url
from test_bots_desktop_plugins import ASKS, OWN, READ, App, _review, _runtime, _task, response

from alysis_code.bots.action_review import ApprovalDecision
from alysis_code.bots.browser_images import ConnectorImageResult
from alysis_code.bots.connector_delivery import app_action
from alysis_code.bots.desktop_connectors import DesktopConnectors, ResultInput
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_review import ReplyReview
from alysis_code.bots.store import NotFound, Store
from alysis_code.bots.task_results import ReadToolResult, TaskResults


class SyntheticApp(App):
    def __init__(self, hub, evidence, *, is_error=False):
        self.returned = ResultInput(
            ok=True,
            is_error=is_error,
            dispatch_state="dispatched",
            evidence={"version": 1, **evidence},
        )
        super().__init__(hub)

    def run(self):
        while not self.stop.is_set():
            for call in self.hub.next(wait=0.05)["calls"]:
                self.calls.append(call)
                self.hub.answer(call["id"], self.returned)


def tool_receipt(request):
    return json.loads(
        next(m["content"] for m in reversed(request["messages"]) if m["role"] == "tool")
    )


def image_urls(request):
    return [
        p["image_url"]["url"]
        for m in request["messages"]
        if isinstance(m["content"], list)
        for p in m["content"]
        if p.get("type") == "image_url"
    ]


@pytest.mark.parametrize("vision", [True, False])
def test_image_only_change_reaches_eligible_route_once_and_cannot_replay_write(tmp_path, vision):
    store = Store(tmp_path / "bots.sqlite3")
    task = _task(store)
    hub = DesktopConnectors()
    app = SyntheticApp(
        hub, {"text": "", "image": {"mime_type": "image/png", "data": png_url().split(",")[1]}}
    )
    change = response(name=OWN, arguments={"content": "synthetic"})
    try:
        runtime, client = _runtime(
            store, tmp_path, hub, [change, _review("allow"), change, response("Fixture done")]
        )
        client._alysis_browser_image_input = vision
        runtime.run(store.claim("owner"), runtime.task_token(task["id"]))
    finally:
        app.close()
    assert store.task("owner", task["id"])["status"] == "completed"
    assert len(app.calls) == 1 and len(client.requests) == 4
    view = client.requests[2]
    evidence = tool_receipt(view)
    assert evidence["result"] == "" and not evidence["is_error"]
    if vision:
        assert evidence["visual_status"] == "available_for_next_response"
        (url,) = image_urls(view)
        with Image.open(io.BytesIO(base64.b64decode(url.split(",")[1]))) as decoded:
            assert decoded.size == (64, 64) and decoded.convert("RGB").getpixel((32, 32))[2] > 240
    else:
        assert evidence["visual_status"] == "model_cannot_view" and not image_urls(view)
    assert "data:image" not in json.dumps(evidence)
    assert not image_urls(client.requests[-1])
    assert tool_receipt(client.requests[-1])["executed"] is False
    with store.connect() as db:
        events = list(db.execute("SELECT data FROM events WHERE task_id=?", (task["id"],)))
    assert all("data:image" not in event[0] for event in events)
    steps = [
        e
        for e in store.task("owner", task["id"])["events"]
        if e["kind"] in ("tool_started", "tool_completed", "tool_failed")
    ]
    assert steps[0]["data"] == {
        "name": OWN,
        "app_action": {"app": "Todoist", "operation": "add-tasks", "effect": "own_change"},
    }
    assert all(e["data"]["app_action"] == steps[0]["data"]["app_action"] for e in steps)


def test_mixed_result_pages_late_text_without_replaying_or_exposing_it_to_another_task(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    task = _task(store)
    hub = DesktopConnectors()
    text = "  " + "écriture\n" * 9000 + "LIQUIDACIÓN-A87  "
    app = SyntheticApp(
        hub,
        {
            "text": text,
            "structured_content": {"totales": [4, 7]},
            "omitted": {"blocks": 2},
            "image": {"mime_type": "image/png", "data": png_url().split(",")[1]},
        },
    )
    ids = []

    def answers():
        yield response(name=READ, arguments={"content": "synthetic"})
        evidence = tool_receipt(client.requests[-1])
        assert evidence["omitted"] == {"blocks": 2} and evidence["excerpt_incomplete"]
        assert evidence["structured_content"] == {"totales": [4, 7]}
        assert "LIQUIDACIÓN-A87" not in evidence["excerpt"] and image_urls(client.requests[-1])
        ids.append(evidence["stored_result_id"])
        yield response(
            name="read_tool_result", arguments={"result_id": ids[0], "find": "LIQUIDACIÓN"}
        )
        assert "LIQUIDACIÓN-A87" in tool_receipt(client.requests[-1])["content"]
        assert not image_urls(client.requests[-1])
        yield response("Fixture done")

    try:
        runtime, client = _runtime(store, tmp_path, hub, answers())
        client._alysis_browser_image_input = True
        runtime.run(store.claim("owner"), runtime.task_token(task["id"]))
    finally:
        app.close()
    assert store.task("owner", task["id"])["status"] == "completed" and len(app.calls) == 1
    with store.connect() as db:
        raw = db.execute("SELECT content FROM task_tool_results WHERE id=?", (ids[0],)).fetchone()[
            0
        ]
    assert "base64" not in raw
    saved = json.loads(raw)
    assert saved["result"] == text and saved["structured_content"] == {"totales": [4, 7]}
    other = store.enqueue(
        "owner", task["chat_id"], TaskInput(message="A different task", request_id="different-task")
    )
    for owner, task_id in (("owner", other["id"]), ("foreign", task["id"])):
        with pytest.raises(NotFound):
            TaskResults(store, owner).read(task_id, ReadToolResult(result_id=ids[0]))


def test_archive_allowance_preserves_small_error_metadata_without_retry(tmp_path, monkeypatch):
    from alysis_code.bots import task_results

    monkeypatch.setattr(task_results, "MAX_TASK_RESULT_BYTES", 100)
    store = Store(tmp_path / "bots.sqlite3")
    task = _task(store)
    hub = DesktopConnectors()
    app = SyntheticApp(
        hub,
        {"text": "Detailed error. " * 2000, "structured_content": {"receipt": "ERR-7"}},
        is_error=True,
    )
    try:
        runtime, client = _runtime(
            store,
            tmp_path,
            hub,
            [
                response(name=READ, arguments={"content": "fixture"}),
                response("Partial evidence only"),
            ],
        )
        runtime.run(store.claim("owner"), runtime.task_token(task["id"]))
    finally:
        app.close()
    result = tool_receipt(client.requests[-1])
    assert result["is_error"] and result["structured_content"] == {"receipt": "ERR-7"}
    assert (
        result["retention"] == "not_saved"
        and result["excerpt_incomplete"]
        and "stored_result_id" not in result
    )
    assert len(app.calls) == 1
    with store.connect() as db:
        statuses = [
            row[0]
            for row in db.execute(
                "SELECT kind FROM events WHERE task_id=? AND kind IN ('tool_completed','tool_failed')",
                (task["id"],),
            )
        ]
    assert statuses == ["tool_failed"]


def test_checker_compares_actual_pixels_after_paging_and_repairs_without_another_connector_call(
    tmp_path,
):
    store = Store(tmp_path / "bots.sqlite3")
    task = _task(store)
    hub = DesktopConnectors()
    app = SyntheticApp(
        hub,
        {
            "text": "x" * 22_000 + " Référence Z-9",
            "image": {"mime_type": "image/png", "data": png_url().split(",")[1]},
        },
    )

    def answers():
        yield response(name=READ, arguments={"content": "read the blue swatch and text"})
        (source,) = image_urls(client.requests[-1])
        result_id = tool_receipt(client.requests[-1])["stored_result_id"]
        yield response(
            name="read_tool_result", arguments={"result_id": result_id, "find": "Référence"}
        )
        assert not image_urls(client.requests[-1])
        yield response("La couleur est rouge. Référence Z-9.")
        review = client.requests[-1]
        assert not review["tools"] and image_urls(review) == [source]
        assert "data:image" not in review["messages"][1]["content"]
        yield response(
            json.dumps(
                {
                    "decision": "repair",
                    "reason": "The returned swatch is blue. The draft calls it red.",
                    "false_claim": "La couleur est rouge.",
                    "issue": "contradiction",
                }
            )
        )
        assert image_urls(client.requests[-1]) == [source]
        yield response("La couleur est bleue. Référence Z-9.")
        assert not client.requests[-1]["tools"] and image_urls(client.requests[-1]) == [source]
        yield response(
            json.dumps(
                {"decision": "ready", "reason": "The actual swatch and retrieved reference agree."}
            )
        )

    try:
        runtime, client = _runtime(store, tmp_path, hub, answers())
        client._alysis_browser_image_input = True
        runtime.reply_review = ReplyReview(runtime)
        runtime.run(store.claim("owner"), runtime.task_token(task["id"]))
    finally:
        app.close()
    final = store.task("owner", task["id"])
    assert (
        final["status"] == "completed" and final["output"] == "La couleur est bleue. Référence Z-9."
    )
    assert len(app.calls) == 1
    assert [
        e["data"]["decision"] for e in final["events"] if e["kind"] == "reply_review_finished"
    ] == ["repair", "ready"]


@pytest.mark.parametrize("mismatch", ["owner", "task", "text_model"])
def test_checker_withholds_foreign_or_unsupported_visual(tmp_path, mismatch):
    from test_bots_reply_review import study, verdict

    _, task, model, runtime, token = study(tmp_path, [verdict("ready")])
    model._alysis_browser_image_input = mismatch != "text_model"
    image = ConnectorImageResult(
        {},
        png_url(),
        caption="private-source-caption",
        owner="other" if mismatch == "owner" else "owner",
        task_id="other-task" if mismatch == "task" else task["id"],
    )
    runtime.reply_review.check(
        task, model, token, [], [], "I could not inspect it.", 12, connector_image=image
    )
    request = model.requests[0]
    assert not image_urls(request) and "private-source-caption" not in json.dumps(
        request["messages"]
    )


def test_progress_uses_only_safe_catalog_labels(tmp_path):
    assert app_action(
        {
            "plugin_name": "Aperçu Café",
            "tool_name": "lire_facture",
            "effect": "read",
            "account": "private",
        }
    ) == {"app_action": {"app": "Aperçu Café", "operation": "lire_facture", "effect": "read"}}
    for unsafe in (
        "account@example.test",
        "folder/file",
        "line\nbreak",
        "app\u202esecret",
        "a" * 129,
    ):
        assert app_action({"plugin_name": unsafe, "tool_name": "read", "effect": "read"}) == {}
    store = Store(tmp_path / "bots.sqlite3")
    task = _task(store)
    store.claim("owner")
    action = {"app": "Aperçu Café", "operation": "lire_facture", "effect": "read"}
    store.event("owner", task["id"], "tool_started", {"name": READ, "app_action": action})
    assert store.list_bots("owner")[0]["now_doing"]["app_action"] == action


def test_approval_checkpoint_does_not_replay_an_earlier_change_or_keep_its_image(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    task = _task(store)
    hub = DesktopConnectors()
    app = SyntheticApp(
        hub, {"text": "", "image": {"mime_type": "image/png", "data": png_url().split(",")[1]}}
    )
    first = response(name=OWN, arguments={"content": "make record once"})
    second = response(name=ASKS, arguments={"content": "share record once"})
    try:
        runtime, client = _runtime(store, tmp_path, hub, [first, _review("allow"), second])
        client._alysis_browser_image_input = True
        runtime.run(store.claim("owner"), runtime.task_token(task["id"]))
        assert store.task("owner", task["id"])["status"] == "waiting"
        (review,) = runtime.action_reviews.list(task["bot_id"])
        runtime.action_reviews.decide(
            task["bot_id"],
            review["id"],
            ApprovalDecision(action="allow", expected_revision=review["revision"]),
        )
        resumed, next_model = _runtime(store, tmp_path, hub, [first, second, response("Done")])
        next_model._alysis_browser_image_input = True
        pending = resumed.background.claim()
        checkpoint = resumed.background.checkpoint(task["id"])
        assert checkpoint[0]["_completed_connector_changes"] and "data:image" not in json.dumps(
            checkpoint
        )
        resumed.run(pending, resumed.task_token(task["id"]), background=True)
    finally:
        app.close()
    assert store.task("owner", task["id"])["status"] == "completed"
    assert [call["name"] for call in app.calls] == ["add-tasks", "add-comments"]
    assert tool_receipt(next_model.requests[1])["executed"] is False
    assert all(
        "_completed_connector_changes" not in json.dumps(r["messages"]) for r in next_model.requests
    )


def test_fully_reported_identical_changes_take_the_ordinary_approval_path(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("owner", BotInput(name="Fixture"))
    chat = store.create_chat("owner", bot["id"], "Two entries")
    task = store.enqueue(
        "owner",
        chat["id"],
        TaskInput(message="Create two separate, identical test entries.", request_id="two-entries"),
    )
    hub = DesktopConnectors()
    app = SyntheticApp(hub, {"text": "The synthetic entry was saved."})
    change = response(name=OWN, arguments={"content": "one of two deliberate entries"})
    try:
        runtime, client = _runtime(
            store,
            tmp_path,
            hub,
            [
                change,
                _review("allow"),
                change,
                _review("allow"),
                response("Created the two requested entries."),
            ],
        )
        runtime.run(store.claim("owner"), runtime.task_token(task["id"]))
    finally:
        app.close()
    assert store.task("owner", task["id"])["status"] == "completed"
    assert [c["approval"] for c in app.calls] == ["automatic", "automatic"]
    assert [tool_receipt(client.requests[i])["result"] for i in (2, 4)] == [
        "The synthetic entry was saved."
    ] * 2
