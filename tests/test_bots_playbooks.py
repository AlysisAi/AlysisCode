"""Playbooks an owl keeps for jobs that come again (playbooks.py).

After a run that worked and whose reply passed the reply checker, the owl may keep a short
procedure written from that run; nothing is kept from a failed, stopped or unchecked run. Later
turns and routines see an index and read one with a tool; a better run saves a new version, and
Undo brings the one before back. Playbooks travel with a moved owl, go with a deleted one, and
have their own routes. Real store and runtime; scripted model and reviewer; Greek and Chinese.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.clarifications import AskQuestion, ClarificationStore
from alysis_code.bots.models import BotInput, ReplyReviewSettings, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.owl_move import (
    MAX_COUNTS,
    InvalidMove,
    MovePage,
    OwlMoves,
)
from alysis_code.bots.owl_move import fingerprint as owl_fingerprint
from alysis_code.bots.owl_tools import ROOM_TURN_LEFT_OUT
from alysis_code.bots.owl_toolset import ACTIVATION_REVIEW_TOOLS, EVERYDAY_TOOLS, Turn, tool_names
from alysis_code.bots.playbooks import (
    MAX_EARLIER_VERSIONS,
    MAX_PLAYBOOKS,
    PlaybookInput,
    Playbooks,
    PlaybookUndo,
)
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.store import Conflict, NotFound, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

OWNER = "alice"  # as in test_bots_owl_move, whose move helpers these tests share

GREEK = {
    "name": "Μηνιαία αναφορά πωλήσεων",
    "when_to_use": "Όταν ζητά τη μηνιαία αναφορά πωλήσεων από το αρχείο της βιβλιοθήκης.",
    "steps": [
        "Διάβασε το πιο πρόσφατο αρχείο πωλήσεων της βιβλιοθήκης.",
        "Άθροισε τις πωλήσεις ανά κατηγορία.",
        "Αποθήκευσε την αναφορά ως νέο αρχείο.",
    ],
    "pitfalls": ["Οι στήλες με κόμμα ως υποδιαστολή διαβάζονται ως κείμενο."],
    "done_check": "Το άθροισμα των κατηγοριών ισούται με το σύνολο του αρχείου.",
}
CHINESE = {
    "name": "每周销售报告",
    "when_to_use": "当用户要求根据资料库里的销售文件写每周报告时。",
    "steps": ["读取最新的销售文件。", "按地区汇总销售额。", "把报告保存为新文件。"],
    "pitfalls": ["日期列可能使用不同的格式。"],
    "done_check": "各地区合计等于文件中的总额。",
}


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("ALYSIS_BOTS_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set disposable PostgreSQL 18 DSN for shared playbook checks")
        shared = PostgresStore(
            dsn,
            objects=LocalObjectStorage(tmp_path / "objects"),
            state_directory=tmp_path / "state",
        )
        yield shared
        shared.close()
    else:
        yield Store(tmp_path / "bots.db")


def owl(store, **setup):
    bot = store.save_bot(OWNER, BotInput(name="Γλαύκα", purpose="Βοηθός γραφείου", **setup))
    return bot, store.create_chat(OWNER, bot["id"], "Συνομιλία")


def ask(store, chat, text="Φτιάξε τη μηνιαία αναφορά."):
    task = store.enqueue(OWNER, chat["id"], TaskInput(message=text, request_id=uuid4().hex))
    assert store.claim(OWNER)["id"] == task["id"]
    return store.task(OWNER, task["id"])


def worked(store, task, *names):
    """The run's own receipts: tools it completed."""
    for name in names or ("read_file",):
        store.event(OWNER, task["id"], "tool_completed", {"name": name})


def hold(store, task, playbook=GREEK, **changes):
    return Playbooks(store, OWNER).hold(task, PlaybookInput(**{**playbook, **changes}))


def kept(store, chat, playbook=GREEK, **changes):
    """A run that worked, wrote a playbook and ended with a checked reply."""
    task = ask(store, chat)
    worked(store, task, "read_file", "save_file")
    held = hold(store, task, playbook, **changes)
    store.finish(OWNER, task["id"], "completed", content="Έγινε.", checked=True)
    return task, held


def current(store, bot):
    return Playbooks(store, OWNER).list(bot["id"])


def under_reply(store, chat, task_id):
    tasks = store.conversation(OWNER, chat["id"])["tasks"]
    return next(task for task in tasks if task["id"] == task_id).get("playbook")


# ---------------------------------------------------------------------------------------------
# Kept only from a run that worked, with a checked reply


