"""Durable native form snapshots; no model or fake success claims."""

import json

import pytest
from test_bots_browser import dom, task
from test_bots_clarifications import setup

from alysis_code.bots.browser_drafts import form_draft
from alysis_code.bots.clarifications import AskQuestion, ClarificationStore, ReadBrowserDraft
from alysis_code.bots.store import NotFound, Store


def test_native_values_are_exact_and_sensitive_controls_are_excluded():
    raw = dom()
    value = "Αύριο / 東京 / " + "λ" * 9000
    raw["strings"][12] = value
    result = form_draft({"data": raw})
    assert result["fields"][0]["value"] == value
    assert len(result["fields"]) == 1  # Non-rendered control was not captured.
    assert "selector" not in json.dumps(result)
    for attr, secret in [
        ("type", "password"),
        ("type", "file"),
        ("autocomplete", "section-checkout cc-number"),
        ("autocomplete", "one-time-code"),
    ]:
        changed = dom()
        changed["strings"] += [attr, secret]
        changed["documents"][0]["nodes"]["attributes"][5] += [
            len(changed["strings"]) - 2,
            len(changed["strings"]) - 1,
        ]
        excluded = form_draft({"data": changed})
        assert excluded["fields"] == [] and not excluded["complete"]
        assert "Visible input" not in json.dumps(excluded)
    assert not form_draft({"data": raw, "truncated": True})["complete"]

    # Chromium uses textValue for TEXTAREA, not inputValue.
    textarea = dom()
    textarea["strings"].append("TEXTAREA")
    textarea["documents"][0]["nodes"]["nodeName"][5] = len(textarea["strings"]) - 1
    textarea["documents"][0]["nodes"]["inputValue"] = {"index": [], "value": []}
    textarea["documents"][0]["nodes"]["textValue"] = {"index": [5], "value": [12]}
    assert form_draft({"data": textarea})["fields"][0]["value"] == "Visible input"


def test_editor_snapshot_preserves_inert_structure_and_excludes_hidden_text():
    raw = dom()
    strings = raw["strings"]

    def s(value):
        strings.append(value)
        return len(strings) - 1

    nodes = raw["documents"][0]["nodes"]
    nodes["nodeName"][6] = s("DIV")
    nodes["attributes"][6] = [s("contenteditable"), s("TRUE"), s("onclick"), s("doSomething()")]
    nodes["nodeValue"][7] = s("Exact 日本語\nΩ")
    for key, value in [
        ("parentIndex", 6),
        ("nodeType", 3),
        ("nodeName", s("#text")),
        ("nodeValue", s("hidden draft")),
        ("backendNodeId", 999),
        ("attributes", []),
    ]:
        nodes[key].append(value)
    result = form_draft({"data": raw})
    editor = next(f for f in result["fields"] if f["type"] == "contenteditable")
    assert editor["content_tree"] == {"tag": "DIV", "children": [{"text": "Exact 日本語\nΩ"}]}
    assert not editor["complete"] and not result["complete"]
    serialized = json.dumps(result)
    assert all(
        value not in serialized
        for value in ("hidden draft", "onclick", "doSomething", "backendNodeId", "selector")
    )
    # A read-only element is not an unsupported editor merely because the
    # attribute is present; it must have an enabled HTML enumerated value.
    nodes["attributes"][6] = [s("contenteditable"), s("false")]
    assert form_draft({"data": raw})["complete"]


@pytest.mark.parametrize("tag", ["CANVAS", "IFRAME"])
def test_non_dom_editor_surfaces_are_explicitly_incomplete(tag):
    raw = dom()
    raw["strings"].append(tag)
    raw["documents"][0]["nodes"]["nodeName"][6] = len(raw["strings"]) - 1
    snapshot = form_draft({"data": raw})
    assert not snapshot["complete"] and snapshot["omitted_controls"] == 1


def test_draft_paging_restart_isolation_and_stopped_source(tmp_path):
    store, bot, chat, request, questions = setup(tmp_path)
    active = store.claim("owner")
    q = questions.ask(active, AskQuestion(question="Which rate?"))
    draft = {
        "pages": [form_draft({"data": dom(text="unused")})],
        "complete": True,
        "note": "日本語" * 2500,
    }
    assert questions.save_browser_draft(active, draft)["saved"]
    store.finish("owner", active["id"], "completed", content="Waiting for the rate.")
    reopened = Store(store.path)
    questions = ClarificationStore(reopened, "owner")
    state = questions.context(request)
    assert state[0]["browser_draft"]["bytes"] > 4000
    offset, chunks = 0, []
    while offset is not None:
        receipt = questions.read_browser_draft(
            request, ReadBrowserDraft(question_id=q["id"], offset=offset)
        )
        assert len(receipt["content"]) <= 4000
        chunks.append(receipt["content"])
        offset = receipt["next_offset"]
    assert json.loads("".join(chunks)) == draft
    references = (
        ReadBrowserDraft(question_id=q["id"]),
        ReadBrowserDraft(source_task_id=active["id"]),
    )
    for reference in references:
        with pytest.raises(NotFound):
            ClarificationStore(reopened, "another-owner").read_browser_draft(request, reference)
    # An unrelated bot cannot use another bot's question reference.
    from alysis_code.bots.models import BotInput

    other = reopened.save_bot("owner", BotInput(name="Other", purpose="Other work"))
    for reference in references:
        with pytest.raises(NotFound):
            questions.read_browser_draft({**request, "bot_id": other["id"]}, reference)
    with reopened.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (active["id"],))
    for reference in references:
        with pytest.raises(NotFound):
            questions.read_browser_draft(request, reference)


