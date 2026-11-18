"""An owl that needs the person on its computer waits and reloads once, then asks with one
card whose kind comes from its own tool call, and carries on after "I've done it"."""

import json
import threading
from uuid import uuid4

import pytest
from pydantic import ValidationError

from alysis_code.bots.computer import ComputerControlPause, ComputerViews
from alysis_code.bots.computer_control import ComputerControls, ControlHandoff, ControlRequest
from alysis_code.bots.computer_help import HELP_KINDS, ComputerHelp, help_site
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.store import Store

GREEK_HOST = "παράδειγμα.δοκιμή".encode("idna").decode()


class Token:
    def __init__(self):
        self.waits = []
        self.cancelled = threading.Event()

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.cancelled.is_set()

    def throw_if_cancelled(self):
        if self.cancelled.is_set():
            raise RuntimeError("cancelled")


class Browsers:
    def __init__(self, url):
        self.url, self.calls = url, []

    def page_url(self, task):
        return self.url

    def call(self, task, name, arguments, token, **options):
        self.calls.append((name, arguments))
        return {"source_id": "fresh", "url": arguments.get("url")}


class Runtime:
    def __init__(self, store, browsers, controls):
        self.store, self.owner = store, "owner"
        self.browser_computers, self.computer_controls = browsers, controls