def test_a_checked_success_keeps_the_playbook_and_shows_it_under_the_reply(store):
    bot, chat = owl(store)
    task = ask(store, chat)
    worked(store, task, "read_file", "save_playbook", "save_file", "read_file")
    held = hold(store, task)
    assert held["state"] == "held" and "kept only when this run finishes" in held["message"]
    # The tools come from the run's receipts, in the order it first used them.
    assert held["tools"] == ["read_file", "save_file"]
    # While the run works, nothing is kept or shown.
    assert current(store, bot) == [] and under_reply(store, chat, task["id"]) is None
    store.finish(OWNER, task["id"], "completed", content="Έτοιμη η αναφορά.", checked=True)
    [listed] = current(store, bot)
    assert (listed["id"], listed["name"], listed["version"]) == (held["id"], GREEK["name"], 1)
    assert listed["earlier_versions"] == 0 and listed["source_task_id"] == task["id"]
    assert under_reply(store, chat, task["id"]) == {
        "id": held["id"],
        "name": GREEK["name"],
        "version": 1,
        "current": True,
    }
    events = store.task(OWNER, task["id"])["events"]
    assert [e["kind"] for e in events][-2:] == ["playbook_kept", "completed"]


@pytest.mark.parametrize(
    "ending",
    [
        pytest.param(
            lambda s, t: s.finish(OWNER, t["id"], "completed", content="Ok"), id="unchecked"
        ),
        pytest.param(
            lambda s, t: s.finish(OWNER, t["id"], "failed", error="Δεν βγήκε."), id="failed"
        ),
        pytest.param(lambda s, t: s.finish(OWNER, t["id"], "cancelled"), id="cancelled"),
        # A restart interrupts a run that acted (one that only read starts again instead).
        pytest.param(
            lambda s, t: (
                s.event(OWNER, t["id"], "tool_completed", {"name": "browser_click"}),
                s.recover(OWNER),
            ),
            id="interrupted-by-restart",
        ),
        pytest.param(
            lambda s, t: s.finish(OWNER, t["id"], "completed", publish_reply=False, checked=True),
            id="no-reply",
        ),
    ],
)
def test_a_run_that_did_not_succeed_with_a_checked_reply_keeps_nothing(store, ending):
    bot, chat = owl(store)
    task = ask(store, chat)
    worked(store, task)
    hold(store, task)
    ending(store, task)
    assert current(store, bot) == []
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM playbooks").fetchone()[0] == 0
    assert under_reply(store, chat, task["id"]) is None
    dropped = [e["data"] for e in store.task(OWNER, task["id"])["events"]]
    assert {"reason": "run_not_checked"} in dropped


def test_a_run_a_restart_starts_again_keeps_its_playbook_for_its_checked_reply(store):
    """Cut off with nothing done outside its records, the same run carries on: what it wrote
    waits for its reply's check, as before the restart."""
    bot, chat = owl(store)
    task = ask(store, chat, "写本周的销售报告。")
    worked(store, task, "read_file", "save_file")
    hold(store, task, CHINESE)
    assert store.recover(OWNER) == 1
    assert store.task(OWNER, task["id"])["status"] == "queued"
    assert store.claim(OWNER)["id"] == task["id"]
    store.finish(OWNER, task["id"], "completed", content="报告已保存。", checked=True)
    assert [p["name"] for p in current(store, bot)] == [CHINESE["name"]]


def test_a_run_still_waiting_for_the_persons_answer_keeps_nothing(store):
    bot, chat = owl(store)
    task = ask(store, chat)
    worked(store, task)
    hold(store, task)
    ClarificationStore(store, OWNER).ask(task, AskQuestion(question="Ποιον μήνα;"))
    store.finish(OWNER, task["id"], "completed", content="Ποιον μήνα;", checked=True)
    assert current(store, bot) == []
    assert {"reason": "waiting_for_person"} in [
        e["data"] for e in store.task(OWNER, task["id"])["events"]
    ]


def test_a_run_that_has_done_nothing_or_has_ended_cannot_write_one(store):
    bot, chat = owl(store)
    task = ask(store, chat)
    with pytest.raises(Conflict, match="no finished steps"):
        hold(store, task)
    # Reading playbooks is not work to write one from.
    worked(store, task, "read_playbook")
    with pytest.raises(Conflict, match="no finished steps"):
        hold(store, task)
    worked(store, task)
    store.finish(OWNER, task["id"], "completed", content="Έγινε.", checked=True)
    with pytest.raises(Conflict, match="still working"):
        hold(store, task)
    off, off_chat = owl(store, memory_enabled=False)
    other = ask(store, off_chat)
    worked(store, other)
    with pytest.raises(Conflict, match="Memory is off"):
        hold(store, other)
    store.finish(OWNER, other["id"], "completed", content="Έγινε.", checked=True)
    assert current(store, bot) == [] and current(store, off) == []