def test_failed_refresh_keeps_old_values_and_marks_them_stale(tmp_path):
    store, bot, chat, request, questions = setup(tmp_path)
    active = store.claim("owner")
    q = questions.ask(active, AskQuestion(question="Which rate?"))
    questions.save_browser_draft(active, {"complete": True, "pages": [{"value": "東京"}]})
    questions.mark_browser_draft_stale(active)
    raw = json.loads(
        questions.read_browser_draft(active, ReadBrowserDraft(question_id=q["id"]))["content"]
    )
    assert raw["pages"][0]["value"] == "東京"
    assert raw["refresh_failed"] and not raw["complete"]


def test_checkpoint_requires_current_task_and_does_not_grant_input(lab):
    lab.call()
    assert lab.adapter.checkpoint_draft(task(identifier="other")) is None
    result = lab.adapter.checkpoint_draft(task())
    assert result["pages"][0]["fields"][0]["value"] == "Visible input"
    assert lab.adapter._computers["bot-a"].action_source_id is None


# Reuse the real adapter harness, not another draft implementation.
from test_bots_browser import lab as lab  # noqa: E402


def test_pending_question_follows_accepted_task_then_stop_closes_it(tmp_path):
    from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore

    store, bot, chat, request, questions = setup(tmp_path)
    active = store.claim("owner")
    q = questions.ask(active, AskQuestion(question="Which rate?"))
    proposals = ProposalStore(store, "owner")
    p = proposals.propose(
        active,
        ActionProposal(
            question="Use the site?",
            instructions="Fill the draft and wait for the rate.",
            browser_origin="https://example.org",
        ),
    )
    store.finish(
        "owner", active["id"], "completed", content="Review the permission and answer later."
    )
    decision = proposals.decide(
        bot["id"],
        p["id"],
        ActionDecision(
            action="accept", expected_revision=p["revision"], browser_origin=p["browser_origin"]
        ),
    )
    approved = store.claim("owner")
    assert approved["id"] == decision["task_id"]
    questions.handoff_approved(approved)
    moved = questions.list(bot["id"])[0]
    assert moved["id"] == q["id"] and moved["source_task_id"] == approved["id"]
    assert moved["revision"] == q["revision"] + 1
    questions.handoff_approved(approved)
    assert questions.list(bot["id"])[0]["revision"] == moved["revision"]
    questions.save_browser_draft(approved, {"complete": True, "pages": [{"value": "東京"}]})
    store.cancel("owner", approved["id"])
    assert questions.list(bot["id"])[0]["status"] == "closed"
    with pytest.raises(NotFound):
        questions.read_browser_draft(approved, ReadBrowserDraft(question_id=q["id"]))


def test_postgres_draft_schema_and_roundtrip(databases):
    from uuid import uuid4

    from alysis_code.bots.models import BotInput, TaskInput
    from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore

    store = databases()
    bot = store.save_bot("draft-owner", BotInput(name="Draft", purpose="Preserve form"))
    chat = store.create_chat("draft-owner", bot["id"], "Draft")
    store.enqueue(
        "draft-owner", chat["id"], TaskInput(message="Wait for a rate", request_id=uuid4().hex)
    )
    active = store.claim("draft-owner")
    questions = ClarificationStore(store, "draft-owner")
    q = questions.ask(active, AskQuestion(question="Which rate?"))
    original = {"complete": True, "pages": [{"value": "Αύριο / 東京 / " + "λ" * 6000}]}
    questions.save_browser_draft(active, original)
    proposals = ProposalStore(store, "draft-owner")
    proposal = proposals.propose(
        active,
        ActionProposal(
            question="Use this form?",
            instructions="Preserve the draft while preparing the known fields.",
            browser_origin="https://example.org",
        ),
    )
    store.finish("draft-owner", active["id"], "completed", content="Waiting")
    chunks, offset = [], 0
    while offset is not None:
        result = questions.read_browser_draft(
            active, ReadBrowserDraft(question_id=q["id"], offset=offset)
        )
        chunks.append(result["content"])
        offset = result["next_offset"]
    assert json.loads("".join(chunks)) == original
    proposals.decide(
        bot["id"],
        proposal["id"],
        ActionDecision(
            action="accept",
            expected_revision=proposal["revision"],
            browser_origin=proposal["browser_origin"],
        ),
    )
    approved = store.claim("draft-owner")
    questions.handoff_approved(approved)
    assert questions.context(approved)[0]["browser_draft"]["source_task_id"] == approved["id"]
    assert (
        questions.read_browser_draft(approved, ReadBrowserDraft(question_id=q["id"]))["content"]
        == chunks[0]
    )