def running_task(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Κουκουβάγια"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue("owner", chat["id"], TaskInput(message="Book it", request_id=uuid4().hex))
    return store, store.claim("owner")


def controls_with_computer(store, task):
    views = ComputerViews()
    session = views.bind(
        "owner",
        task["bot_id"],
        capture=lambda: b"png",
        alive=lambda: True,
        surface="browser",
        human_input=lambda event: {"dispatched": True},
        checkpoint=lambda session: None,
    )
    views.claim("owner", task["bot_id"], task["id"], "browser")
    return ComputerControls(store, "owner", views), session


def events(store, task, kind):
    with store.connect() as db:
        return [
            json.loads(row["data"])
            for row in db.execute(
                "SELECT data FROM events WHERE task_id=? AND kind=? ORDER BY seq",
                (task["id"], kind),
            )
        ]


def ask(help, task, kind, token):
    return help.request(task, ControlHandoff(kind=kind, reason="Πρέπει να συνδεθείς"), token)


def test_the_kind_is_a_closed_choice_of_the_tool_call():
    assert HELP_KINDS == ("human_check", "code", "sign_in", "permission", "other")
    schema = ControlHandoff.model_json_schema()
    assert schema["properties"]["kind"]["enum"] == list(HELP_KINDS)
    assert set(schema["required"]) == {"kind", "reason"}
    with pytest.raises(ValidationError):
        ControlHandoff(kind="captcha", reason="Solve it")
    with pytest.raises(ValidationError):
        ControlHandoff(reason="Help")


@pytest.mark.parametrize("kind", ["human_check", "sign_in", "permission"])
def test_waits_and_reloads_once_then_asks_with_one_card(tmp_path, kind):
    store, task = running_task(tmp_path)
    controls, _ = controls_with_computer(store, task)
    browsers = Browsers(f"https://{GREEK_HOST}/σύνδεση?next=1#form")
    help = ComputerHelp(Runtime(store, browsers, controls), wait_seconds=8)
    token = Token()
    first = ask(help, task, kind, token)
    assert first["status"] == "waited" and first["reloaded"] is True
    assert token.waits == [8]
    # The same page again, as the browser had it, without its in-page fragment.
    assert browsers.calls == [("browser_navigate", {"url": f"https://{GREEK_HOST}/σύνδεση?next=1"})]
    assert first["page"]["source_id"] == "fresh"
    assert events(store, task, "computer_control_requested") == []
    with pytest.raises(ComputerControlPause):
        ask(help, task, kind, token)
    assert token.waits == [8] and len(browsers.calls) == 1
    (requested,) = events(store, task, "computer_control_requested")
    assert requested == {
        "message": "Πρέπει να συνδεθείς",
        "help": {"kind": kind, "site": GREEK_HOST, "site_name": "παράδειγμα.δοκιμή"},
    }


def test_a_code_page_is_waited_on_but_never_reloaded(tmp_path):
    store, task = running_task(tmp_path)
    controls, _ = controls_with_computer(store, task)
    browsers = Browsers("https://bank.example/verify")
    help = ComputerHelp(Runtime(store, browsers, controls), wait_seconds=5)
    token = Token()
    first = ask(help, task, "code", token)
    assert first["status"] == "waited" and first["reloaded"] is False
    assert token.waits == [5] and browsers.calls == []
    with pytest.raises(ComputerControlPause):
        ask(help, task, "code", token)
    assert events(store, task, "computer_control_requested")[0]["help"]["kind"] == "code"


def test_another_human_step_asks_at_once(tmp_path):
    store, task = running_task(tmp_path)
    controls, _ = controls_with_computer(store, task)
    help = ComputerHelp(Runtime(store, Browsers(None), controls))
    token = Token()
    with pytest.raises(ComputerControlPause):
        ask(help, task, "other", token)
    assert token.waits == []
    assert events(store, task, "computer_control_requested")[0]["help"] == {"kind": "other"}


def test_a_new_kind_or_site_waits_again_but_a_restart_does_not(tmp_path):
    store, task = running_task(tmp_path)
    controls, _ = controls_with_computer(store, task)
    browsers = Browsers("https://one.example/")
    help = ComputerHelp(Runtime(store, browsers, controls), wait_seconds=1)
    token = Token()
    assert ask(help, task, "human_check", token)["status"] == "waited"
    browsers.url = "https://two.example/"
    assert ask(help, task, "human_check", token)["status"] == "waited"
    assert ask(help, task, "sign_in", token)["status"] == "waited"
    # A restarted service reads what was already tried from the task's own record.
    again = ComputerHelp(Runtime(store, browsers, controls), wait_seconds=1)
    with pytest.raises(ComputerControlPause):
        ask(again, task, "sign_in", token)


def test_a_stop_during_the_wait_stops_without_asking(tmp_path):
    store, task = running_task(tmp_path)
    controls, _ = controls_with_computer(store, task)
    browsers = Browsers("https://one.example/")
    help = ComputerHelp(Runtime(store, browsers, controls))
    token = Token()
    token.cancelled.set()
    with pytest.raises(RuntimeError, match="cancelled"):
        ask(help, task, "human_check", token)
    assert browsers.calls == [] and events(store, task, "computer_control_requested") == []


def test_help_site_comes_from_web_addresses_only():
    assert help_site("about:blank") is None
    assert help_site("file:///etc/passwd") is None
    assert help_site(None) is None
    assert help_site("https://Accounts.Example.com:8443/x") == {
        "site": "accounts.example.com",
        "site_name": "accounts.example.com",
    }


def test_needs_you_says_what_was_asked_and_ive_done_it_lets_the_owl_carry_on(tmp_path):
    store, task = running_task(tmp_path)
    controls, session = controls_with_computer(store, task)
    help = ComputerHelp(Runtime(store, Browsers("https://shop.example/login"), controls))
    token = Token()
    ask(help, task, "sign_in", token)
    with pytest.raises(ComputerControlPause):
        ask(help, task, "sign_in", token)
    controls.park(task, [{"role": "user", "content": "Book it"}])
    (item,) = store.needs_you("owner")["items"]
    assert item["kind"] == "computer_control"
    assert item["help"] == {"kind": "sign_in", "site": "shop.example", "site_name": "shop.example"}
    assert item["text"] == "Πρέπει να συνδεθείς"
    # "I've done it" gives control back from the card, from a window that never held control.
    done = ControlRequest(action="resume", client_id=uuid4().hex, session_id=session)
    assert controls.begin_return(task["bot_id"], done)["state"] == "returning"
    assert controls.complete_return(task["bot_id"], done)["state"] == "agent"
    with store.connect() as db:
        turn = db.execute(
            "SELECT state,messages FROM background_turns WHERE task_id=?", (task["id"],)
        ).fetchone()
    assert turn["state"] == "queued"
    assert json.loads(turn["messages"])[-1]["_bot_control_return"] == task["id"]
    assert store.needs_you("owner")["items"] == []
