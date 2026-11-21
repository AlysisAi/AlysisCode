"""Routines that use the owl's computer, browser and websites the person allowed for them.

The owl declares a routine's needs as structured data; the person allows them once for that
routine; each run gets exactly what was allowed when it was queued, without a card. A run that
needs anything else pauses its routine with a plain reason and shows in Needs you. Real
persistence and the real runtime loop; the model and the computers are fixtures.
"""

from __future__ import annotations

import json
import time

import pytest
from test_bots_browser_runtime import BrowserFixture, ClientFixture, advertised_names, response
from test_bots_desktop import Executor

from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.routine_access import RoutineAccessDecision, RoutineNeeds
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, NotFound, Store

SITE = "https://shop.example"
OTHER = "https://news.example"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "routine-access.db")


@pytest.fixture
def bot(store):
    return store.save_bot("owner", BotInput(name="Ήρα", purpose="Καθημερινός έλεγχος"))["id"]


def routine(store, bot, *, needs=None, prompt="Έλεγξε τις τιμές και γράψε μια σύνοψη.", **extra):
    return store.save_schedule(
        "owner",
        bot,
        ScheduleInput.model_validate(
            {
                "name": extra.pop("name", "毎朝の確認"),
                "prompt": prompt,
                "timing": {"kind": "interval", "every_minutes": 60},
                **({"needs": needs} if needs is not None else {}),
                **extra,
            }
        ),
        now=time.time() - 3600,
    )


def allow(store, bot, item, **extra):
    current = next(s for s in store.list_schedules("owner", bot) if s["id"] == item["id"])
    return store.allow_routine_access(
        "owner",
        bot,
        item["id"],
        RoutineAccessDecision(
            expected_revision=current["revision"],
            allow=current["access"]["to_allow"],
            **extra,
        ),
    )


def start_run(store, item):
    """Queue the routine's due run and start it, as the scheduler and worker do."""
    assert store.enqueue_due_schedules("owner", max_calls=None, now=item["next_run_at"]) == 1
    return store.claim("owner")


def listed(store, bot, item):
    return next(s for s in store.list_schedules("owner", bot) if s["id"] == item["id"])


