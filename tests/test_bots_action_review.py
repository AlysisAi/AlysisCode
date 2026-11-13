"""Provider-free action authority and durable pause/restart tests."""

import json
from dataclasses import replace

import pytest
from test_bots_browser_runtime import ClientFixture, response
from test_bots_desktop_runtime import run_task
from test_bots_desktop_runtime import study as execution_study

from alysis_code.bots.action_review import (
    ActionReviews,
    ApprovalDecision,
    ReviewSettings,
)
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound

study = execution_study


@pytest.fixture(autouse=True)
def reviewed_surface(monkeypatch):
    """These flows exercise review, pause and resume end to end on the desktop fixture. The
    owl's private computer no longer waits for review in automatic mode (see
    test_bots_approval_rules), so drive it here as a reviewed surface, as the person's own
    computer and websites still are."""
    from alysis_code.bots.action_review import BotActionReviewer

    original = BotActionReviewer.check
    monkeypatch.setattr(
        BotActionReviewer,
        "check",
        lambda self, name, arguments, context=None, **_: original(self, name, arguments, context),
    )


def review_response(decision, reason="Fixture assessment", **fields):
    return response(
        json.dumps(
            {
                "decision": decision,
                "risk": "low",
                "user_authorization": "high",
                "reason": reason,
                **fields,
            }
        )
    )


def test_disabling_answer_review_keeps_automatic_action_review_and_human_approval(study):
    from alysis_code.bots.reply_review import ReplyReview
    from alysis_code.bots.store import encode

    store, task, _, instances = study
    task["config"]["reply_review_enabled"] = False
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET config=? WHERE id=?", (encode(task["config"]), task["id"]))
    runtime, client = runtime_for(
        study,
        [
            response(name="computer_run", arguments={"command": "printf human > result.txt"}),
            review_response("ask_person", "Please confirm"),
        ],
    )
    runtime.reply_review = ReplyReview(runtime)
    run_task(runtime, task)
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert len(client.requests) == 2
    assert all(not instance.calls for instance in instances)
    (review,) = runtime.action_reviews.list(task["bot_id"])
    assert review["status"] == "pending"
    runtime.action_reviews.decide(
        task["bot_id"], review["id"], ApprovalDecision(action="deny", expected_revision=1)
    )
    assert store.task("owner", task["id"])["status"] == "cancelled"


def runtime_for(study, answers):
    store, task, manager, instances = study
    client = ClientFixture(answers)
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.action_review_enabled = True
    return runtime, client


def test_auto_allows_exact_command_and_records_separate_review(study):
    runtime, client = runtime_for(
        study,
        [
            response(name="computer_run", arguments={"command": "printf hello > result.txt"}),
            review_response("allow", "Requested work"),
            response("Done"),
        ],
    )
    store, task, _, instances = study
    run_task(runtime, task)
    result = store.task("owner", task["id"])
    assert result["status"] == "completed", result["error"]
    assert [c[0] for c in instances[0].calls] == ["run"]
    reviews = [e["data"] for e in result["events"] if e["kind"] == "action_review"]
    assert len(reviews) == 1 and reviews[0]["outcome"] == "allow"
    assert len(result["usage"]) == 3
    assert any(e["kind"] == "action_review_call" for e in result["events"])
    assert "Prepare work" in str(client.requests[1]["messages"])
    assert "Inspect the requested file." not in str(client.requests[1]["messages"])


def test_deny_never_dispatches_and_returns_feedback(study):
    runtime, _ = runtime_for(
        study,
        [
            response(name="computer_run", arguments={"command": "printf rejected > result.txt"}),
            review_response("deny", "Not requested"),
            response("I cannot do that action"),
        ],
    )
    store, task, _, instances = study
    run_task(runtime, task)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert all(not instance.calls for instance in instances)


def test_human_handoff_survives_recovery_and_resumes_once(study):
    runtime, client = runtime_for(
        study,
        [
            response(name="computer_run", arguments={"command": "printf human > result.txt"}),
            review_response("ask_person", "Please confirm"),
        ],
    )
    store, task, manager, instances = study
    run_task(runtime, task)
    result = store.task("owner", task["id"])
    assert result["status"] == "waiting", result
    assert all(not instance.calls and instance.closed for instance in instances)
    assert not manager._computers
    (review,) = runtime.action_reviews.list(task["bot_id"])
    assert review["status"] == "pending"
    assert store.recover("owner") == 0
    decisions = ActionReviews(store, "owner")
    data = ApprovalDecision(action="allow", expected_revision=review["revision"])
    decisions.decide(task["bot_id"], review["id"], data)
    decisions.decide(task["bot_id"], review["id"], data)
    resumed, _ = runtime_for(
        study,
        [
            response(name="computer_run", arguments={"command": "printf human > result.txt"}),
            response("Done after permission"),
        ],
    )
    claimed = resumed.background.claim()
    assert claimed is not None
    resumed.run(claimed, resumed.task_token(task["id"]), background=True)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert sum(len(instance.calls) for instance in instances) == 1
    assert decisions.list(task["bot_id"], include_history=True)[0]["status"] == "consumed"
    assert resumed.background.claim() is None


