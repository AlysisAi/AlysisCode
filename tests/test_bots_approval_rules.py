"""Scripted policy, durable rule and cancellation regressions; no live model."""

import json

import pytest
from test_bots_action_review import review_response, runtime_for
from test_bots_desktop_runtime import study as execution_study

from alysis_code.bots.action_review import (
    ActionReviews,
    ApprovalDecision,
    ApprovalRequired,
    BotActionReviewer,
    RuleInput,
    RuleRevision,
    RuleUpdate,
    action_identity,
)
from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.store import Conflict, NotFound, Store
from alysis_code.cancellation import CooperativeCancellationError

study = execution_study


def reviewer_for(study, answers=()):
    runtime, client = runtime_for(study, list(answers))
    reviewer = BotActionReviewer(runtime, study[1], client, runtime.task_token(study[1]["id"]))
    return runtime, client, reviewer


@pytest.mark.parametrize(
    "command", ["ls -la", "cat report.txt | wc -l", "git status --short", "printf hello"]
)
def test_established_read_only_commands_skip_model_review(study, command):
    runtime, client, reviewer = reviewer_for(study)
    reviewer.check("computer_run", {"command": command})
    assert client.requests == []
    assert runtime.action_reviews.usage(study[1]["bot_id"])["actions"]["calls"] == 0


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("computer_run", {"command": "ls && printf changed > report.txt"}),
        ("computer_run", {"command": "python worker.py"}),
        ("computer_run", {"command": "cat ~/.ssh/id_rsa"}),
        ("browser_click", {"source_id": "page", "node": 4}),
        ("computer_click", {"source_id": "screen", "x": 2, "y": 3}),
    ],
)
def test_uncertain_or_side_effecting_actions_still_reviewed(study, tool, arguments):
    _, client, reviewer = reviewer_for(study, [review_response("ask_person")])
    with pytest.raises(ApprovalRequired):
        reviewer.check(tool, arguments)
    assert len(client.requests) == 1


PRIVATE = {"environment": "private offline Linux computer", "network": "disabled"}


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("computer_run", {"command": "python - <<'PY'\nopen('deck.py','w').write('x')\nPY"}),
        ("computer_run", {"command": "rm -rf build && python make_deck.py"}),
        ("computer_launch", {"command": "libreoffice --calc budget.xlsx"}),
        ("computer_click", {"source_id": "screen", "x": 2, "y": 3}),
    ],
)
def test_private_computer_work_never_waits_for_the_person_in_automatic_mode(study, tool, arguments):
    runtime, client, reviewer = reviewer_for(study)
    reviewer.check(tool, arguments, PRIVATE, private_computer=True)
    assert client.requests == []  # no reviewer call, no card
    assert not runtime.action_reviews.list(study[1]["bot_id"])
    # Nothing was reviewed, so the activity shows the action itself, not a review line.
    assert not [
        e
        for e in runtime.store.task("owner", study[1]["id"])["events"]
        if e["kind"] == "action_review"
    ]


def test_private_computer_command_runs_without_a_review_call(study):
    from test_bots_browser_runtime import response
    from test_bots_desktop_runtime import run_task

    runtime, client = runtime_for(
        study,
        [
            response(name="computer_run", arguments={"command": "printf hello > result.txt"}),
            response("Done"),
        ],
    )
    store, task, _, instances = study
    run_task(runtime, task)
    result = store.task("owner", task["id"])
    assert result["status"] == "completed", result["error"]
    assert [c[0] for c in instances[0].calls] == ["run"]
    assert not any(e["kind"] == "action_review_call" for e in result["events"])
    assert len(client.requests) == 2  # the work and its report; no reviewer call


def test_an_ask_first_rule_still_reviews_private_computer_work(study):
    # An "ask first" rule is the person's own choice, so it keeps the reviewer in the loop.
    runtime, client, reviewer = reviewer_for(study, [review_response("ask_person")])
    runtime.action_reviews.save_rule(
        study[1]["bot_id"], RuleInput(effect="ask", description="Ask before deleting files")
    )
    with pytest.raises(ApprovalRequired):
        reviewer.check("computer_run", {"command": "rm -rf build"}, PRIVATE, private_computer=True)
    assert len(client.requests) == 1


def test_reviewing_every_action_still_covers_the_private_computer(study):
    from alysis_code.bots.action_review import ReviewSettings

    runtime, client, reviewer = reviewer_for(study)
    runtime.action_reviews.settings(study[1]["bot_id"], ReviewSettings(mode="review"))
    with pytest.raises(ApprovalRequired):
        reviewer.check(
            "computer_run", {"command": "touch report.txt"}, PRIVATE, private_computer=True
        )
    assert client.requests == []  # the person reviews it directly; no model call


