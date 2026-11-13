"""Follow-up decisions are durable authority boundaries, not simulated execution."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.store import Conflict, NotFound, Store


@pytest.fixture
def proposal(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Follow through"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue(
        "owner",
        chat["id"],
        TaskInput(message="Review and suggest a next step", request_id=uuid4().hex),
    )
    task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    item = proposals.propose(
        task,
        ActionProposal(
            question="Shall I draft the test plan?",
            instructions="Draft a test plan for the reviewed report, using only the supplied facts.",
        ),
    )
    store.finish(
        "owner", task["id"], "completed", content="Review ready. Shall I draft the test plan?"
    )
    return store, proposals, bot, chat, task, item


def decision(action, revision=1, browser_origin=None):
    return ActionDecision(action=action, expected_revision=revision, browser_origin=browser_origin)


def test_model_proposal_cannot_silently_default_its_permission_scope(tmp_path):
    from pydantic import ValidationError

    from alysis_code.bots.runtime import BotRuntime, tool_schema

    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Documents"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue(
        "owner", chat["id"], TaskInput(message="Suggest a next step", request_id=uuid4().hex)
    )
    task = store.claim("owner")
    runtime = BotRuntime(store, "owner", lambda _: None)
    required = tool_schema("propose_action")["function"]["parameters"]["required"]
    assert "offline_computer" in required
    assert "browser_origin" not in required
    data = {"question": "Prepare a draft?", "instructions": "Draft an outline."}
    with pytest.raises(ValidationError):
        runtime._tool(task, "propose_action", data, ["propose_action"])
    assert not runtime.proposals.list(bot["id"])
    receipt = runtime._tool(
        task,
        "propose_action",
        {**data, "offline_computer": False},
        ["propose_action"],
    )
    assert receipt["offline_computer"] is False and receipt["browser_origin"] is None
    assert receipt["state"] == "pending"
    with pytest.raises(Conflict, match="Website interaction is unavailable"):
        runtime._tool(
            task,
            "propose_action",
            {**data, "offline_computer": False, "browser_origin": "https://example.org"},
            ["propose_action"],
        )
    with pytest.raises(Conflict, match="private computer is unavailable"):
        runtime._tool(
            task, "propose_action", {**data, "offline_computer": True}, ["propose_action"]
        )


def test_unpublished_card_can_be_corrected_but_stale_or_missing_scope_cannot_be_approved(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Documents"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue("owner", chat["id"], TaskInput(message="Make a report", request_id=uuid4().hex))
    task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    first = proposals.propose(
        task, ActionProposal(question="Make it?", instructions="Create the report.")
    )
    revised_data = ActionProposal(
        question="Use the private computer?",
        instructions="Create the report.",
        offline_computer=True,
    )
    revised = proposals.propose(task, revised_data)
    assert revised["id"] == first["id"] and revised["revision"] == 2
    assert len(proposals.list(bot["id"])) == 1
    assert proposals.propose(task, revised_data)["revision"] == 2
    with pytest.raises(Conflict):
        proposals.decide(
            bot["id"],
            first["id"],
            ActionDecision(action="accept", expected_revision=2, offline_computer=True),
        )
    withdrawn = proposals.withdraw(task)
    restored = proposals.propose(task, revised_data)
    assert restored["id"] == first["id"] and restored["revision"] == withdrawn["revision"] + 1 == 4
    assert restored["status"] == "pending" and len(proposals.list(bot["id"])) == 1
    assert proposals.propose(task, revised_data)["revision"] == 4
    assert any(
        e["kind"] == "action_proposal_restored" for e in store.task("owner", task["id"])["events"]
    )
    with pytest.raises(Conflict):
        proposals.decide(
            bot["id"],
            first["id"],
            ActionDecision(action="accept", expected_revision=4, offline_computer=True),
        )
    store.finish("owner", task["id"], "completed", content="Please review the computer permission.")
    for revision, scope in ((1, True), (2, True), (3, True), (4, False)):
        with pytest.raises(Conflict):
            proposals.decide(
                bot["id"],
                first["id"],
                ActionDecision(action="accept", expected_revision=revision, offline_computer=scope),
            )
    with pytest.raises(Conflict):
        proposals.propose(
            task, ActionProposal(question="Change again?", instructions="Different work.")
        )
    accepted = proposals.decide(
        bot["id"],
        first["id"],
        ActionDecision(action="accept", expected_revision=4, offline_computer=True),
    )
    worker = store.claim("owner")
    assert worker["id"] == accepted["task_id"] and proposals.offline_computer_allowed(worker)


@pytest.mark.parametrize("change", ["new_request", "bot_configuration", "published"])
def test_withdrawn_draft_cannot_be_restored_after_its_authority_changes(tmp_path, change):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Documents"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue("owner", chat["id"], TaskInput(message="Make a report", request_id=uuid4().hex))
    task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    data = ActionProposal(question="Make it?", instructions="Create the report.")
    proposals.propose(task, data)
    proposals.withdraw(task)
    if change == "new_request":
        store.enqueue(
            "owner", chat["id"], TaskInput(message="Do something else", request_id=uuid4().hex)
        )
    elif change == "bot_configuration":
        # The public setup API already blocks edits during work. Inject a
        # revision change to test the draft's own stale-configuration fence.
        with store.connect(write=True) as db:
            db.execute("UPDATE bots SET updated_at=updated_at+1 WHERE id=?", (bot["id"],))
    else:
        store.finish("owner", task["id"], "completed", content="No action requested.")
    try:
        unchanged = proposals.propose(task, data)
        assert unchanged["status"] == "outdated"
    except Conflict:
        pass
    with pytest.raises(Conflict):
        proposals.propose(
            task, ActionProposal(question="Try again?", instructions="Different report.")
        )
    assert proposals.for_task(task)["status"] == "outdated"


def test_approval_starts_once_even_after_restart_and_concurrent_retries(proposal):
    store, proposals, bot, chat, task, item = proposal
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: proposals.decide(bot["id"], item["id"], decision("accept")), range(12)
            )
        )
    assert len({r["task_id"] for r in results}) == 1
    queued = store.claim("owner")
    assert queued["message"] == item["instructions"]
    assert queued["request_id"] == "proposal_" + item["id"]
    assert (
        ProposalStore(Store(store.path), "owner").decide(bot["id"], item["id"], decision("accept"))[
            "task_id"
        ]
        == queued["id"]
    )
    assert len(store.conversation("owner", chat["id"])["tasks"]) == 2


def test_approved_source_is_owned_provenance_not_permission(proposal):
    store, proposals, bot, chat, source, item = proposal
    assert proposals.accepted_source(source) is None
    accepted = proposals.decide(bot["id"], item["id"], decision("accept"))
    task = store.task("owner", accepted["task_id"])
    assert ProposalStore(Store(store.path), "owner").accepted_source(task) == {
        "source_task_id": source["id"],
        "original_request": source["message"],
    }
    assert not proposals.offline_computer_allowed(task)
    assert proposals.browser_origins(task) == ()
    assert proposals.accepted_source({**task, "bot_id": "forged"}) is None
    with pytest.raises(NotFound):
        ProposalStore(store, "other-owner").accepted_source(task)
    next_task = store.enqueue(
        "owner", chat["id"], TaskInput(message="Independent question", request_id=uuid4().hex)
    )
    assert proposals.accepted_source(next_task) is None


def test_dismissal_persists_and_does_not_spend_or_repeat(proposal):
    store, proposals, bot, chat, task, item = proposal
    proposals.decide(bot["id"], item["id"], decision("dismiss"))
    assert ProposalStore(Store(store.path), "owner").list(bot["id"])[0]["status"] == "dismissed"
    assert store.claim("owner") is None
    assert store.usage("owner")["calls"] == 0
    store.enqueue("owner", chat["id"], TaskInput(message="Review again", request_id=uuid4().hex))
    current = store.claim("owner")
    with pytest.raises(Conflict, match="dismissed"):
        proposals.propose(
            current, ActionProposal(question="Another phrasing?", instructions=item["instructions"])
        )


@pytest.mark.parametrize("change", ["new_message", "setup", "workspace", "cancelled_source"])
def test_old_scope_cannot_be_approved(proposal, change):
    store, proposals, bot, chat, task, item = proposal
    if change == "new_message":
        store.enqueue(
            "owner",
            chat["id"],
            TaskInput(message="The project scope has changed", request_id=uuid4().hex),
        )
    elif change == "setup":
        store.save_bot("owner", BotInput(name="Mira", purpose="Changed"), bot_id=bot["id"])
    elif change == "workspace":
        from alysis_code.bots.coding import CodingStore, WorkspaceRegistration

        coding = CodingStore(store, "owner")
        workspace = coding.register(
            WorkspaceRegistration(id=uuid4().hex, worker_id=uuid4().hex, name="Changed scope")
        )
        coding.bind(bot["id"], workspace["id"])
    else:
        with store.connect(write=True) as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task["id"],))
    with pytest.raises(Conflict, match="no longer current"):
        proposals.decide(bot["id"], item["id"], decision("accept"))
    assert proposals.list(bot["id"])[0]["status"] == "outdated"
    assert all(
        t["request_id"] != "proposal_" + item["id"]
        for t in store.conversation("owner", chat["id"])["tasks"]
    )


def test_later_changes_only_visibility_and_revision(proposal):
    store, proposals, bot, chat, task, item = proposal
    later = proposals.decide(bot["id"], item["id"], decision("later"))
    assert later["status"] == "pending" and later["revision"] == 2
    assert later["visible_after"] >= item["created_at"] + 3600
    assert store.claim("owner") is None and store.usage("owner")["calls"] == 0
    with pytest.raises(Conflict, match="changed"):
        proposals.decide(bot["id"], item["id"], decision("accept"))
    assert proposals.decide(bot["id"], item["id"], decision("accept", 2))["status"] == "accepted"


def test_cross_bot_and_owner_decisions_are_denied(proposal):
    store, proposals, bot, chat, task, item = proposal
    other = store.save_bot("owner", BotInput(name="Peer", purpose="Separate"))
    with pytest.raises(NotFound):
        proposals.decide(other["id"], item["id"], decision("accept"))
    with pytest.raises(NotFound):
        ProposalStore(store, "other").decide(bot["id"], item["id"], decision("accept"))


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:password@example.org",
        "https://example.org/path",
        "https://example.org?secret=value",
        "https://example.org#fragment",
        "file:///tmp/document",
        "javascript:alert(1)",
        "https://exa\nmple.org",
        "https://example.org/ ",
        "https://*.example.org",
    ],
)
def test_website_approval_rejects_ambiguous_or_non_origin_scope(origin):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ActionProposal(question="Allow?", instructions="Use the page", browser_origin=origin)


def website_proposal(store, proposals, bot, chat):
    store.enqueue("owner", chat["id"], TaskInput(message="Use the website", request_id=uuid4().hex))
    source = store.claim("owner")
    item = proposals.propose(
        source,
        ActionProposal(
            question="Allow this site?",
            instructions="Save the exact supplied note once.",
            browser_origin="https://EXAMPLE.org:443/",
        ),
    )
    assert item["browser_origin"] == "https://example.org"
    assert proposals.browser_origins(source) == ()
    store.finish("owner", source["id"], "completed", content="Please review the site and task.")
    return item


def test_website_grant_requires_acceptance_and_is_bound_to_one_task(proposal):
    store, proposals, bot, chat, source, _ = proposal
    item = website_proposal(store, proposals, bot, chat)
    for value in (None, "https://wrong.example.org"):
        with pytest.raises(Conflict, match="website permission"):
            proposals.decide(bot["id"], item["id"], decision("accept", browser_origin=value))
    accepted = proposals.decide(
        bot["id"], item["id"], decision("accept", browser_origin=item["browser_origin"])
    )
    task = store.task("owner", accepted["task_id"])
    assert proposals.browser_origins(task) == ()  # Queued work has no input authority.
    task = store.claim("owner")
    assert proposals.browser_origins(task) == ("https://example.org",)
    assert ProposalStore(Store(store.path), "owner").browser_origins(task) == (
        "https://example.org",
    )
    assert proposals.browser_origins({**task, "bot_id": "forged"}) == ()
    with pytest.raises(NotFound):
        ProposalStore(store, "other-owner").browser_origins(task)
    assert proposals.browser_origins(source) == ()
    store.finish("owner", task["id"], "completed", content="Actually checked result")
    assert proposals.browser_origins(task) == ()
    store.enqueue("owner", chat["id"], TaskInput(message="Next message", request_id=uuid4().hex))
    next_task = store.claim("owner")
    assert proposals.browser_origins(next_task) == ()


@pytest.mark.parametrize("action", ["dismiss", "later"])
def test_unaccepted_website_grant_never_runs(proposal, action):
    store, proposals, bot, chat, _, _ = proposal
    item = website_proposal(store, proposals, bot, chat)
    result = proposals.decide(bot["id"], item["id"], decision(action))
    assert result["task_id"] is None
    assert store.claim("owner") is None
    assert store.usage("owner")["calls"] == 0


def test_cancelled_task_cannot_reuse_browser_grant(proposal):
    store, proposals, bot, chat, _, _ = proposal
    item = website_proposal(store, proposals, bot, chat)
    proposals.decide(
        bot["id"], item["id"], decision("accept", browser_origin=item["browser_origin"])
    )
    task = store.claim("owner")
    assert proposals.browser_origins(task)
    store.cancel("owner", task["id"])
    assert proposals.browser_origins(task) == ()


def test_v9_proposals_migrate_without_inventing_browser_permission(proposal):
    store, _, bot, _, _, item = proposal
    with store.connect(write=True) as db:
        db.execute("ALTER TABLE action_proposals DROP COLUMN browser_origin")
        db.execute("PRAGMA user_version=9")
    migrated = Store(store.path)
    old = ProposalStore(migrated, "owner").list(bot["id"])[0]
    assert old["id"] == item["id"] and old["instructions"] == item["instructions"]
    assert old["browser_origin"] is None
    assert Store(store.path)  # Repeated startup does not duplicate the column.


def test_v10_site_grant_survives_peer_schema_upgrade(proposal):
    store, proposals, bot, chat, _, _ = proposal
    item = website_proposal(store, proposals, bot, chat)
    accepted = proposals.decide(
        bot["id"], item["id"], decision("accept", browser_origin=item["browser_origin"])
    )
    with store.connect(write=True) as db:
        db.execute("DROP TABLE peer_calls")
        db.execute("DROP TABLE peer_exchanges")
        db.execute("PRAGMA user_version=10")
    migrated = Store(store.path)
    task = migrated.claim("owner")
    assert task["id"] == accepted["task_id"]
    assert ProposalStore(migrated, "owner").browser_origins(task) == ("https://example.org",)
    with migrated.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT count(*) FROM peer_exchanges").fetchone()[0] == 0


def test_peer_schema_without_site_column_does_not_invent_permissions(proposal):
    store, _, bot, _, _, item = proposal
    with store.connect(write=True) as db:
        db.execute("ALTER TABLE action_proposals DROP COLUMN browser_origin")
        db.execute("PRAGMA user_version=11")
    migrated = Store(store.path)
    proposal = ProposalStore(migrated, "owner").list(bot["id"])[0]
    assert proposal["id"] == item["id"] and proposal["browser_origin"] is None


def test_offline_computer_grant_requires_visible_acceptance_and_expires(proposal):
    store, proposals, bot, chat, _, _ = proposal
    store.enqueue(
        "owner", chat["id"], TaskInput(message="Use the private computer", request_id=uuid4().hex)
    )
    source = store.claim("owner")
    item = proposals.propose(
        source,
        ActionProposal(
            question="Use the offline computer?",
            instructions="Prepare the requested local file.",
            offline_computer=True,
        ),
    )
    store.finish("owner", source["id"], "completed", content="Please review computer access.")
    with pytest.raises(Conflict, match="offline computer"):
        proposals.decide(bot["id"], item["id"], decision("accept"))
    accepted = proposals.decide(
        bot["id"],
        item["id"],
        ActionDecision(action="accept", expected_revision=1, offline_computer=True),
    )
    queued = store.task("owner", accepted["task_id"])
    assert not proposals.offline_computer_allowed(queued)
    task = store.claim("owner")
    assert proposals.offline_computer_allowed(task)
    assert proposals.browser_origins(task) == ()
    assert ProposalStore(Store(store.path), "owner").offline_computer_allowed(task)
    assert not proposals.offline_computer_allowed({**task, "bot_id": "other"})
    store.cancel("owner", task["id"])
    assert not proposals.offline_computer_allowed(task)


def test_old_proposal_rows_gain_no_offline_permission(proposal):
    store, _, bot, _, _, item = proposal
    with store.connect(write=True) as db:
        db.execute("ALTER TABLE action_proposals DROP COLUMN offline_computer")
        db.execute("PRAGMA user_version=11")
    migrated = Store(store.path)
    current = ProposalStore(migrated, "owner").list(bot["id"])[0]
    assert current["id"] == item["id"] and current["offline_computer"] == 0


def test_withdraw_only_current_unpublished_proposal_and_reject_stale_acceptance(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Follow through"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue("owner", chat["id"], TaskInput(message="Review", request_id=uuid4().hex))
    task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    item = proposals.propose(
        task, ActionProposal(question="Repeat?", instructions="An unnecessary follow-up.")
    )
    with pytest.raises(NotFound):
        ProposalStore(store, "other").withdraw(task)
    receipt = proposals.withdraw(task)
    assert receipt["state"] == "outdated" and receipt["revision"] == item["revision"] + 1
    assert proposals.withdraw(task) == receipt  # Retry does not advance the revision again.
    assert proposals.for_task(task)["instructions"] == item["instructions"]
    store.finish("owner", task["id"], "completed", content="The requested work is done.")
    for revision in (item["revision"], receipt["revision"]):
        with pytest.raises(Conflict):
            proposals.decide(bot["id"], item["id"], decision("accept", revision))
    with pytest.raises(Conflict, match="no longer"):
        proposals.withdraw(task)
    assert store.claim("owner") is None


def test_current_turn_cannot_withdraw_another_turns_accepted_proposal(proposal):
    store, proposals, bot, chat, source, item = proposal
    accepted = proposals.decide(bot["id"], item["id"], decision("accept"))
    followup = store.claim("owner")
    assert followup["id"] == accepted["task_id"]
    assert proposals.withdraw(followup)["state"] == "none"
    assert proposals.for_task(source)["status"] == "accepted"
    assert store.task("owner", followup["id"])["status"] == "running"


def test_a_permission_the_task_already_holds_is_not_requested_again(proposal):
    store, proposals, bot, chat, _, _ = proposal
    item = website_proposal(store, proposals, bot, chat)
    accepted = proposals.decide(
        bot["id"], item["id"], decision("accept", browser_origin=item["browser_origin"])
    )
    task = store.claim("owner")
    assert task["id"] == accepted["task_id"]

    def ask(**scope):
        return ActionProposal(question="May I?", instructions="Do the approved work.", **scope)

    assert proposals.already_granted(task, ask(browser_origin="https://example.org"))
    assert not proposals.already_granted(task, ask(browser_origin="https://other.example.org"))
    assert not proposals.already_granted(
        task, ask(browser_origin="https://example.org", offline_computer=True)
    )
    assert not proposals.already_granted(task, ask())  # a plain suggestion still makes a card
    store.finish("owner", task["id"], "completed", content="Done")
    assert not proposals.already_granted(task, ask(browser_origin="https://example.org"))


COMPUTER_WORK = ActionProposal(
    question="Shall I build the spreadsheet?",
    instructions="Build the spreadsheet from the supplied numbers.",
    offline_computer=True,
)


def running_request(tmp_path, message="Make a spreadsheet from these numbers: 1, 2, 3"):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Documents"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue("owner", chat["id"], TaskInput(message=message, request_id=uuid4().hex))
    return store, bot, store.claim("owner")


def test_the_persons_request_approves_the_private_computer_without_a_card(tmp_path):
    store, bot, task = running_request(tmp_path)
    proposals = ProposalStore(store, "owner")
    assert not proposals.offline_computer_allowed(task)
    grant = proposals.approve_by_request(task, COMPUTER_WORK)
    assert grant["status"] == "accepted" and grant["task_id"] == task["id"]
    assert grant["offline_computer"] and grant["browser_origin"] is None
    assert proposals.offline_computer_allowed(task)
    assert proposals.already_granted(task, COMPUTER_WORK)
    assert proposals.browser_origins(task) == ()  # no website input comes with it
    assert not [p for p in proposals.list(bot["id"]) if p["status"] == "pending"]
    kinds = [e["kind"] for e in store.task("owner", task["id"])["events"]]
    assert "action_approved_by_request" in kinds
    store.finish("owner", task["id"], "completed", content="Done.")
    assert not proposals.offline_computer_allowed(task)  # it ends with the task


def test_this_turns_draft_card_becomes_the_grant(tmp_path):
    store, bot, task = running_request(tmp_path)
    proposals = ProposalStore(store, "owner")
    draft = proposals.propose(task, ActionProposal(question="Make it?", instructions="Create it."))
    grant = proposals.approve_by_request(task, COMPUTER_WORK)
    assert grant["id"] == draft["id"] and grant["revision"] == draft["revision"] + 1
    assert grant["status"] == "accepted" and grant["instructions"] == COMPUTER_WORK.instructions
    assert len(proposals.list(bot["id"])) == 1


def test_website_input_routine_runs_and_finished_tasks_still_need_a_card(tmp_path):
    store, bot, task = running_request(tmp_path)
    proposals = ProposalStore(store, "owner")
    with pytest.raises(ValueError):
        proposals.approve_by_request(
            task,
            ActionProposal(
                question="Fill it in?",
                instructions="Fill in the form.",
                offline_computer=True,
                browser_origin="https://example.org",
            ),
        )
    with pytest.raises(ValueError):
        proposals.approve_by_request(task, ActionProposal(question="Draft?", instructions="Draft."))
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO schedules(id,bot_id,name,prompt,timing,state,revision,next_run_at,"
            "pause_reason,source_task_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s" * 32, bot["id"], "Daily", "Check", "{}", "active", 1, None, None, None, 0, 0),
        )
        db.execute(
            "INSERT INTO schedule_runs(schedule_id,schedule_revision,scheduled_for,task_id,status,"
            "reason,config,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("s" * 32, 1, 0, task["id"], "queued", None, "{}", 0),
        )
    with pytest.raises(Conflict):
        proposals.approve_by_request(task, COMPUTER_WORK)
    assert not proposals.offline_computer_allowed(task)
    (tmp_path / "other").mkdir()
    other_store, _, finished = running_request(tmp_path / "other")
    other_store.finish("owner", finished["id"], "completed", content="Done.")
    with pytest.raises(Conflict):
        ProposalStore(other_store, "owner").approve_by_request(finished, COMPUTER_WORK)


def test_the_request_still_approves_its_computer_while_it_continues_in_the_background(tmp_path):
    from alysis_code.bots.background import BackgroundStore

    store, bot, task = running_request(tmp_path)
    background = BackgroundStore(store, "owner")
    proposals = ProposalStore(store, "owner")
    background.submit(task, "Research, then build the spreadsheet.", [])
    queued = store.task("owner", task["id"])
    # Queued work is not executing yet: nothing can be approved on its behalf.
    with pytest.raises(Conflict):
        proposals.approve_by_request(queued, COMPUTER_WORK)
    running = background.claim()
    assert running["id"] == task["id"] and running["status"] == "waiting"
    grant = proposals.approve_by_request(running, COMPUTER_WORK)
    assert grant["status"] == "accepted" and grant["task_id"] == task["id"]
    assert proposals.offline_computer_allowed(running)
    # Routine changes keep their stricter rule: only a reply turn in the chat makes them.
    with store.connect() as db, pytest.raises(Conflict):
        store.direct_request(db, "owner", bot["id"], task["id"])
    # The same turn restarts from its checkpoint, with the computer tools this time.
    background.resume(running, COMPUTER_WORK.instructions, [{"role": "user", "content": "x"}])
    again = background.claim()
    assert again["id"] == task["id"]
    assert background.checkpoint(task["id"]) == [{"role": "user", "content": "x"}]
    store.finish("owner", task["id"], "completed", content="Done.")
    with pytest.raises(Conflict):
        background.resume(again, "x", [])


def test_propose_action_says_plainly_when_there_is_no_private_computer(tmp_path):
    from alysis_code.bots.runtime import BotRuntime

    store, bot, task = running_request(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    data = {
        "question": "Build it?",
        "instructions": "Build the spreadsheet.",
        "offline_computer": True,
    }
    with pytest.raises(Conflict) as refusal:
        runtime._tool(task, "propose_action", data, ["propose_action"])
    assert "Do not ask for approval" in str(refusal.value)
    assert "Settings > Owls" in str(refusal.value)
    assert not runtime.proposals.list(bot["id"])
    runtime.desktop_computers = object()
    receipt = runtime._tool(task, "propose_action", data, ["propose_action"])
    assert receipt["state"] == "approved_by_request" and receipt["offline_computer"] is True
    assert receipt["summary"] == "Build the spreadsheet."
    assert runtime._tool(task, "propose_action", data, ["propose_action"])["state"] == (
        "already_approved"
    )


def test_the_request_continues_on_the_computer_in_the_same_task(tmp_path):
    from types import SimpleNamespace

    from test_bots_reply_review import response, study

    from alysis_code.llm.types import ToolCall

    work = {
        "question": "Build it?",
        "instructions": "Build the spreadsheet from the numbers.",
        "offline_computer": True,
    }
    store, task, model, runtime, token = study(
        tmp_path,
        [response(calls=[ToolCall("p", "propose_action", work)])],
        message="Make a spreadsheet from these numbers: 1, 2, 3",
    )
    runtime.desktop_computers = SimpleNamespace(schemas=lambda *a, **k: [])
    handed = []
    runtime.background.submit = lambda task, summary, messages: handed.append(
        (task["id"], summary, messages)
    )
    runtime.run(task, token)
    assert len(handed) == 1 and handed[0][0] == task["id"]
    assert handed[0][1] == work["instructions"]
    receipt = handed[0][2][-1]
    assert receipt["role"] == "tool" and "approved_by_request" in receipt["content"]
    assert len(model.requests) == 1  # no card, no reply turn before the work
    assert not [p for p in runtime.proposals.list(task["bot_id"]) if p["status"] == "pending"]