@pytest.mark.parametrize("change", ["cancel", "correction", "other_owner"])
@pytest.mark.parametrize("decision", ["allow", "always_allow"])
def test_stale_or_cross_owner_approval_never_resumes(study, change, decision):
    runtime, _ = runtime_for(
        study,
        [
            response(name="computer_run", arguments={"command": "printf safe > result.txt"}),
            response("unreadable"),
        ],
    )
    store, task, _, instances = study
    run_task(runtime, task)
    (review,) = runtime.action_reviews.list(task["bot_id"])
    if change == "cancel":
        store.cancel("owner", task["id"])
    elif change == "correction":
        from alysis_code.bots.models import TaskInput

        store.enqueue(
            "owner",
            task["chat_id"],
            TaskInput(message="Stop and change this request", request_id="changed-request"),
        )
    handler = ActionReviews(store, "other") if change == "other_owner" else runtime.action_reviews
    with pytest.raises((Conflict, NotFound)):
        handler.decide(
            task["bot_id"], review["id"], ApprovalDecision(action=decision, expected_revision=1)
        )
    assert runtime.background.claim() is None
    assert all(not i.calls for i in instances)


def test_ask_mode_does_not_spend_reviewer_call_and_deny_stops(study):
    runtime, client = runtime_for(
        study, [response(name="computer_run", arguments={"command": "printf pending"})]
    )
    store, task, _, instances = study
    runtime.action_reviews.settings(task["bot_id"], ReviewSettings(mode="review"))
    run_task(runtime, task)
    assert len(client.requests) == 1
    (review,) = runtime.action_reviews.list(task["bot_id"])
    runtime.action_reviews.decide(
        task["bot_id"], review["id"], ApprovalDecision(action="deny", expected_revision=1)
    )
    assert store.task("owner", task["id"])["status"] == "cancelled"
    assert runtime.background.claim() is None
    assert all(not i.calls for i in instances)


def test_user_grant_requires_same_observed_page_and_is_single_use(study):
    from alysis_code.bots.action_review import ApprovalRequired

    store, task, _, _ = study
    reviews = ActionReviews(store, "owner")
    action = {
        "tool": "browser_click",
        "arguments": {"source_id": "old", "node": 5},
        "observed_context": {
            "url": "https://example.org",
            "target": {"text": "Submit"},
            "page": {"amount": "17.25"},
        },
    }
    with pytest.raises(ApprovalRequired):
        reviews.request(task, action, "Review submission")
    reviews.pause(task, [])
    (review,) = reviews.list(task["bot_id"])
    reviews.decide(
        task["bot_id"], review["id"], ApprovalDecision(action="allow", expected_revision=1)
    )
    from alysis_code.bots.background import BackgroundStore

    BackgroundStore(store, "owner").claim()
    changed = {
        **action,
        "arguments": {**action["arguments"], "source_id": "new"},
        "observed_context": {**action["observed_context"], "page": {"amount": "999"}},
    }
    assert not reviews.consume(task, changed)
    same = {**action, "arguments": {**action["arguments"], "source_id": "new"}}
    assert reviews.consume(task, same)
    assert not reviews.consume(task, same)


def test_approval_pause_completes_every_unexecuted_tool_call(study):
    import json

    from alysis_code.bots.action_review import ApprovalRequired

    store, task, _, _ = study
    reviews = ActionReviews(store, "owner")
    with pytest.raises(ApprovalRequired):
        reviews.request(task, {"tool": "computer_run", "arguments": {"command": "true"}}, "Check")
    calls = [
        {"id": key, "type": "function", "function": {"name": "computer_run", "arguments": "{}"}}
        for key in ["done", "pending", "not-started"]
    ]
    messages = [
        {"role": "assistant", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "done", "content": "actual result"},
    ]
    reviews.pause(task, messages)
    with store.connect() as db:
        checkpoint = json.loads(
            db.execute(
                "SELECT messages FROM background_turns WHERE task_id=?", (task["id"],)
            ).fetchone()[0]
        )
    outputs = [m for m in checkpoint if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in outputs] == ["done", "pending", "not-started"]
    assert outputs[0]["content"] == "actual result"
    assert all(json.loads(m["content"])["executed"] is False for m in outputs[1:])


def test_usage_counts_action_checks_separately_from_execution(study):
    runtime, _ = runtime_for(
        study,
        [
            response(name="computer_run", arguments={"command": "touch result.txt"}),
            review_response("allow", "Fixture assessment"),
            response("Done"),
        ],
    )
    store, task, _, _ = study
    run_task(runtime, task)
    usage = runtime.action_reviews.usage(task["bot_id"])
    assert usage["actions"]["calls"] == 1
    assert usage["replies"]["calls"] == 0
    assert store.usage("owner")["calls"] == 3