def save_exact(study, action):
    store, task, _, _ = study
    reviews = ActionReviews(store, "owner")
    with pytest.raises(ApprovalRequired):
        reviews.request(task, action, "Confirm this action")
    reviews.pause(task, [])
    (pending,) = reviews.list(task["bot_id"])
    decision = ApprovalDecision(action="always_allow", expected_revision=pending["revision"])
    reviews.decide(task["bot_id"], pending["id"], decision)
    reviews.decide(task["bot_id"], pending["id"], decision)
    assert len(reviews.rules(task["bot_id"])) == 1
    BackgroundStore(store, "owner").claim()
    return reviews, pending, decision


def test_exact_grant_restart_changed_action_and_revocation(study):
    store, task, manager, instances = study
    action = {
        "tool": "computer_run",
        "arguments": {"command": "touch report.txt"},
        "observed_context": {},
    }
    reviews, pending, decision = save_exact(study, action)
    # Re-open the real database, not just the adapter's in-memory state.
    restarted = Store(store.path)
    runtime, client, reviewer = reviewer_for((restarted, task, manager, instances))
    reviewer.check(action["tool"], action["arguments"])  # one-use approval
    reviewer.check(action["tool"], action["arguments"])  # durable rule
    assert not client.requests
    (rule,) = runtime.action_reviews.rules(task["bot_id"])
    assert rule["action"] == action
    runtime.action_reviews.remove_rule(
        task["bot_id"], rule["id"], RuleRevision(expected_revision=1)
    )
    reviews.decide(task["bot_id"], pending["id"], decision)  # retry after lost response
    assert runtime.action_reviews.rules(task["bot_id"]) == []  # never resurrects
    client.responses = iter([review_response("ask_person")])
    with pytest.raises(ApprovalRequired):
        reviewer.check(action["tool"], action["arguments"])
    assert len(client.requests) == 1


@pytest.mark.parametrize("change", ["arguments", "page", "screenshot"])
def test_exact_rules_do_not_expand_to_changed_action_or_observation(study, change):
    action = {
        "tool": "browser_click",
        "arguments": {"source_id": "old", "node": 5},
        "observed_context": {
            "url": "https://example.org",
            "page": {"amount": "10"},
            "screenshot": "synthetic-pixels",
        },
    }
    reviews, _, _ = save_exact(study, action)
    (rule,) = reviews.rules(study[1]["bot_id"])
    assert "synthetic-pixels" not in json.dumps(rule)
    changed = json.loads(json.dumps(action))
    changed["arguments"]["source_id"] = "fresh"
    assert action_identity(changed) == rule["action"]
    if change == "arguments":
        changed["arguments"]["node"] = 6
    elif change == "page":
        changed["observed_context"]["page"]["amount"] = "1000"
    else:
        changed["observed_context"]["screenshot"] = "changed-pixels"
    _, client, reviewer = reviewer_for(study, [review_response("ask_person")])
    with pytest.raises(ApprovalRequired):
        reviewer.check(changed["tool"], changed["arguments"], changed["observed_context"])
    assert len(client.requests) == 1


def test_custom_rule_revision_ownership_edit_and_restart(study):
    store, task, _, _ = study
    reviews = ActionReviews(store, "owner")
    rule = reviews.save_rule(
        task["bot_id"], RuleInput(effect="ask", description=" Πριν σβήσεις αρχεία ")
    )
    with pytest.raises(NotFound):
        ActionReviews(store, "other").rules(task["bot_id"])
    with pytest.raises(Conflict):
        reviews.remove_rule(task["bot_id"], rule["id"], RuleRevision(expected_revision=2))
    updated = reviews.save_rule(
        task["bot_id"],
        RuleUpdate(effect="allow", description="临时工作区中的报告", expected_revision=1),
        rule["id"],
    )
    assert updated["revision"] == 2 and updated["action"] is None
    assert ActionReviews(Store(store.path), "owner").rules(task["bot_id"]) == [updated]
    with pytest.raises(Conflict):
        reviews.save_rule(
            task["bot_id"],
            RuleUpdate(effect="ask", description="new", expected_revision=1),
            rule["id"],
        )
    with pytest.raises(ValueError):
        RuleInput(effect="allow", description="  ")


@pytest.mark.parametrize("command", ["cat report.txt", "touch report.txt"])
def test_ask_first_wins_even_when_model_returns_allow(study, command):
    runtime, client, reviewer = reviewer_for(study)
    rules = [
        runtime.action_reviews.save_rule(
            study[1]["bot_id"], RuleInput(effect=effect, description=description)
        )
        for effect, description in [
            ("allow", "Work with report.txt"),
            ("ask", "Before accessing report.txt"),
        ]
    ]
    client.responses = iter(
        [review_response("allow", rule_matches={r["id"]: "match" for r in rules})]
    )
    with pytest.raises(ApprovalRequired):
        reviewer.check("computer_run", {"command": command})
    payload = json.JSONDecoder().raw_decode(client.requests[0]["messages"][1]["content"])[0]
    assert {r["description"] for r in payload["approval_rules"]} == {
        r["description"] for r in rules
    }
    assert len(client.requests) == 1