class OriginBrowser(BrowserFixture):
    """Records which website origins the runtime passes as approved."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.approved = []

    def schemas(self, task, eligible, *, approved_origins=(), image_input=False, **_):
        self.approved.append(("schemas", tuple(approved_origins)))
        return super().schemas(task, eligible, approved_origins=approved_origins)

    def call(self, task, name, args, token, *, approved_origins=(), **kwargs):
        self.approved.append(("call", tuple(approved_origins)))
        return super().call(task, name, args, token, approved_origins=approved_origins, **kwargs)


def tool_results(client):
    return [
        json.loads(m["content"])
        for m in client.requests[-1]["messages"]
        if m["role"] == "tool" and m["content"].startswith("{")
    ]


def test_needs_and_grants_belong_to_one_routine(store, bot):
    shop = routine(store, bot, needs={"sites": [SITE + "/"]}, name="Τιμές")
    news = routine(store, bot, needs={"online_browser": True}, name="ニュース")
    plain = routine(store, bot, name="Σημειώσεις")

    # Declared needs are shown as given, normalised: a site is used through the browser.
    assert listed(store, bot, shop)["needs"] == {
        "offline_computer": False,
        "online_browser": True,
        "sites": [SITE],
    }
    assert listed(store, bot, shop)["access"] == {
        "allowed": None,
        "allowed_at": None,
        "requested": None,
        "to_allow": {"offline_computer": False, "online_browser": True, "sites": [SITE]},
    }
    # A routine that needs nothing asks for nothing.
    assert listed(store, bot, plain)["access"]["to_allow"] is None

    allow(store, bot, shop)
    assert listed(store, bot, shop)["access"]["allowed"]["sites"] == [SITE]
    assert listed(store, bot, shop)["access"]["to_allow"] is None
    # Allowing one routine allows nothing for another.
    assert listed(store, bot, news)["access"]["allowed"] is None
    assert listed(store, bot, news)["access"]["to_allow"]["online_browser"] is True


def test_the_person_allows_exactly_what_their_app_showed(store, bot):
    item = routine(store, bot, needs={"sites": [SITE]})
    current = listed(store, bot, item)
    with pytest.raises(Conflict, match="access changed"):
        store.allow_routine_access(
            "owner",
            bot,
            item["id"],
            RoutineAccessDecision(
                expected_revision=current["revision"],
                allow={"online_browser": True, "sites": [SITE, OTHER]},
            ),
        )
    with pytest.raises(Conflict, match="routine changed"):
        store.allow_routine_access(
            "owner",
            bot,
            item["id"],
            RoutineAccessDecision(
                expected_revision=current["revision"] + 1, allow=current["access"]["to_allow"]
            ),
        )
    assert listed(store, bot, item)["access"]["allowed"] is None
    with pytest.raises(NotFound):
        store.allow_routine_access(
            "someone-else",
            bot,
            item["id"],
            RoutineAccessDecision(
                expected_revision=current["revision"], allow=current["access"]["to_allow"]
            ),
        )
    # Allow and turn on, in one decision; sending it again changes nothing.
    on = allow(store, bot, item, turn_on=True)
    assert on["state"] == "active" and on["access"]["allowed"]["sites"] == [SITE]
    again = store.allow_routine_access(
        "owner",
        bot,
        item["id"],
        RoutineAccessDecision(
            expected_revision=current["revision"], allow=current["access"]["to_allow"], turn_on=True
        ),
    )
    assert again["revision"] == on["revision"]


def test_an_allowed_run_uses_exactly_its_sites_without_a_card(store, bot):
    item = routine(store, bot, needs={"sites": [SITE]})
    allow(store, bot, item, turn_on=True)
    item = listed(store, bot, item)
    task = start_run(store, item)
    browser = OriginBrowser()
    client = ClientFixture(
        [response(name="browser_click", arguments={"target": "Προσθήκη"}), response("Έτοιμο.")]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, browser_computers=browser)

    runtime.run(task, runtime.task_token(task["id"]))

    assert store.task("owner", task["id"])["status"] == "completed"
    assert browser.eligibility == [(task["id"], True)]
    assert "browser_click" in advertised_names(client.requests[0])
    assert ("call", (SITE,)) in browser.approved
    assert len(browser.calls) == 1
    assert runtime.proposals.list(bot) == []
    # The task state tells the owl what this routine allows.
    state = "\n".join(
        m["content"] for m in client.requests[0]["messages"] if isinstance(m["content"], str)
    )
    assert "This routine run may use only what the person allowed" in state
    assert SITE in state
    assert listed(store, bot, item)["state"] == "active"


def test_an_allowed_offline_computer_is_used_without_a_card(store, bot):
    item = routine(store, bot, needs={"offline_computer": True})
    allow(store, bot, item, turn_on=True)
    task = start_run(store, listed(store, bot, item))
    instances = []

    def factory(*args, **kwargs):
        instances.append(Executor(*args, **kwargs))
        return instances[-1]

    manager = DesktopComputers("owner", ComputerViews(), factory=factory)
    client = ClientFixture(
        [response(name="computer_run", arguments={"command": "fixture"}), response("Έγινε.")]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    try:
        runtime.run(task, runtime.task_token(task["id"]))
        queued = runtime.background.claim()
        assert queued is not None and queued["id"] == task["id"]
        runtime.run(queued, runtime.task_token(queued["id"]), background=True)
    finally:
        manager.close()

    assert store.task("owner", task["id"])["status"] == "completed"
    assert len(instances) == 1
    assert [(name, args["command"]) for name, args in instances[0].calls] == [("run", "fixture")]
    assert runtime.proposals.list(bot) == []
    assert listed(store, bot, item)["state"] == "active"


def test_a_routine_allowed_nothing_gets_no_computer_or_browser(store, bot):
    item = routine(store, bot, needs={"offline_computer": True, "sites": [SITE]}, enabled=True)
    task = start_run(store, item)
    browser = OriginBrowser()
    client = ClientFixture([response("Τίποτα.")])
    runtime = BotRuntime(store, "owner", lambda _: client, browser_computers=browser)
    runtime.run(task, runtime.task_token(task["id"]))
    assert browser.eligibility == [(task["id"], False)]
    assert browser.calls == []


@pytest.mark.parametrize(
    ("needs", "asked", "expected", "reason"),
    [
        # A new site when the routine's sites were allowed.
        (
            {"sites": [SITE]},
            {"browser_origin": OTHER},
            {"offline_computer": False, "online_browser": True, "sites": [OTHER]},
            "Needs approval for a new site: " + OTHER,
        ),
        # The computer when only the browser was allowed.
        (
            {"online_browser": True},
            {"offline_computer": True},
            {"offline_computer": True, "online_browser": False, "sites": []},
            "Needs approval for the owl's offline computer",
        ),
    ],
)
def test_a_run_that_needs_more_pauses_its_routine_and_asks_the_person(
    store, bot, needs, asked, expected, reason
):
    item = routine(store, bot, needs=needs)
    allow(store, bot, item, turn_on=True)
    task = start_run(store, listed(store, bot, item))
    browser = OriginBrowser()
    client = ClientFixture(
        [
            response(
                name="propose_action",
                arguments={
                    "question": "Να συνεχίσω;",
                    "instructions": "Σύγκρινε και με τον δεύτερο ιστότοπο.",
                    "offline_computer": False,
                    "browser_origin": None,
                    **asked,
                },
            ),
            response("Χρειάζομαι την άδειά σου για να συνεχίσω."),
        ]
    )
    # Both are available, so what the run asks for is about permission only.
    desktop = DesktopComputers("owner", ComputerViews(), factory=Executor)
    browser.views = desktop.views
    runtime = BotRuntime(
        store, "owner", lambda _: client, browser_computers=browser, desktop_computers=desktop
    )
    try:
        runtime.run(task, runtime.task_token(task["id"]))
    finally:
        desktop.close()

    # No card waits for a click nobody is there to give, and nothing was done with it.
    assert runtime.proposals.list(bot) == []
    assert any(r.get("state") == "routine_paused" for r in tool_results(client))
    assert store.task("owner", task["id"])["status"] == "completed"
    paused = listed(store, bot, item)
    assert paused["state"] == "paused" and paused["pause_code"] == "access"
    assert paused["pause_reason"].startswith(reason)
    assert paused["access"]["requested"] == expected
    assert (
        paused["access"]["to_allow"]
        == RoutineNeeds.model_validate(paused["access"]["allowed"])
        .union(RoutineNeeds.model_validate(expected))
        .model_dump()
    )
    waiting = [i for i in store.needs_you("owner")["items"] if i["id"] == item["id"]]
    assert waiting == [
        {
            "kind": "routine",
            "id": item["id"],
            "bot_id": bot,
            "bot_name": "Ήρα",
            "bot_color": None,
            "bot_background": None,
            "task_id": None,
            "text": "毎朝の確認",
            "at": paused["updated_at"],
            "routine": "schedule",
            "state": "paused",
            "pause_code": "access",
        }
    ]

    # Allow and run again: the new run gets what was just allowed, and the routine is on.
    again = allow(store, bot, item, turn_on=True, run_request_id="allow-and-run-1")
    run = store.run_schedule_now("owner", bot, item["id"], "allow-and-run-1", max_calls=None)
    assert again["state"] == "active" and again["access"]["requested"] is None
    assert again["access"]["to_allow"] is None
    assert listed(store, bot, item)["pause_code"] is None
    store.claim("owner")
    allowed = store.routine_run_access("owner", run["task_id"])
    assert allowed.covers(RoutineNeeds.model_validate(expected))
    assert not [i for i in store.needs_you("owner")["items"] if i["id"] == item["id"]]


def test_already_allowed_access_needs_no_asking(store, bot):
    item = routine(store, bot, needs={"sites": [SITE]})
    allow(store, bot, item, turn_on=True)
    task = start_run(store, listed(store, bot, item))
    client = ClientFixture(
        [
            response(
                name="propose_action",
                arguments={
                    "question": "OK?",
                    "instructions": "Continue on the allowed site.",
                    "offline_computer": False,
                    "browser_origin": SITE,
                },
            ),
            response("Done."),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, browser_computers=OriginBrowser())
    runtime.run(task, runtime.task_token(task["id"]))
    assert any(r.get("state") == "already_approved" for r in tool_results(client))
    assert listed(store, bot, item)["state"] == "active"
    assert runtime.proposals.list(bot) == []


def test_a_stopped_run_does_not_pause_its_routine(store, bot):
    item = routine(store, bot, needs={"sites": [SITE]})
    allow(store, bot, item, turn_on=True)
    task = start_run(store, listed(store, bot, item))
    store.event("owner", task["id"], "routine_access_needed", {"need": {"sites": [OTHER]}})
    store.cancel("owner", task["id"])
    store.finish("owner", task["id"], "cancelled")
    assert listed(store, bot, item)["state"] == "active"


def test_editing_the_job_or_needs_asks_again_and_timing_does_not(store, bot):
    item = routine(store, bot, needs={"sites": [SITE]})
    allow(store, bot, item)

    def edit(**changes):
        current = listed(store, bot, item)
        data = {
            "name": current["name"],
            "prompt": current["prompt"],
            "timing": current["timing"],
            "expected_revision": current["revision"],
            **changes,
        }
        return store.save_schedule(
            "owner", bot, ScheduleInput.model_validate(data), schedule_id=item["id"]
        )

    # Timing only: what the person allowed stays.
    assert edit(timing={"kind": "interval", "every_minutes": 30})["access"]["allowed"] is not None
    # Needs left out keep the routine's needs.
    assert edit()["needs"]["sites"] == [SITE]
    # A new job asks again, for the same needs.
    changed = edit(prompt="Σύγκρινε τις τιμές με χθες.")
    assert changed["access"]["allowed"] is None
    assert changed["access"]["to_allow"]["sites"] == [SITE]
    allow(store, bot, item)
    # New needs ask again too.
    widened = edit(needs={"sites": [SITE, OTHER]})
    assert widened["access"]["allowed"] is None
    assert widened["access"]["to_allow"]["sites"] == [OTHER, SITE]
    # Needs taken away leave nothing to allow.
    assert edit(needs={})["access"]["to_allow"] is None


def test_revoking_takes_effect_from_the_next_run(store, bot):
    item = routine(store, bot, needs={"sites": [SITE]})
    allow(store, bot, item, turn_on=True)
    first = start_run(store, listed(store, bot, item))
    revoked = store.revoke_routine_access("owner", bot, item["id"])
    assert revoked["access"]["allowed"] is None
    assert revoked["access"]["to_allow"]["sites"] == [SITE]
    # The run under way keeps what it started with.
    assert store.routine_run_access("owner", first["id"]).sites == [SITE]
    store.finish("owner", first["id"], "completed", content="Έτοιμο.")
    # Once it ended it holds nothing, and the next run gets nothing.
    assert store.routine_run_access("owner", first["id"]) is None
    second = start_run(store, listed(store, bot, item))
    assert store.routine_run_access("owner", second["id"]) is None


def test_deleting_a_routine_or_its_owl_deletes_its_grants(store, bot):
    item = routine(store, bot, needs={"offline_computer": True})
    allow(store, bot, item)
    other = routine(store, bot, needs={"sites": [SITE]}, name="Άλλη")
    allow(store, bot, other)
    store.delete_schedule("owner", bot, item["id"])
    with store.connect() as db:
        rows = [r[0] for r in db.execute("SELECT schedule_id FROM routine_access").fetchall()]
    assert rows == [other["id"]]
    store.delete_bot("owner", bot)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM routine_access").fetchone()[0] == 0


def test_peer_turns_still_cannot_use_the_computer_or_browser(store, bot, monkeypatch):
    item = routine(store, bot, needs={"sites": [SITE]})
    allow(store, bot, item, turn_on=True)
    task = start_run(store, listed(store, bot, item))
    browser = OriginBrowser()
    client = ClientFixture([response("Report.")])
    runtime = BotRuntime(store, "owner", lambda _: client, browser_computers=browser)
    monkeypatch.setattr(runtime.peers, "for_task", lambda _: {"status": "running"})
    runtime.run(task, runtime.task_token(task["id"]))
    assert browser.eligibility == [(task["id"], False)]
    assert browser.calls == []


def test_a_chat_turn_gets_nothing_from_a_routine_grant(store, bot):
    item = routine(store, bot, needs={"sites": [SITE]})
    allow(store, bot, item)
    chat = store.create_chat("owner", bot, "Συζήτηση")
    task = store.enqueue("owner", chat["id"], TaskInput(message="Γεια", request_id="chat-turn"))
    store.claim("owner")
    assert store.routine_run_access("owner", task["id"]) is None


def test_the_owl_declares_needs_when_it_sets_up_a_routine(store, bot):
    chat = store.create_chat("owner", bot, "Συζήτηση")
    task = store.enqueue(
        "owner",
        chat["id"],
        TaskInput(message="Κάθε πρωί έλεγξε τις τιμές στο κατάστημα.", request_id="ask-routine"),
    )
    task = store.claim("owner")
    runtime = BotRuntime(store, "owner", lambda _: None, browser_computers=OriginBrowser())
    arguments = {
        "name": "Τιμές",
        "prompt": "Έλεγξε τις τιμές στο κατάστημα.",
        "timing": {"kind": "interval", "every_minutes": 60},
        "needs": {"sites": [SITE]},
    }
    result = runtime._tool(task, "create_schedule", arguments, ["create_schedule"])
    # Saved ready to turn on, with what it needs to be allowed.
    assert result["state"] == "draft"
    assert result["needs"]["sites"] == [SITE]
    assert result["access"]["to_allow"]["sites"] == [SITE]
    assert "allow what its runs use" in result["message"]
    # Needs this service can't provide are refused, not saved.
    with pytest.raises(Conflict, match="private computer is unavailable"):
        runtime._tool(
            task,
            "propose_schedule",
            {**arguments, "name": "Υπολογισμοί", "needs": {"offline_computer": True}},
            ["propose_schedule"],
        )
    # A page path is not a website origin.
    with pytest.raises(ValueError):
        RoutineNeeds(sites=[SITE + "/basket"])


def test_routine_access_routes(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "routes.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    owner = settings.owner
    store = app.state.store
    bot = store.save_bot(owner, BotInput(name="Ήρα", purpose="Routes"))["id"]
    item = store.save_schedule(
        owner,
        bot,
        ScheduleInput.model_validate(
            {
                "name": "毎朝",
                "prompt": "Έλεγξε τις τιμές.",
                "timing": {"kind": "interval", "every_minutes": 60},
                "needs": {"sites": [SITE]},
                "enabled": False,
            }
        ),
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "routine_access" in client.get("/v1/status").json()["capabilities"]
        base = f"/v1/bots/{bot}/schedules/{item['id']}"
        listed = client.get(f"/v1/bots/{bot}/schedules").json()[0]
        decision = {"expected_revision": listed["revision"], "allow": listed["access"]["to_allow"]}
        assert client.put(base + "/access", json={**decision, "extra": 1}).status_code == 422
        stale = {**decision, "allow": {"sites": [OTHER]}}
        assert client.put(base + "/access", json=stale).status_code == 409
        allowed = client.put(
            base + "/access",
            json={**decision, "turn_on": True, "run_request_id": "allow-and-run-route"},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["state"] == "active"
        runs = client.post(base + "/runs", json={}).json()["runs"]
        assert runs[0]["origin"] == "manual"
        assert runs[0]["config"]["access"]["sites"] == [SITE]
        revoked = client.delete(base + "/access")
        assert revoked.status_code == 200
        assert revoked.json()["access"]["allowed"] is None


def test_a_run_on_its_computer_that_needs_a_new_site_pauses_too(store, bot):
    """Work on the owl's computer continues off the reply loop; it can still say what it needs."""
    item = routine(store, bot, needs={"offline_computer": True})
    allow(store, bot, item, turn_on=True)
    task = start_run(store, listed(store, bot, item))
    desktop = DesktopComputers("owner", ComputerViews(), factory=Executor)
    browser = OriginBrowser()
    browser.views = desktop.views
    client = ClientFixture(
        [
            response(
                name="propose_action",
                arguments={
                    "question": "Να ανοίξω τον ιστότοπο;",
                    "instructions": "Κατέβασε τον τιμοκατάλογο.",
                    "offline_computer": True,
                    "browser_origin": SITE,
                },
            ),
            response("Χρειάζομαι άδεια για τον ιστότοπο."),
        ]
    )
    runtime = BotRuntime(
        store, "owner", lambda _: client, browser_computers=browser, desktop_computers=desktop
    )
    try:
        runtime.run(task, runtime.task_token(task["id"]))
        queued = runtime.background.claim()
        assert queued is not None and queued["id"] == task["id"]
        runtime.run(queued, runtime.task_token(queued["id"]), background=True)
    finally:
        desktop.close()
    # Nobody is there to answer a question either.
    assert "ask_clarification" not in advertised_names(client.requests[0])
    assert "propose_action" in advertised_names(client.requests[0])
    assert runtime.proposals.list(bot) == []
    paused = listed(store, bot, item)
    assert paused["state"] == "paused" and paused["pause_code"] == "access"
    # Only what is new is asked for: the computer was allowed already.
    assert paused["access"]["requested"] == {
        "offline_computer": False,
        "online_browser": True,
        "sites": [SITE],
    }
    assert paused["pause_reason"].startswith("Needs approval for a new site: " + SITE)
