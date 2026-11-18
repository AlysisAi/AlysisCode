"""The look a person picks for an owl's face: kept with the owl, a label like its name."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest
from pydantic import ValidationError

from alysis_code.bots.models import BotCreateInput, BotInput, TaskInput
from alysis_code.bots.store import Store


def test_a_picked_colour_is_kept_and_listed_and_older_owls_have_none(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    picked = store.save_bot(
        "alice", BotCreateInput(name="Νεφέλη", color="yellow", background="stars")
    )
    unpicked = store.save_bot("alice", BotInput(name="花子"))
    assert (picked["color"], picked["background"]) == ("yellow", "stars")
    assert (unpicked["color"], unpicked["background"]) == (None, None)
    assert {bot["name"]: (bot["color"], bot["background"]) for bot in store.list_bots("alice")} == {
        "Νεφέλη": ("yellow", "stars"),
        "花子": (None, None),
    }


def test_a_change_without_a_colour_keeps_it_and_null_clears_it(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Νεφέλη", color="sky", background="ocean"))
    # An app from before looks sends the owl without one: renaming keeps the face.
    renamed = store.save_bot("alice", BotInput(name="Σύννεφο"), bot["id"])
    assert (renamed["name"], renamed["color"], renamed["background"]) == (
        "Σύννεφο",
        "sky",
        "ocean",
    )
    # Each part changes on its own; the other stays.
    changed = store.save_bot(
        "alice", BotInput(name="Σύννεφο", purpose="Νέα", color="red"), bot["id"]
    )
    assert (changed["purpose"], changed["color"], changed["background"]) == ("Νέα", "red", "ocean")
    cleared = store.save_bot("alice", BotInput(name="Σύννεφο", background=None), bot["id"])
    assert (cleared["color"], cleared["background"]) == ("red", None)


def test_a_colour_changes_at_once_even_while_the_owl_works(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Νεφέλη", purpose="Έρευνα"))
    chat = store.create_chat("alice", bot["id"], "Νεφέλη")
    store.enqueue("alice", chat["id"], TaskInput(message="Ψάξε", request_id="req_" + uuid4().hex))
    assert store.claim("alice")
    # Like a new name, a colour is only a label: the running work and its setup are untouched.
    recoloured = store.save_bot(
        "alice", BotInput(name="Νεφέλη", purpose="Έρευνα", color="pink"), bot["id"]
    )
    assert recoloured["color"] == "pink"
    assert recoloured["updated_at"] == bot["updated_at"]


@pytest.mark.parametrize("value", ["Red", "red!", "κόκκινο", "", "-red", "x" * 25, "#ff0000"])
@pytest.mark.parametrize("part", ["color", "background"])
def test_a_look_is_a_short_plain_name(part, value):
    with pytest.raises(ValidationError):
        BotInput(name="Νεφέλη", **{part: value})


def test_any_short_name_the_app_may_add_later_is_kept(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot(
        "alice", BotInput(name="Νεφέλη", color="sea-green2", background="aurora-2")
    )
    saved = store.bot("alice", bot["id"])
    assert (saved["color"], saved["background"]) == ("sea-green2", "aurora-2")


def test_needs_you_items_carry_the_owls_colour(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    picked = store.save_bot("alice", BotInput(name="Νεφέλη", color="purple", background="checker"))
    unpicked = store.save_bot("alice", BotInput(name="花子"))
    for bot in (picked, unpicked):
        chat = store.create_chat("alice", bot["id"], bot["name"])
        task = store.enqueue(
            "alice", chat["id"], TaskInput(message="Συνδέσου", request_id="req_" + uuid4().hex)
        )
        assert store.claim("alice")["id"] == task["id"]
        with store.connect(write=True) as db:
            db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (task["id"],))
            db.execute(
                "INSERT INTO background_turns VALUES(?,'computer_control','x','[]',0)",
                (task["id"],),
            )
        store.event("alice", task["id"], "computer_control_requested", {"message": "ログイン"})
    looks = {
        item["bot_name"]: (item["bot_color"], item["bot_background"])
        for item in store.needs_you("alice")["items"]
    }
    assert looks == {"Νεφέλη": ("purple", "checker"), "花子": (None, None)}


def test_deleting_an_owl_clears_its_colour(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Νεφέλη", color="orange", background="sunrise"))
    store.delete_bot("alice", bot["id"])
    with store.connect() as db:
        row = db.execute(
            "SELECT name,color,background FROM bots WHERE id=?", (bot["id"],)
        ).fetchone()
    assert (row["name"], row["color"], row["background"]) == ("", None, None)


def test_an_existing_database_gains_the_colour_and_keeps_its_owls(tmp_path):
    path = tmp_path / "bots.sqlite3"
    Store(path).save_bot("alice", BotInput(name="Νεφέλη"))
    # A database written before looks: the columns didn't exist.
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE bots DROP COLUMN color")
        db.execute("ALTER TABLE bots DROP COLUMN background")
        db.execute("PRAGMA user_version=41")
    store = Store(path)
    [bot] = store.list_bots("alice")
    assert (bot["name"], bot["color"], bot["background"]) == ("Νεφέλη", None, None)
    recoloured = store.save_bot(
        "alice", BotInput(name="Νεφέλη", color="green", background="dots"), bot["id"]
    )
    assert (recoloured["color"], recoloured["background"]) == ("green", "dots")
