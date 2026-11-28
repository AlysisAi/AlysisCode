"""An owl knows where its own computer stands while it can't use it (computer_readiness.py).

The desktop app writes the setup state of this device's computer as a typed value; a provider's
typed refusal and a failed start give the same kind of state. The owl gets the state as data, the
same for every language, points at the card on its page and keeps working from the web instead of
stopping.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from alysis_code.bots import computer_readiness as readiness
from alysis_code.bots import web_lookup
from alysis_code.bots.compute import ComputeCreationRefused, ComputeError, ComputeUncertain
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

OWNER = "owner"

# What the app writes for each setup state (owlsComputerDownload.ts setupOf), and what the owl is
# told it means.
APP_SETUPS = [
    ({"schema": 1, "state": "downloading", "percent": 42}, "being downloaded"),
    ({"schema": 1, "state": "downloading"}, "being downloaded"),
    ({"schema": 1, "state": "unpacking", "percent": 90}, "being unpacked"),
    ({"schema": 1, "state": "download_paused", "percent": 50}, "Continue on the card"),
    ({"schema": 1, "state": "waiting_for_windows_approval"}, "Windows asks once"),
    ({"schema": 1, "state": "restart_needed"}, "restart once"),
    ({"schema": 1, "state": "virtualization_off"}, "virtualization is turned on"),
    ({"schema": 1, "state": "not_enough_disk_space"}, "free disk space"),
    ({"schema": 1, "state": "setup_failed"}, "Try again"),
    ({"schema": 1, "state": "not_set_up"}, "not set up yet"),
    ({"schema": 1, "state": "not_supported_here"}, "Alysis Cloud"),
    ({"schema": 1, "state": "ready"}, "no owl on this computer is working"),
]


def write_setup(folder: Path, value) -> Path:
    path = folder / readiness.SETUP_FILE
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.parametrize(("written", "meaning"), APP_SETUPS)
def test_each_setup_state_the_app_writes_is_told_to_the_owl_as_data(tmp_path, written, meaning):
    state = readiness.read_setup(write_setup(tmp_path, written))
    assert state == readiness.Readiness(written["state"], written.get("percent"))
    told = readiness.task_state(state)
    # The state as data, its meaning in plain words, what works once ready, and where to look.
    data = {key: value for key, value in written.items() if key != "schema"}
    assert json.dumps(data, separators=(",", ":")) in told
    assert meaning in told
    assert "websites it can click and type on" in told
    # The card on the owl's page while it shows something to do; it hides once set up or where no
    # computer can run, so the owl points nowhere then.
    assert ("card on this owl's page" in told) == (
        written["state"] not in ("ready", "not_supported_here")
    )
    assert "Settings" not in told
    if "percent" in written:
        assert f"({written['percent']}% so far)" in told


@pytest.mark.parametrize(
    "written",
    [
        {"schema": 2, "state": "downloading"},
        {"schema": 1, "state": "almost_there"},
        {"schema": 1, "state": "downloading", "percent": 101},
        {"schema": 1, "state": "downloading", "percent": "42"},
        {"schema": 1, "state": "ready", "percent": 10},
        {"schema": 1, "state": "start_failed"},
        {"schema": 1, "state": "downloading", "words": "Λήψη 42%"},
        ["downloading"],
        "{not json",
        "x" * (readiness.MAX_SETUP_BYTES + 1),
    ],
)
def test_anything_else_in_the_file_leaves_the_setup_unknown(tmp_path, written):
    assert readiness.read_setup(write_setup(tmp_path, written)) is None


def test_no_file_leaves_the_setup_unknown(tmp_path):
    assert readiness.read_setup(tmp_path / readiness.SETUP_FILE) is None


@pytest.mark.parametrize(
    ("code", "state"),
    [
        ("windows_computer_service_missing", "waiting_for_windows_approval"),
        ("windows_computer_service_outdated", "waiting_for_windows_approval"),
        ("virtualization_off", "virtualization_off"),
        ("insufficient_local_storage", "not_enough_disk_space"),
        ("local_computer_limit", "all_computers_busy"),
        ("a_code_from_a_newer_provider", "start_failed"),
    ],
)
def test_a_refused_start_gives_the_same_kind_of_state(code, state):
    receipt = ComputeCreationRefused("Δεν ξεκίνησε 東京", code=code).receipt()
    assert receipt["computer"] == {"state": state}
    assert receipt["error"] == "Δεν ξεκίνησε 東京" and receipt["code"] == code
    assert receipt["executed"] is False and receipt["computer_created"] is False
    assert "instead of stopping" in receipt["note"]


def test_this_pcs_service_refusals_point_at_the_card_not_at_settings():
    from alysis_code.bots import local_hcs

    for words in (local_hcs.SERVICE_MISSING, local_hcs.SERVICE_OUTDATED):
        assert "Finish setup on the private computer card on the owl's page" in words
        assert "Settings" not in words


def test_a_failed_start_is_said_plainly_and_an_uncertain_one_keeps_its_own_handling():
    failed = readiness.start_failed(ComputeError("provider detail that is not for people"))
    assert failed["computer"] == {"state": "start_failed"}
    assert "provider detail" not in json.dumps(failed)
    assert readiness.start_failed(ComputeUncertain("Lost creation response")) is None
    assert readiness.start_failed(ValueError("not a provider error")) is None


# ---------------------------------------------------------------------------------------------
# A task while the computer downloads


def response(text="", calls=()):
    return LLMResponse(text, list(calls), {}, usage=LLMUsage(10, 5, 15))


class Model:
    model = "fixture"
    supports_tool_calling = True

    def __init__(self, responses, *, image_input):
        self.responses = iter(responses)
        self.requests = []
        # The model's declared image input, as the service sets it (app.py model_client).
        self._alysis_browser_image_input = image_input

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.responses)


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(web_lookup, "web_tools_available", lambda: True)
    monkeypatch.setattr("alysis_code.bots.runtime.web_tools_available", lambda: True)
    monkeypatch.setattr(
        "alysis_code.bots.runtime.web_search",
        lambda query, limit: {
            "query": query,
            "results": [{"title": "Ξενοδοχεία", "url": "https://hotels.example/nafplio"}],
        },
    )


@pytest.mark.parametrize("image_input", [True, False])
@pytest.mark.parametrize(
    "message",
    ["Βρες μου τρία ξενοδοχεία στο Ναύπλιο για το Σάββατο.", "Find three hotels in Kyoto."],
)
def test_a_task_while_the_computer_downloads_answers_from_the_web(
    tmp_path, web, message, image_input
):
    setup = write_setup(tmp_path, {"schema": 1, "state": "downloading", "percent": 42})
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot(OWNER, BotInput(name="Γλαύκα", purpose="Ταξίδια"))
    chat = store.create_chat(OWNER, bot["id"], "Chat")
    store.enqueue(OWNER, chat["id"], TaskInput(message=message, request_id="during-download"))
    task = store.claim(OWNER)
    model = Model(
        [
            response(calls=[ToolCall("s", "web_search", {"query": message})]),
            response("Found them on hotels.example; my computer is still downloading (42%)."),
        ],
        image_input=image_input,
    )
    runtime = BotRuntime(store, OWNER, lambda _: model, limits=RuntimeLimits(calls_per_task=8))
    runtime.reply_review = None
    runtime.computer_setup = lambda: readiness.read_setup(setup)
    runtime.run(task, EventCancellationToken(threading.Event()))

    final = store.task(OWNER, task["id"])
    assert final["status"] == "completed" and final["output"].startswith("Found them")
    first = model.requests[0]
    offered = {tool["function"]["name"] for tool in first["tools"]}
    # The web works without the computer; nothing of the computer is offered while it downloads.
    assert {"web_search", "web_fetch"} <= offered
    assert not any(name.startswith(("browser_", "computer_")) for name in offered)
    system = "\n".join(m["content"] for m in first["messages"] if m["role"] == "system")
    assert '{"state":"downloading","percent":42}' in system
    assert "card on this owl's page" in system and "Settings > Owls" not in system
    assert "instead of stopping" in system
    # The same state reaches the owl whatever the request's language and the model's inputs.
    assert readiness.task_state(readiness.Readiness("downloading", 42)) in system


def test_an_owl_with_its_computer_is_not_told_about_setup(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot(OWNER, BotInput(name="Mira", purpose="Help"))
    chat = store.create_chat(OWNER, bot["id"], "Chat")
    store.enqueue(OWNER, chat["id"], TaskInput(message="Hi", request_id="with-computer"))
    task = store.claim(OWNER)
    runtime = BotRuntime(store, OWNER, lambda _: None)
    runtime.computer_setup = lambda: readiness.Readiness("ready")
    assert "computer, not connected yet" in runtime._context(task)[1]["content"]
    runtime.desktop_computers = object()
    assert "not connected yet" not in "".join(m["content"] for m in runtime._context(task))