def test_answer_after_approval_retains_original_request(tmp_path):
    from alysis_code.bots.clarifications import QuestionDecision
    from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore

    store, bot, chat, request, questions = setup(tmp_path)
    active = store.claim("owner")
    q = questions.ask(active, AskQuestion(question="Which date?"))
    proposals = ProposalStore(store, "owner")
    p = proposals.propose(
        active,
        ActionProposal(
            question="Fill in the known fields?",
            instructions="Fill the name, then wait.",
            browser_origin="https://example.org",
        ),
    )
    store.finish("owner", active["id"], "completed", content="Waiting")
    proposals.decide(
        bot["id"],
        p["id"],
        ActionDecision(
            action="accept",
            expected_revision=p["revision"],
            browser_origin=p["browser_origin"],
        ),
    )
    approved = store.claim("owner")
    questions.handoff_approved(approved)
    store.finish("owner", approved["id"], "completed", content="Name entered. Waiting")
    moved = questions.list(bot["id"])[0]
    answer = questions.decide(
        bot["id"],
        q["id"],
        QuestionDecision(
            action="answer",
            expected_revision=moved["revision"],
            answer="Next Friday",
        ),
    )
    followup = store.task("owner", answer["answer_task_id"])
    context = questions.context(followup)[0]
    assert context["original_request"] == request["message"]
    assert context["original_request"] != approved["message"]
    assert not context["report_only"]
    # A fresh approval after the answer must retain the checkpoint reference and
    # the answer's provenance even if all chat history has been compacted away.
    followup = store.claim("owner")
    p = proposals.propose(
        followup,
        ActionProposal(
            question="Resume the site?",
            instructions="Resume the form using the saved answer.",
            browser_origin="https://example.org",
        ),
    )
    store.finish("owner", followup["id"], "completed", content="Waiting for website permission")
    proposals.decide(
        bot["id"],
        p["id"],
        ActionDecision(
            action="accept",
            expected_revision=p["revision"],
            browser_origin=p["browser_origin"],
        ),
    )
    continuation = store.claim("owner")
    context = questions.context(continuation)[0]
    assert context["id"] == q["id"]
    assert context["answer"] == "Next Friday"
    assert context["original_request"] == request["message"]
    assert not context["report_only"]


from test_bots_backup import databases as databases  # noqa: E402


def test_checkpoint_storage_failure_still_releases_computer(tmp_path, monkeypatch):
    from test_bots_browser_runtime import BrowserFixture

    from alysis_code.bots.object_storage import ObjectStorageError
    from alysis_code.bots.runtime import BotRuntime

    store, bot, chat, request, questions = setup(tmp_path)
    manager = BrowserFixture()
    runtime = BotRuntime(store, "owner", lambda _: None, browser_computers=manager)

    def fail(*args):
        raise OSError("Synthetic database outage")

    monkeypatch.setattr(runtime, "_checkpoint_browser_draft", fail)
    with pytest.raises(ObjectStorageError):
        runtime._release_computers(request["id"], cancelled=False)
    assert manager.releases == [(request["id"], False)]


def test_approval_pause_preserves_draft_before_any_question(tmp_path):
    from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore

    store, bot, chat, request, questions = setup(tmp_path)
    active = store.claim("owner")
    assert not questions.needs_browser_draft(active)
    proposals = ProposalStore(store, "owner")
    p = proposals.propose(
        active,
        ActionProposal(
            question="Use this form?",
            instructions="Preserve its existing draft and fill in my name.",
            browser_origin="https://example.org",
        ),
    )
    assert questions.needs_browser_draft(active)
    draft = {"complete": True, "pages": [{"value": "Original / 日本語"}]}
    questions.save_browser_draft(active, draft)
    store.finish("owner", active["id"], "completed", content="Waiting for approval")
    proposals.decide(
        bot["id"],
        p["id"],
        ActionDecision(
            action="accept",
            expected_revision=p["revision"],
            browser_origin=p["browser_origin"],
        ),
    )
    approved = store.claim("owner")
    questions.handoff_approved(approved)
    assert questions.list(bot["id"]) == []
    reference = questions.browser_draft_reference(approved)
    assert reference["source_task_id"] == approved["id"]
    result = questions.read_browser_draft(
        approved,
        ReadBrowserDraft(
            source_task_id=reference["source_task_id"],
        ),
    )
    assert json.loads(result["content"]) == draft
    q = questions.ask(approved, AskQuestion(question="What rate?"))
    result = questions.read_browser_draft(approved, ReadBrowserDraft(question_id=q["id"]))
    assert json.loads(result["content"]) == draft
    assert questions.context(approved)[0]["browser_draft"] == reference
