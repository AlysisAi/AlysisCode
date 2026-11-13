"""Provider fixtures exercise real task/store behavior; no live-model claims."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_bots_coding import handoff as handoff
from test_bots_runtime import ScriptedClient, response

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.clarifications import AskQuestion, ClarificationStore, QuestionDecision
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import ToolCall


def setup(tmp_path):
    store = Store(tmp_path / "questions.db")
    bot = store.save_bot(
        "owner", BotInput(name="Planner", purpose="Prepare workshop", memory_enabled=False)
    )
    chat = store.create_chat("owner", bot["id"], "Workshop")
    task = store.enqueue(
        "owner",
        chat["id"],
        TaskInput(
            message="Make a guide. Ask for missing date and keep working on independent parts.",
            request_id=uuid4().hex,
        ),
    )
    return store, bot, chat, task, ClarificationStore(store, "owner")


def run(store, client):
    runtime = BotRuntime(store, "owner", lambda _: client)
    runtime.run(store.claim("owner"), EventCancellationToken(threading.Event()))
    return runtime


def test_question_draft_unrelated_chat_restart_answer_revision(tmp_path):
    store, bot, chat, task, questions = setup(tmp_path)
    client = ScriptedClient(
        [
            response(
                calls=[
                    ToolCall(
                        "ask",
                        "ask_clarification",
                        {"question": "Πότε / いつ?", "options": ["Leave placeholders"]},
                    )
                ]
            ),
            response(
                calls=[
                    ToolCall(
                        "draft",
                        "save_file",
                        {"name": "guide.md", "content": "Draft: [DATE TBD]\nMaterials: paper"},
                    )
                ]
            ),
            response("Draft ready; date is still pending."),
        ]
    )
    run(store, client)
    question = questions.list(bot["id"])[0]
    assert question["status"] == "pending"
    first = store.list_files("owner", bot["id"])[0]
    store.enqueue(
        "owner", chat["id"], TaskInput(message="What is 7 times 8?", request_id=uuid4().hex)
    )
    run(store, ScriptedClient([response("56")]))
    assert questions.list(bot["id"])[0]["status"] == "pending"
    store = Store(store.path)
    questions = ClarificationStore(store, "owner")
    decision = QuestionDecision(
        action="answer", expected_revision=1, answer="6 November, 10:30; keep Νεφέλη / 東京"
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(
            pool.map(lambda _: questions.decide(bot["id"], question["id"], decision), range(4))
        )
    assert len({a["answer_task_id"] for a in answers}) == 1
    assert len(store.conversation("owner", chat["id"])["tasks"]) == 3
    revised = ScriptedClient(
        [
            response(
                calls=[
                    ToolCall(
                        "revise",
                        "save_file",
                        {
                            "name": first["name"],
                            "content": "6 November 10:30 — Νεφέλη / 東京",
                            "previous_file_id": first["id"],
                        },
                    )
                ]
            ),
            response("Updated the guide."),
        ]
    )
    run(store, revised)
    assert task["message"] in json.dumps(revised.requests, ensure_ascii=False)
    assert questions.list(bot["id"])[0]["status"] == "answered"
    assert store.file("owner", bot["id"], first["id"])["content"].startswith("Draft:")
    assert len(store.list_files("owner", bot["id"])) == 2


def test_answer_arriving_during_response_discards_stale_action_and_has_one_work_owner(tmp_path):
    store, bot, chat, task, questions = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()

    class Client(ScriptedClient):
        def chat(self, **kw):
            if len(self.requests) == 1:
                entered.set()
                assert release.wait(5)
            return super().chat(**kw)

    client = Client(
        [
            response(calls=[ToolCall("ask", "ask_clarification", {"question": "Which date?"})]),
            response(
                calls=[ToolCall("stale", "save_file", {"name": "wrong.md", "content": "guessed"})]
            ),
            response(
                calls=[
                    ToolCall(
                        "current", "save_file", {"name": "current.md", "content": "6 November"}
                    )
                ]
            ),
            response("Current answer used."),
        ]
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, store, client)
        assert entered.wait(5)
        q = questions.list(bot["id"])[0]
        result = questions.decide(
            bot["id"],
            q["id"],
            QuestionDecision(action="answer", answer="6 November", expected_revision=1),
        )
        assert result["delivered_to_source"]
        release.set()
        future.result(timeout=5)
    assert [f["name"] for f in store.list_files("owner", bot["id"])] == ["current.md"]

    class Acknowledge(ScriptedClient):
        def chat(self, **kw):
            assert not kw["tools"]  # Do not start a competing copy of the work.
            return super().chat(**kw)

    run(store, Acknowledge([response("Your answer was delivered to the original task.")]))
    assert len(store.list_files("owner", bot["id"])) == 1
    assert store.task("owner", task["id"])["calls"] == 4


@pytest.mark.parametrize("terminal", ["cancelled", "failed", "interrupted"])
def test_stopped_or_uncertain_source_cannot_be_revived_by_question(tmp_path, terminal):
    store, bot, chat, task, questions = setup(tmp_path)
    claimed = store.claim("owner")
    q = questions.ask(claimed, AskQuestion(question="When?"))
    store.finish("owner", task["id"], terminal)
    with pytest.raises(Conflict):
        questions.decide(
            bot["id"],
            q["id"],
            QuestionDecision(action="answer", expected_revision=1, answer="Tomorrow"),
        )
    assert questions.list(bot["id"])[0]["status"] == "closed"
    assert len(store.conversation("owner", chat["id"])["tasks"]) == 1


def test_close_is_not_an_answer_and_other_bot_or_owner_cannot_resolve(tmp_path):
    store, bot, chat, task, questions = setup(tmp_path)
    q = questions.ask(store.claim("owner"), AskQuestion(question="When?", options=["Later"]))
    other = store.save_bot("owner", BotInput(name="Other", purpose="Separate"))
    decision = QuestionDecision(action="answer", expected_revision=1, answer="Tomorrow")
    with pytest.raises(NotFound):
        questions.decide(other["id"], q["id"], decision)
    with pytest.raises(NotFound):
        ClarificationStore(store, "someone-else").decide(bot["id"], q["id"], decision)
    with pytest.raises(Conflict):
        questions.decide(bot["id"], q["id"], decision.model_copy(update={"expected_revision": 2}))
    closed = questions.decide(
        bot["id"], q["id"], QuestionDecision(action="close", expected_revision=1)
    )
    assert closed["status"] == "closed" and closed["answer"] is None
    assert store.task("owner", task["id"])["status"] == "running"
    assert len(store.conversation("owner", chat["id"])["tasks"]) == 1


def test_question_http_routes_auth_and_validation(tmp_path):
    settings = Settings(
        tmp_path / "api.db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/v1/bots/missing/questions").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Bot", "purpose": "Test"}).json()
        path = f"/v1/bots/{bot['id']}/questions"
        assert client.get(path).json() == []
        assert (
            client.patch(
                path + "/missing", json={"action": "answer", "answer": "", "expected_revision": 1}
            ).status_code
            == 422
        )
        assert (
            client.patch(
                path + "/missing", json={"action": "close", "expected_revision": 1}
            ).status_code
            == 404
        )


def test_real_background_worker_keeps_chat_responsive_and_consumes_answer(tmp_path):
    from test_bots_adaptive import wait

    store, bot, chat, source, questions = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    counts = {}

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def __init__(self, task_id):
            self.task_id = task_id

        def chat(self, **kw):
            n = counts[self.task_id] = counts.get(self.task_id, 0) + 1
            if self.task_id != source["id"]:
                return response("56" if kw["tools"] else "Answer saved for the current work.")
            if n == 1:
                return response(calls=[ToolCall("ask", "ask_clarification", {"question": "When?"})])
            if n == 2:
                return response(
                    calls=[
                        ToolCall(
                            "draft",
                            "save_file",
                            {"name": "guide.md", "content": "DRAFT — [DATE TBD]"},
                        )
                    ]
                )
            if n == 3:
                return response(
                    calls=[
                        ToolCall(
                            "background",
                            "continue_in_background",
                            {"summary": "Finishing the independent parts"},
                        )
                    ]
                )
            if n == 4:
                entered.set()
                assert release.wait(6)
                return response(
                    calls=[
                        ToolCall(
                            "stale", "save_file", {"name": "stale.md", "content": "Wrong date"}
                        )
                    ]
                )
            if n == 5:
                assert "6 November — 東京" in str(kw["messages"])
                previous = store.list_files("owner", bot["id"])[0]
                return response(
                    calls=[
                        ToolCall(
                            "update",
                            "save_file",
                            {
                                "name": "guide.md",
                                "content": "6 November — 東京",
                                "previous_file_id": previous["id"],
                            },
                        )
                    ]
                )
            return response("Updated guide ready.")

    runtime = BotRuntime(store, "owner", Client)
    runtime.start()
    try:
        assert entered.wait(6)
        unrelated = store.enqueue(
            "owner", chat["id"], TaskInput(message="What is 7 times 8?", request_id=uuid4().hex)
        )
        runtime.wake()
        wait(lambda: store.task("owner", unrelated["id"]), lambda t: t["status"] == "completed")
        assert store.task("owner", source["id"])["status"] == "waiting"
        q = questions.list(bot["id"])[0]
        assert q["status"] == "pending"
        answer = questions.decide(
            bot["id"],
            q["id"],
            QuestionDecision(action="answer", expected_revision=1, answer="6 November — 東京"),
        )
        runtime.wake()
        wait(
            lambda: store.task("owner", answer["answer_task_id"]),
            lambda t: t["status"] == "completed",
        )
        release.set()
        wait(lambda: store.task("owner", source["id"]), lambda t: t["status"] == "completed")
        files = store.list_files("owner", bot["id"])
        assert len(files) == 2 and {f["name"] for f in files} == {"guide.md"}
        assert (
            store.file("owner", bot["id"], next(f["id"] for f in files if f["revision"] == 1))[
                "content"
            ]
            == "DRAFT — [DATE TBD]"
        )
        assert (
            store.file("owner", bot["id"], next(f["id"] for f in files if f["revision"] == 2))[
                "content"
            ]
            == "6 November — 東京"
        )
    finally:
        release.set()
        runtime.stop()


def test_queued_background_answer_keeps_one_work_owner_and_fences_stale_writes(tmp_path):
    from alysis_code.bots.background import BackgroundStore

    store, bot, chat, source, questions = setup(tmp_path)
    task = store.claim("owner")
    q = questions.ask(task, AskQuestion(question="Which date?"))
    bg = BackgroundStore(store, "owner")
    bg.submit(task, "Preparing draft", [{"role": "user", "content": source["message"]}])
    answer = questions.decide(
        bot["id"],
        q["id"],
        QuestionDecision(action="answer", expected_revision=1, answer="Tomorrow"),
    )
    assert answer["delivered_to_source"]
    bg.claim()
    with pytest.raises(Conflict):
        store.save_file(
            "owner",
            bot["id"],
            FileInput(name="stale.md", content="No answer"),
            source_task_id=source["id"],
        )
    assert bg.updates(source["id"])[0]["content"] == "Tomorrow"
    store.save_file(
        "owner",
        bot["id"],
        FileInput(name="current.md", content="Tomorrow"),
        source_task_id=source["id"],
    )
    # An acknowledgement never receives tools, even if a provider invents one.
    bad = ScriptedClient(
        [
            response(
                calls=[
                    ToolCall("duplicate", "save_file", {"name": "duplicate.md", "content": "No"})
                ]
            )
        ]
    )
    run(store, bad)
    assert [f["name"] for f in store.list_files("owner", bot["id"])] == ["current.md"]
    assert store.task("owner", answer["answer_task_id"])["status"] == "failed"


def test_answer_reaches_existing_coding_job_without_starting_another(handoff):
    store, coding, bot, workspace, worker, task = handoff
    questions = ClarificationStore(store, "owner")
    q = questions.ask(task, AskQuestion(question="What label should the report use?"))
    job = coding.submit(
        task, "Prepare independent report layout", [{"role": "user", "content": task["message"]}]
    )
    decision = QuestionDecision(action="answer", expected_revision=1, answer="Νεφέλη / 東京")
    saved = questions.decide(bot["id"], q["id"], decision)
    assert (
        questions.decide(bot["id"], q["id"], decision)["answer_task_id"] == saved["answer_task_id"]
    )
    claimed = coding.claim(workspace, worker)
    assert claimed["id"] == job["id"]
    with store.connect() as db:
        instructions = db.execute(
            "SELECT content FROM coding_instructions WHERE job_id=?", (job["id"],)
        ).fetchall()
        assert [r["content"] for r in instructions] == ["Νεφέλη / 東京"]
        assert db.execute("SELECT count(*) FROM coding_jobs").fetchone()[0] == 1
