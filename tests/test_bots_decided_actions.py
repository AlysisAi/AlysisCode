"""An approval the person decided stays in the chat as one quiet line, read from the conversation.

Each task in a conversation lists the actions a person approved or declined, with the action's
tool, arguments and what the owl saw, but never the screenshot the review card showed.
"""

from __future__ import annotations

import pytest

from alysis_code.bots.action_review import ActionReviews, ApprovalDecision, ApprovalRequired
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.store import Store, encode


def pending_review(store, owner, task, action):
    reviews = ActionReviews(store, owner)
    with pytest.raises(ApprovalRequired):
        reviews.request(task, action, "Αυτό στέλνει μήνυμα σε άλλους.")
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO background_turns VALUES(?,'approval',?,?,0)",
            (task["id"], "Waiting for your approval", encode([])),
        )
        db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (task["id"],))
    return reviews, reviews.list(task["bot_id"])[0]


def test_decided_actions_join_their_task_without_the_screenshot(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Ήλιος"))
    chat = store.create_chat("alice", bot["id"], "Συζήτηση")
    store.enqueue("alice", chat["id"], TaskInput(message="Στείλε το email", request_id="request_1"))
    task = store.claim("alice")
    action = {
        "tool": "send_email",
        "arguments": {"to": "μαρία@παράδειγμα.ελ", "subject": "会議"},
        "observed_context": {
            "plugin": "Mail",
            "plugin_tool": "send_email",
            "screenshot": "data:image/png;base64,AAAA",
        },
    }
    reviews, review = pending_review(store, "alice", task, action)

    pending = store.conversation("alice", chat["id"])["tasks"][0]
    assert pending["decided_actions"] == [], "a review still waiting is not a decided line"

    reviews.decide(bot["id"], review["id"], ApprovalDecision(action="deny", expected_revision=1))
    (decided,) = store.conversation("alice", chat["id"])["tasks"][0]["decided_actions"]
    assert decided["id"] == review["id"]
    assert decided["status"] == "denied"
    assert decided["decided_at"] > 0
    assert decided["action"] == {
        "tool": "send_email",
        "arguments": {"to": "μαρία@παράδειγμα.ελ", "subject": "会議"},
        "observed_context": {"plugin": "Mail", "plugin_tool": "send_email"},
    }


def test_an_approval_that_ran_reads_as_approved(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Ήλιος"))
    chat = store.create_chat("alice", bot["id"], "Συζήτηση")
    store.enqueue("alice", chat["id"], TaskInput(message="Τρέξε το", request_id="request_1"))
    task = store.claim("alice")
    action = {"tool": "computer_run", "arguments": {"command": "ls"}, "observed_context": {}}
    reviews, review = pending_review(store, "alice", task, action)
    reviews.decide(bot["id"], review["id"], ApprovalDecision(action="allow", expected_revision=1))
    with store.connect(write=True) as db:
        db.execute("UPDATE bot_action_reviews SET status='consumed' WHERE id=?", (review["id"],))
    (decided,) = store.conversation("alice", chat["id"])["tasks"][0]["decided_actions"]
    assert decided["status"] == "approved"
    assert decided["action"]["arguments"] == {"command": "ls"}


def test_a_conversation_page_carries_decided_actions_and_a_decision_shows_in_the_next_poll(
    tmp_path,
):
    from alysis_code.bots.models import ConversationPageQuery

    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Ήλιος"))
    chat = store.create_chat("alice", bot["id"], "Συζήτηση")
    store.enqueue("alice", chat["id"], TaskInput(message="Στείλε το email", request_id="request_1"))
    task = store.claim("alice")
    action = {"tool": "send_email", "arguments": {"subject": "会議"}, "observed_context": {}}
    reviews, review = pending_review(store, "alice", task, action)

    page = store.conversation_page("alice", chat["id"], ConversationPageQuery())
    assert page["tasks"][0]["decided_actions"] == []
    reviews.decide(bot["id"], review["id"], ApprovalDecision(action="deny", expected_revision=1))
    newest = page["messages"][-1]["id"] if page["messages"] else None
    poll = store.conversation_page(
        "alice", chat["id"], ConversationPageQuery(version=page["version"], after=newest)
    )
    (changed,) = [item for item in poll["tasks"] if item["id"] == task["id"]]
    assert [line["status"] for line in changed["decided_actions"]] == ["denied"]
    assert (
        store.conversation_page("alice", chat["id"], ConversationPageQuery())["tasks"][0][
            "decided_actions"
        ]
        == changed["decided_actions"]
    )