def test_one_per_run_saving_again_corrects_it(store):
    bot, chat = owl(store)
    task = ask(store, chat)
    worked(store, task)
    first = hold(store, task)
    second = hold(store, task, CHINESE)
    assert first["id"] != second["id"]
    store.finish(OWNER, task["id"], "completed", content="完成。", checked=True)
    assert [p["name"] for p in current(store, bot)] == [CHINESE["name"]]


def test_what_a_stopped_run_left_held_is_cleared_and_never_counts(store):
    bot, chat = owl(store)
    stopped = ask(store, chat)
    worked(store, stopped)
    hold(store, stopped)
    # Ended some other way than finish (as a stop from an approval card does).
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (stopped["id"],))
    kept(store, chat, CHINESE)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM playbooks WHERE status='held'").fetchone()[0] == 0
    assert [p["name"] for p in current(store, bot)] == [CHINESE["name"]]


# ---------------------------------------------------------------------------------------------
# In real turns: offered by what the turn can keep, kept only after the reply checker


def response(text="", calls=()):
    return LLMResponse(text, list(calls), {}, usage=LLMUsage(10, 5, 15))


def verdict(decision, **extra):
    return response(json.dumps({"decision": decision, "reason": "Checked.", **extra}))


class Model:
    model = "fixture"
    supports_tool_calling = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.responses)

    def tools(self, index=0):
        return {t["function"]["name"] for t in self.requests[index].get("tools") or []}

    def state(self, index=0):
        return "\n".join(str(m.get("content")) for m in self.requests[index]["messages"])


def run(store, model, *, review=True):
    rt = BotRuntime(store, OWNER, lambda _: model, limits=RuntimeLimits(calls_per_task=14))
    rt.reply_review = ReplyReview(rt) if review else None
    task = store.claim(OWNER)
    rt.run(task, EventCancellationToken(threading.Event()))
    return store.task(OWNER, task["id"])


def save_calls(playbook=GREEK, **changes):
    return [
        response(calls=[ToolCall("w", "save_file", {"name": "αναφορά.md", "content": "Σύνολο"})]),
        response(calls=[ToolCall("p", "save_playbook", {**playbook, **changes})]),
    ]