def test_pause_preserves_form_draft_without_base64_in_resume_context(study):
    import json
    from types import SimpleNamespace

    from alysis_code.bots.action_review import ApprovalRequired
    from alysis_code.bots.clarifications import ReadBrowserDraft

    store, task, _, _ = study
    runtime, _ = runtime_for(study, [])
    original = {"complete": True, "pages": [{"fields": [{"value": "Νεφέλη / 東京"}]}]}
    lifecycle = []

    def capture(current):
        assert current["id"] == task["id"]
        lifecycle.append("checkpoint")
        return original

    browser = SimpleNamespace(
        checkpoint_draft=capture,
        release=lambda *args, **kwargs: lifecycle.append("release"),
    )
    runtime.browser_computers = browser
    runtime.computer_managers = (browser,)
    pixels = "data:image/png;base64,c3ludGhldGljLXJlZ3Jlc3Npb24="
    action = {
        "tool": "browser_visual_click",
        "arguments": {"source_id": "old", "x": 10, "y": 20},
        "observed_context": {"screenshot": pixels},
    }
    with pytest.raises(ApprovalRequired):
        runtime.action_reviews.request(task, action, "Review the proposed click")
    runtime._release_computers(task["id"], cancelled=False)
    assert lifecycle == ["checkpoint", "release"]
    runtime.action_reviews.pause(task, [])
    saved = runtime.clarifications.read_browser_draft(
        task, ReadBrowserDraft(source_task_id=task["id"])
    )
    assert json.loads(saved["content"]) == original
    with store.connect() as db:
        checkpoint = db.execute(
            "SELECT messages FROM background_turns WHERE task_id=?", (task["id"],)
        ).fetchone()[0]
    assert pixels not in checkpoint
    assert "read_browser_draft" in checkpoint
    (review,) = runtime.action_reviews.list(task["bot_id"])
    assert review["action"]["observed_context"]["screenshot"] == pixels


def test_switching_to_manual_during_review_prevents_automatic_dispatch(study):
    from alysis_code.bots.action_review import ApprovalRequired, BotActionReviewer

    runtime, client = runtime_for(study, [])
    store, task, _, _ = study

    def review_response(**kwargs):
        runtime.action_reviews.settings(task["bot_id"], ReviewSettings(mode="review"))
        return review_response("allow", "Requested work")

    client.chat = review_response
    reviewer = BotActionReviewer(runtime, task, client, runtime.task_token(task["id"]))
    with pytest.raises(ApprovalRequired):
        reviewer.check("computer_run", {"command": "touch result.txt"})
    (pending,) = runtime.action_reviews.list(task["bot_id"])
    assert pending["status"] == "pending"


def test_an_approval_refused_before_dispatch_is_kept_for_the_identical_retry(study):
    from alysis_code.bots.action_review import ApprovalRequired, SpentApproval
    from alysis_code.bots.background import BackgroundStore

    store, task, _, _ = study
    reviews = ActionReviews(store, "owner")
    action = {
        "tool": "browser_click",
        "arguments": {"source_id": "old", "node": 5},
        "observed_context": {"url": "https://example.org", "target": {"text": "Learn more"}},
    }
    with pytest.raises(ApprovalRequired):
        reviews.request(task, action, "Review the click")
    reviews.pause(task, [])
    (review,) = reviews.list(task["bot_id"])
    reviews.decide(
        task["bot_id"], review["id"], ApprovalDecision(action="allow", expected_revision=1)
    )
    BackgroundStore(store, "owner").claim()
    retry = {**action, "arguments": {**action["arguments"], "source_id": "new"}}
    spent = reviews.consume(task, retry)
    assert spent
    SpentApproval(reviews, task, spent).release()  # refused before dispatch: nothing was sent
    assert reviews.consume(task, retry)  # the identical retry needs no second approval
    assert not reviews.consume(task, retry)  # once sent, the approval is spent


def test_a_resumed_task_is_told_the_person_approved_its_waiting_action(study):
    from alysis_code.bots.action_review import ApprovalRequired
    from alysis_code.bots.background import BackgroundStore

    store, task, _, _ = study
    reviews = ActionReviews(store, "owner")
    action = {
        "tool": "browser_click",
        "arguments": {"source_id": "old", "node": 5},
        "observed_context": {"url": "https://example.org", "label": "Learn more"},
    }
    with pytest.raises(ApprovalRequired):
        reviews.request(task, action, "Review the click")
    reviews.pause(task, [])
    assert reviews.decision_notes(task) == []  # nothing decided yet
    (review,) = reviews.list(task["bot_id"])
    reviews.decide(
        task["bot_id"], review["id"], ApprovalDecision(action="allow", expected_revision=1)
    )
    BackgroundStore(store, "owner").claim()
    (note,) = reviews.decision_notes(task)
    assert note["role"] == "system" and "approved" in note["content"]
    assert '"node":5' in note["content"].replace(" ", "") and "old" not in note["content"]
    assert reviews.consume(task, {**action, "arguments": {"source_id": "new", "node": 5}})
    assert reviews.decision_notes(task) == []  # used once, no longer announced