@pytest.mark.parametrize("change", ["edit", "remove", "stop", "correction"])
def test_policy_or_instructions_changed_during_review_cannot_dispatch(study, change):
    runtime, client, reviewer = reviewer_for(study)
    store, task, _, _ = study
    rule = runtime.action_reviews.save_rule(
        task["bot_id"], RuleInput(effect="allow", description="Write the report")
    )

    def answer(**kwargs):
        if change == "edit":
            runtime.action_reviews.save_rule(
                task["bot_id"],
                RuleUpdate(effect="ask", description="Write the report", expected_revision=1),
                rule["id"],
            )
        elif change == "remove":
            runtime.action_reviews.remove_rule(
                task["bot_id"], rule["id"], RuleRevision(expected_revision=1)
            )
        elif change == "stop":
            store.cancel("owner", task["id"])
        else:
            from alysis_code.bots.models import TaskInput

            store.enqueue(
                "owner",
                task["chat_id"],
                TaskInput(message="Do not write the report", request_id="correction"),
            )
        return review_response("allow", rule_matches={rule["id"]: "match"})

    client.chat = answer
    with pytest.raises((ApprovalRequired, Conflict, CooperativeCancellationError)):
        reviewer.check("computer_run", {"command": "touch report.txt"})


def test_new_ask_rule_overrides_saved_exact_rule(study):
    action = {
        "tool": "computer_run",
        "arguments": {"command": "touch report.txt"},
        "observed_context": {},
    }
    reviews, _, _ = save_exact(study, action)
    reviews.save_rule(
        study[1]["bot_id"], RuleInput(effect="ask", description="Before writing report.txt")
    )
    rules = reviews.rules(study[1]["bot_id"])
    _, client, reviewer = reviewer_for(
        study,
        [review_response("allow", rule_matches={r["id"]: "match" for r in rules})],
    )
    with pytest.raises(ApprovalRequired):
        reviewer.check(action["tool"], action["arguments"])
    assert len(client.requests) == 1


def test_schema_28_pending_approval_remains_usable_after_upgrade(study):
    store, task, _, _ = study
    reviews = ActionReviews(store, "owner")
    action = {"tool": "computer_run", "arguments": {"command": "touch report.txt"}}
    with pytest.raises(ApprovalRequired):
        reviews.request(task, action, "Confirm")
    reviews.pause(task, [])
    (pending,) = reviews.list(task["bot_id"])
    assert len(json.loads(pending["generation"])) == 3
    # Reconstruct the previous schema in this disposable fixture only.
    with store.connect(write=True) as db:
        db.execute("DROP TABLE bot_action_rules")
        db.execute("PRAGMA user_version=28")
    upgraded = Store(store.path)
    current = ActionReviews(upgraded, "owner")
    assert current.rules(task["bot_id"]) == []
    current.decide(
        task["bot_id"], pending["id"], ApprovalDecision(action="allow", expected_revision=1)
    )
    BackgroundStore(upgraded, "owner").claim()
    assert current.consume(task, action)


def test_rules_changed_while_paused_block_old_allow_but_never_block_deny_and_stop(study):
    store, task, _, _ = study
    reviews = ActionReviews(store, "owner")
    with pytest.raises(ApprovalRequired):
        reviews.request(
            task, {"tool": "computer_run", "arguments": {"command": "touch report.txt"}}, "Confirm"
        )
    reviews.pause(task, [])
    (pending,) = reviews.list(task["bot_id"])
    reviews.save_rule(
        task["bot_id"], RuleInput(effect="ask", description="Before changing reports")
    )
    with pytest.raises(Conflict):
        reviews.decide(
            task["bot_id"],
            pending["id"],
            ApprovalDecision(action="always_allow", expected_revision=1),
        )
    reviews.decide(
        task["bot_id"], pending["id"], ApprovalDecision(action="deny", expected_revision=1)
    )
    assert store.task("owner", task["id"])["status"] == "cancelled"
    assert BackgroundStore(store, "owner").claim() is None


def test_always_allow_also_holds_when_the_person_reviews_every_action(study):
    from alysis_code.bots.action_review import ReviewSettings

    store, task, _, _ = study
    action = {
        "tool": "computer_run",
        "arguments": {"command": "touch report.txt"},
        "observed_context": {},
    }
    reviews, _, _ = save_exact(study, action)
    reviews.settings(task["bot_id"], ReviewSettings(mode="review"))
    runtime, client, reviewer = reviewer_for(study)
    reviewer.check(action["tool"], action["arguments"])  # the one-time approval
    reviewer.check(action["tool"], action["arguments"])  # the saved exact rule, no new card
    assert not client.requests
    with pytest.raises(ApprovalRequired):  # anything else still asks
        reviewer.check("computer_run", {"command": "touch other.txt"})
    reviews.save_rule(
        task["bot_id"], RuleInput(effect="ask", description="Ask before files change")
    )
    with pytest.raises(ApprovalRequired):  # an ask-first rule wins over the saved action
        reviewer.check(action["tool"], action["arguments"])