def test_in_a_chat_turn_the_reply_checker_decides_whether_it_is_kept(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    store.enqueue(
        OWNER, chat["id"], TaskInput(message="Η μηνιαία αναφορά;", request_id="message-0001")
    )
    model = Model([*save_calls(), response("Έτοιμη η αναφορά."), verdict("ready")])
    done = run(store, model)
    assert done["status"] == "completed"
    assert "save_playbook" in model.tools() and "read_playbook" not in model.tools()
    (tool,) = [t for t in model.requests[0]["tools"] if t["function"]["name"] == "save_playbook"]
    assert (
        "kept only if the run finishes and your reply passes its check"
        in (tool["function"]["description"])
    )
    # The reviewer saw the playbook the run wrote among its receipts.
    review = json.loads(model.requests[3]["messages"][1]["content"])
    assert model.requests[3]["messages"][0]["content"] == REVIEW_INSTRUCTIONS
    assert any("save_playbook" in json.dumps(m) for m in review["context_and_receipts"])
    [listed] = current(store, bot)
    assert listed["name"] == GREEK["name"]
    assert Playbooks(store, OWNER).read(bot["id"], listed["id"])["tools"] == ["save_file"]


def test_a_reply_given_as_a_plain_status_after_withheld_drafts_keeps_nothing(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    store.enqueue(
        OWNER, chat["id"], TaskInput(message="Η μηνιαία αναφορά;", request_id="message-0001")
    )
    wrong = {"issue": "contradiction", "false_claim": "Όλα έτοιμα."}
    model = Model(
        [
            *save_calls(),
            *[
                item
                for _ in range(3)
                for item in (response("Όλα έτοιμα."), verdict("repair", **wrong))
            ],
            response("Αποθηκεύτηκε το αναφορά.md· τα υπόλοιπα δεν ελέγχθηκαν."),
            verdict("ready"),
        ]
    )
    done = run(store, model)
    assert done["status"] == "completed" and done["output"].startswith("Αποθηκεύτηκε")
    assert current(store, bot) == []
    assert {"reason": "run_not_checked"} in [e["data"] for e in done["events"]]


def test_without_the_reply_checker_it_is_never_offered(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    store.enqueue(
        OWNER, chat["id"], TaskInput(message="Η μηνιαία αναφορά;", request_id="message-0001")
    )
    model = Model([response("Εδώ είναι.")])
    run(store, model, review=False)
    assert "save_playbook" not in model.tools()
    # The person switched the checker off: no reply is checked, so none is offered.
    store.reply_review_settings(OWNER, ReplyReviewSettings(enabled=False))
    store.enqueue(OWNER, chat["id"], TaskInput(message="Ξανά;", request_id="message-0002"))
    off = Model([response("Εδώ.")])
    run(store, off)
    assert "save_playbook" not in off.tools()


def test_later_turns_and_routines_see_the_index_and_read_one(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    _task, greek = kept(store, chat)
    _task, chinese = kept(store, chat, CHINESE)
    # The person's next message: the index, both tools.
    store.enqueue(
        OWNER, chat["id"], TaskInput(message="Και αυτό τον μήνα;", request_id="message-0002")
    )
    model = Model(
        [
            response(calls=[ToolCall("r", "read_playbook", {"playbook_id": greek["id"]})]),
            response("Ακολουθώ τα βήματα."),
            verdict("ready"),
        ]
    )
    run(store, model)
    state = model.state()
    assert "Your playbooks" in state
    assert GREEK["name"] in state and GREEK["when_to_use"] in state and CHINESE["name"] in state
    # A short index: the steps stay in the playbook until it is read.
    assert GREEK["steps"][0] not in state and CHINESE["done_check"] not in state
    assert {"read_playbook", "save_playbook"} <= model.tools()
    result = json.loads(
        next(m for m in model.requests[1]["messages"] if m["role"] == "tool")["content"]
    )
    assert (result["steps"], result["pitfalls"]) == (GREEK["steps"], GREEK["pitfalls"])
    assert result["done_check"] == GREEK["done_check"] and result["version"] == 1
    # A routine's run: the index and reading, but its reply isn't checked, so it keeps none.
    routine = store.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "每周报告",
                "prompt": "写每周销售报告。",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
    )
    store.run_schedule_now(OWNER, bot["id"], routine["id"], uuid4().hex, max_calls=None)
    routine_run = Model(
        [
            response(calls=[ToolCall("r", "read_playbook", {"playbook_id": chinese["id"]})]),
            response("完成。"),
        ]
    )
    run(store, routine_run)
    assert "Your playbooks" in routine_run.state() and CHINESE["name"] in routine_run.state()
    assert "read_playbook" in routine_run.tools() and "save_playbook" not in routine_run.tools()
    read = json.loads(
        next(m for m in routine_run.requests[1]["messages"] if m["role"] == "tool")["content"]
    )
    assert read["steps"] == CHINESE["steps"]
    with pytest.raises(NotFound):
        Playbooks(store, OWNER).read(bot["id"], uuid4().hex)


def test_memory_off_shows_no_playbooks_and_offers_no_tools(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    kept(store, chat)
    store.save_bot(
        OWNER,
        BotInput(name=bot["name"], purpose=bot["purpose"], memory_enabled=False),
        bot_id=bot["id"],
    )
    store.enqueue(OWNER, chat["id"], TaskInput(message="Η αναφορά;", request_id="message-0002"))
    model = Model([response("Εδώ."), verdict("ready")])
    run(store, model)
    assert "Your playbooks" not in model.state()
    assert not {"read_playbook", "save_playbook"} & model.tools()


def test_computer_work_keeps_a_playbook_only_with_a_checked_report(tmp_path):
    from test_bots_browser_runtime import response as work_response

    store, task, manager = _approved_computer_work(tmp_path)
    try:

        class Client:
            model = "fixture"
            supports_tool_calling = True
            script = [
                work_response(name="computer_run", arguments={"command": "printf inspected"}),
                work_response(name="save_playbook", arguments=CHINESE),
                work_response("报告已检查。"),
            ]

            def __init__(self):
                self.requests = []

            def chat(self, **kwargs):
                self.requests.append(kwargs)
                if kwargs["messages"][0]["content"] == REVIEW_INSTRUCTIONS:
                    return work_response('{"decision":"ready","reason":"Matches."}')
                return self.script.pop(0)

        client = Client()
        runtime = BotRuntime(store, OWNER, lambda _: client, desktop_computers=manager)
        runtime.reply_review = ReplyReview(runtime)
        runtime.limits = replace(runtime.limits, compaction_enabled=False)
        runtime.run(task, runtime.task_token(task["id"]))
        runtime.run(runtime.background.claim(), runtime.task_token(task["id"]), background=True)
        result = store.task(OWNER, task["id"])
        assert result["status"] == "completed", result
        offered = {t["function"]["name"] for t in client.requests[0]["tools"]}
        assert "save_playbook" in offered and "read_playbook" not in offered
        [listed] = Playbooks(store, OWNER).list(task["bot_id"])
        assert listed["name"] == CHINESE["name"]
        playbook = Playbooks(store, OWNER).read(task["bot_id"], listed["id"])
        assert playbook["tools"] == ["computer_run"]
    finally:
        manager.close()


def _approved_computer_work(tmp_path):
    """A turn the person approved for the owl's private computer, ready to run there."""
    from test_bots_desktop import Executor

    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore

    store = Store(tmp_path / "work.db")
    bot = store.save_bot(OWNER, BotInput(name="Mira", purpose="Work offline"))
    chat = store.create_chat(OWNER, bot["id"], "Conversation")
    store.enqueue(OWNER, chat["id"], TaskInput(message="准备报告", request_id="prepare-work"))
    task = store.claim(OWNER)
    proposals = ProposalStore(store, OWNER)
    proposal = proposals.propose(
        task,
        ActionProposal(
            question="Run the offline task?",
            instructions="Inspect the requested file.",
            offline_computer=True,
        ),
    )
    store.finish(OWNER, task["id"], "completed", content="Please review the scope.")
    proposals.decide(
        bot["id"],
        proposal["id"],
        ActionDecision(action="accept", expected_revision=1, offline_computer=True),
    )
    task = store.claim(OWNER)
    return store, task, DesktopComputers(OWNER, ComputerViews(), factory=Executor)


# ---------------------------------------------------------------------------------------------
# Versions, Undo and limits


def test_a_better_run_saves_a_new_version_and_undo_brings_the_earlier_one_back(store):
    bot, chat = owl(store)
    first_task, first = kept(store, chat)
    better = {**GREEK, "steps": [*GREEK["steps"], "Έλεγξε ότι τα σύνολα ταιριάζουν."]}
    second_task, second = kept(store, chat, better, replaces_playbook_id=first["id"])
    assert second["id"] == first["id"] and second["updates"] == GREEK["name"]
    [listed] = current(store, bot)
    assert (listed["version"], listed["earlier_versions"]) == (2, 1)
    assert Playbooks(store, OWNER).read(bot["id"], first["id"])["steps"] == better["steps"]
    # Under each reply: the earlier turn's version is no longer the current one.
    assert under_reply(store, chat, first_task["id"])["current"] is False
    assert under_reply(store, chat, second_task["id"]) == {
        "id": first["id"],
        "name": GREEK["name"],
        "version": 2,
        "current": True,
    }
    books = Playbooks(store, OWNER)
    # Undo takes back only the version the person saw.
    with pytest.raises(Conflict, match="changed since"):
        books.undo(bot["id"], first["id"], PlaybookUndo(version=1))
    assert books.undo(bot["id"], first["id"], PlaybookUndo(version=2)) == {
        "id": first["id"],
        "undone_version": 2,
        "restored_version": 1,
    }
    assert books.read(bot["id"], first["id"])["steps"] == GREEK["steps"]
    assert under_reply(store, chat, first_task["id"])["current"] is True
    assert under_reply(store, chat, second_task["id"]) is None
    # Undo of the first version forgets the playbook.
    assert books.undo(bot["id"], first["id"])["restored_version"] is None
    assert current(store, bot) == []
    with pytest.raises(NotFound):
        books.undo(bot["id"], first["id"])


def test_a_few_earlier_versions_are_kept_for_undo(store):
    bot, chat = owl(store)
    _task, first = kept(store, chat)
    for number in range(2, MAX_EARLIER_VERSIONS + 4):
        kept(
            store,
            chat,
            GREEK,
            done_check=f"Έλεγχος {number}",
            replaces_playbook_id=first["id"],
        )
    [listed] = current(store, bot)
    assert listed["version"] == MAX_EARLIER_VERSIONS + 3
    assert listed["earlier_versions"] == MAX_EARLIER_VERSIONS
    versions = Playbooks(store, OWNER).read(bot["id"], first["id"], for_person=True)["versions"]
    assert [v["version"] for v in versions] == list(range(MAX_EARLIER_VERSIONS + 3, 2, -1))


def test_an_owl_keeps_a_bounded_number_of_short_playbooks(store):
    bot, chat = owl(store)
    for number in range(MAX_PLAYBOOKS):
        kept(store, chat, CHINESE, name=f"报告 {number}")
    task = ask(store, chat)
    worked(store, task)
    with pytest.raises(Conflict, match=f"at most {MAX_PLAYBOOKS} playbooks"):
        hold(store, task)
    # Improving one still works at the limit.
    oldest = current(store, bot)[-1]
    hold(store, task, CHINESE, name=oldest["name"], replaces_playbook_id=oldest["id"])
    store.finish(OWNER, task["id"], "completed", content="完成。", checked=True)
    assert len(current(store, bot)) == MAX_PLAYBOOKS
    # Each one is short.
    for change in (
        {"steps": [f"第{n}步" for n in range(13)]},
        {"pitfalls": [f"问题{n}" for n in range(7)]},
        {"name": "名" * 81},
        {"when_to_use": "时" * 201},
        {"done_check": "查" * 301},
        {"steps": ["好", "步" * 301]},
        {"steps": []},
        {"done_check": "   "},
    ):
        with pytest.raises(ValidationError):
            PlaybookInput(**{**CHINESE, **change})


def test_greek_and_chinese_playbooks_round_trip_and_names_never_repeat(store):
    bot, chat = owl(store)
    _greek_task, greek = kept(store, chat)
    _chinese_task, chinese = kept(store, chat, CHINESE)
    books = Playbooks(store, OWNER)
    for held, source in ((greek, GREEK), (chinese, CHINESE)):
        read = books.read(bot["id"], held["id"])
        assert {key: read[key] for key in source} == source
    assert [p["name"] for p in books.index(bot["id"])] == [CHINESE["name"], GREEK["name"]]
    task = ask(store, chat)
    worked(store, task)
    # The same name in capitals, and in capitals without its accents, is the same playbook.
    for name in ("ΜΗΝΙΑΊΑ ΑΝΑΦΟΡΆ ΠΩΛΉΣΕΩΝ", "ΜΗΝΙΑΙΑ ΑΝΑΦΟΡΑ ΠΩΛΗΣΕΩΝ", "  每周销售报告 "):
        with pytest.raises(Conflict, match="already have a playbook named"):
            hold(store, task, GREEK, name=name)
    # Improving it under its own name is fine.
    hold(store, task, GREEK, name="Μηνιαία αναφορά πωλήσεων", replaces_playbook_id=greek["id"])
    store.finish(OWNER, task["id"], "completed", content="Έγινε.", checked=True)


def test_what_changed_while_the_run_worked_wins(store):
    bot, chat = owl(store)
    _task, greek = kept(store, chat)
    books = Playbooks(store, OWNER)
    # The person deletes the playbook a run is improving.
    task = ask(store, chat)
    worked(store, task)
    hold(store, task, GREEK, done_check="Νέος έλεγχος", replaces_playbook_id=greek["id"])
    books.delete(bot["id"], greek["id"])
    store.finish(OWNER, task["id"], "completed", content="Έγινε.", checked=True)
    assert current(store, bot) == []
    assert {"reason": "playbook_removed"} in [
        e["data"] for e in store.task(OWNER, task["id"])["events"]
    ]
    # Two runs at once (one carries on in the background) write a playbook of the same name:
    # the second to finish keeps nothing.
    first = ask(store, chat, "α")
    worked(store, first)
    hold(store, first, CHINESE)
    background = BackgroundStore(store, OWNER)
    background.submit(first, "Η αναφορά", [])
    assert background.claim()["id"] == first["id"]
    second = ask(store, chat, "β")
    worked(store, second)
    hold(store, second, CHINESE)
    for run_task in (first, second):
        store.finish(OWNER, run_task["id"], "completed", content="完成。", checked=True)
    assert [p["name"] for p in current(store, bot)] == [CHINESE["name"]]
    assert {"reason": "name_taken"} in [
        e["data"] for e in store.task(OWNER, second["id"])["events"]
    ]


# ---------------------------------------------------------------------------------------------
# Which turns are offered the tools


def test_tools_follow_what_the_turn_can_use():
    plain = set(tool_names(Turn(answers_person=True, memory=True)))
    assert not {"read_playbook", "save_playbook"} & plain
    with_both = tool_names(
        Turn(answers_person=True, memory=True, playbooks=True, reply_checked=True)
    )
    assert {"read_playbook", "save_playbook"} <= set(with_both)
    assert not {"read_playbook", "save_playbook"} & set(
        tool_names(Turn(answers_person=True, memory=False, playbooks=True, reply_checked=True))
    )
    # A first hello writes no job down; a room leaves it to the owl's own chat.
    for turn in (
        Turn(answers_person=True, memory=True, reply_checked=True, first_hello=True),
        Turn(answers_person=True, memory=True, reply_checked=True, room=True),
    ):
        assert "save_playbook" not in tool_names(turn)
    assert "save_playbook" in ROOM_TURN_LEFT_OUT
    review = tool_names(Turn(answers_person=False, activation=True, memory=True, playbooks=True))
    assert "read_playbook" in review and set(review) <= ACTIVATION_REVIEW_TOOLS
    assert "read_playbook" in EVERYDAY_TOOLS and "save_playbook" not in EVERYDAY_TOOLS
    background = tool_names(
        Turn(answers_person=False, background=True, memory=True, playbooks=True, reply_checked=True)
    )
    assert {"read_playbook", "save_playbook"} <= set(background)


# ---------------------------------------------------------------------------------------------
# A moved owl brings its playbooks; a deleted one takes them with it


def test_playbooks_travel_with_a_moved_owl(tmp_path):
    from test_bots_owl_move import copy_files, records_of, send_pages, staged

    from alysis_code.bots.owl_move import require_unchanged

    source = Store(tmp_path / "here" / "bots.sqlite3")
    target = Store(tmp_path / "there" / "bots.sqlite3")
    bot, chat = owl(source)
    _task, greek = kept(source, chat)
    kept(source, chat, GREEK, done_check="Νέος έλεγχος", replaces_playbook_id=greek["id"])
    kept(source, chat, CHINESE)
    # A change to a playbook is a change a move notices.
    with source.connect() as db:
        before = owl_fingerprint(db, bot["id"])
    Playbooks(source, OWNER).undo(bot["id"], greek["id"])
    with source.connect() as db:
        assert owl_fingerprint(db, bot["id"]) != before
    kept(source, chat, GREEK, done_check="Νέος έλεγχος", replaces_playbook_id=greek["id"])
    moves, move_id, pages = staged(source, target, bot["id"])
    records = [r for r in records_of(pages) if r["kind"] == "playbook"]
    assert sorted(len(r["versions"]) for r in records) == [1, 2]
    send_pages(moves, move_id, pages)
    assert copy_files(source, target, bot["id"], moves.receipt(move_id), pages)
    done = moves.complete(move_id)
    source.delete_bot(
        OWNER, bot["id"], check=lambda db: require_unchanged(db, bot["id"], pages[0]["fingerprint"])
    )
    copy_id = done["owl"]["id"]
    books = Playbooks(target, OWNER)
    moved = {p["name"]: p for p in books.list(copy_id)}
    assert set(moved) == {GREEK["name"], CHINESE["name"]}
    assert (moved[GREEK["name"]]["version"], moved[GREEK["name"]]["earlier_versions"]) == (2, 1)
    assert books.read(copy_id, moved[GREEK["name"]]["id"])["done_check"] == "Νέος έλεγχος"
    assert books.read(copy_id, moved[CHINESE["name"]]["id"])["steps"] == CHINESE["steps"]
    # Each version keeps the turn that wrote it, and the chat shows it under that reply.
    moved_chat = target.list_chats(OWNER, copy_id)[0]
    turns = target.conversation(OWNER, moved_chat["id"])["tasks"]
    under = [t["playbook"] for t in turns if t.get("playbook")]
    assert sorted((p["name"], p["version"], p["current"]) for p in under) == sorted(
        [
            (GREEK["name"], 1, False),
            (GREEK["name"], 2, True),
            (CHINESE["name"], 1, True),
        ]
    )
    # Undo works in its new place.
    assert books.undo(copy_id, moved[GREEK["name"]]["id"])["restored_version"] == 1
    # The original went with the move.
    with source.connect() as db:
        assert db.execute("SELECT count(*) FROM playbooks").fetchone()[0] == 0


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda r: r["versions"][0].update(current=False), id="no-current"),
        pytest.param(lambda r: r["versions"].append(dict(r["versions"][0])), id="repeated"),
        pytest.param(lambda r: r["versions"][0].update(steps=["β"] * 13), id="too-long"),
        pytest.param(lambda r: r["versions"][0].update(tools=["a b"]), id="odd-tool-name"),
        pytest.param(lambda r: r.update(extra=1), id="unknown-field"),
    ],
)
def test_a_tampered_playbook_is_refused(tmp_path, tamper):
    from test_bots_owl_move import records_of, staged

    source = Store(tmp_path / "here" / "bots.sqlite3")
    target = Store(tmp_path / "there" / "bots.sqlite3")
    bot, chat = owl(source)
    kept(source, chat, CHINESE)
    moves, move_id, pages = staged(source, target, bot["id"])
    records = copy.deepcopy(records_of(pages))
    tamper(next(r for r in records if r["kind"] == "playbook"))
    with pytest.raises(InvalidMove):
        moves.receive(move_id, MovePage(page=0, last=True, records=records))
    assert moves.receipt(move_id)["bot_id"] is None
    assert MAX_COUNTS["playbook"] == MAX_PLAYBOOKS


def test_two_moved_playbooks_of_one_name_are_refused(tmp_path):
    from test_bots_owl_move import records_of, staged

    source = Store(tmp_path / "here" / "bots.sqlite3")
    target = Store(tmp_path / "there" / "bots.sqlite3")
    bot, chat = owl(source)
    kept(source, chat, CHINESE)
    moves, move_id, pages = staged(source, target, bot["id"])
    records = copy.deepcopy(records_of(pages))
    playbook = next(r for r in records if r["kind"] == "playbook")
    records.append({**copy.deepcopy(playbook), "id": "f" * 32})
    with pytest.raises(InvalidMove, match="same name"):
        moves.receive(move_id, MovePage(page=0, last=True, records=records))
    assert isinstance(OwlMoves(target, OWNER).receipt(move_id), dict)


def test_deleting_the_owl_erases_its_playbooks(store):
    bot, chat = owl(store)
    kept(store, chat)
    _task, chinese = kept(store, chat, CHINESE)
    kept(store, chat, CHINESE, done_check="新的检查", replaces_playbook_id=chinese["id"])
    held = ask(store, chat)
    worked(store, held)
    hold(store, held, GREEK, name="Άλλο")
    store.finish(OWNER, held["id"], "cancelled")
    store.delete_bot(OWNER, bot["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM playbooks").fetchone()[0] == 0
    with pytest.raises(NotFound):
        Playbooks(store, OWNER).list(bot["id"])


# ---------------------------------------------------------------------------------------------
# The app's routes, and a database from before playbooks


def test_routes_and_capability(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "playbooks.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    store, owner = app.state.store, settings.owner
    bot = store.save_bot(owner, BotInput(name="フクロウ"))
    chat = store.create_chat(owner, bot["id"], "Chat")
    books = Playbooks(store, owner)
    tasks = []
    for playbook, changes in ((CHINESE, {}), (CHINESE, {"done_check": "再检查一次。"})):
        store.enqueue(owner, chat["id"], TaskInput(message="报告", request_id=uuid4().hex))
        task = store.claim(owner)
        store.event(owner, task["id"], "tool_completed", {"name": "read_file"})
        if tasks:
            changes = {**changes, "replaces_playbook_id": tasks[0][1]["id"]}
        held = books.hold(task, PlaybookInput(**{**playbook, **changes}))
        store.finish(owner, task["id"], "completed", content="完成。", checked=True)
        tasks.append((task, held))
    playbook_id = tasks[0][1]["id"]
    base = f"/v1/bots/{bot['id']}/playbooks"
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "owl_playbooks" in client.get("/v1/status").json()["capabilities"]
        [listed] = client.get(base).json()
        assert listed["name"] == CHINESE["name"] and listed["version"] == 2
        assert listed["earlier_versions"] == 1
        one = client.get(f"{base}/{playbook_id}").json()
        assert one["steps"] == CHINESE["steps"] and one["done_check"] == "再检查一次。"
        assert [v["version"] for v in one["versions"]] == [2, 1]
        page = client.post(f"/v1/chats/{chat['id']}/page", json={}).json()
        assert [t.get("playbook", {}).get("version") for t in page["tasks"]] == [1, 2]
        assert client.post(f"{base}/{playbook_id}/undo", json={"version": 1}).status_code == 409
        assert client.post(f"{base}/{playbook_id}/undo", json={"versio": 2}).status_code == 422
        undone = client.post(f"{base}/{playbook_id}/undo", json={"version": 2})
        assert undone.status_code == 200 and undone.json()["restored_version"] == 1
        assert client.get(f"{base}/{playbook_id}").json()["done_check"] == CHINESE["done_check"]
        assert client.delete(f"{base}/{playbook_id}").status_code == 204
        assert client.get(base).json() == []
        assert client.get(f"{base}/{playbook_id}").status_code == 404
        assert client.delete(f"{base}/{playbook_id}").status_code == 404
        assert client.post(f"{base}/{playbook_id}/undo").status_code == 404
        assert client.get(f"/v1/bots/{uuid4().hex}/playbooks").status_code == 404


def test_an_earlier_database_gains_playbooks_on_open(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    with store.connect(write=True) as db:
        db.execute("DROP TABLE playbooks")
        db.execute("PRAGMA user_version=44")  # the schema before playbooks (45)
    reopened = Store(tmp_path / "bots.db")
    with reopened.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 45
    _task, held = kept(reopened, chat)
    assert [p["id"] for p in current(reopened, bot)] == [held["id"]]
    # A database from a newer engine is still refused.
    with reopened.connect(write=True) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="Unsupported"):
        Store(tmp_path / "bots.db")
