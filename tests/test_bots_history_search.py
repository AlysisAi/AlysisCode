"""Searching an owl's messages works the same in every language and script."""

from __future__ import annotations

from uuid import uuid4

import pytest

from alysis_code.bots.models import BotInput, HistoryQuery, TaskInput
from alysis_code.bots.store import Store
from alysis_code.bots.text_search import excerpt, loose_fold


@pytest.fixture
def owl(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Έλενα"))
    chat = store.create_chat("alice", bot["id"], "Συζήτηση")

    def say(text):
        task = store.enqueue(
            "alice", chat["id"], TaskInput(message=text, request_id="req_" + uuid4().hex)
        )
        store.claim("alice")
        store.finish("alice", task["id"], "completed", content="OK")
        return task

    return store, bot, say


def search(store, bot, query, **options):
    return store.search_history(
        "alice", bot["id"], HistoryQuery(query=query, **options), excerpts=True
    )


def found(result):
    return [item["preview"] for item in result["messages"]]


def test_case_and_normal_form_never_hide_a_match(owl):
    store, bot, say = owl
    say("Καλημέρα Κόσμε από την Αθήνα")
    say("Café au lait")  # written with a separate accent
    result = search(store, bot, "ΚΑΛΗΜΈΡΑ κόσμε")
    assert found(result) == ["Καλημέρα Κόσμε από την Αθήνα"] and result["match"] == "exact"
    assert result["messages"][0]["excerpt"] == {
        "before": "",
        "match": "Καλημέρα Κόσμε",
        "after": " από την Αθήνα",
    }
    assert found(search(store, bot, "CAFÉ")) == ["Café au lait"]


def test_without_an_exact_match_accents_and_spacing_are_ignored(owl):
    store, bot, say = owl
    say("Το ραντεβού είναι   στις\nπέντε")
    result = search(store, bot, "ραντεβου ειναι στις πεντε")
    assert result["match"] == "loose"
    assert result["messages"][0]["excerpt"]["match"] == "ραντεβού είναι   στις\nπέντε"
    assert search(store, bot, "δεν υπάρχει")["messages"] == []


def test_an_exact_match_comes_before_one_without_accents(owl):
    store, bot, say = owl
    say("Un café, s'il vous plaît")
    say("A cafe on the corner")
    result = search(store, bot, "cafe")
    assert found(result) == ["A cafe on the corner"] and result["match"] == "exact"


def test_japanese_matches_in_full_and_half_width(owl):
    store, bot, say = owl
    say("昨日は東京タワーに行きました。とても楽しかったです。")
    exact = search(store, bot, "タワー")
    assert exact["match"] == "exact"
    assert exact["messages"][0]["excerpt"] == {
        "before": "昨日は東京",
        "match": "タワー",
        "after": "に行きました。とても楽しかったです。",
    }
    half = search(store, bot, "ﾀﾜｰ")
    assert half["match"] == "loose"
    assert half["messages"][0]["excerpt"]["match"] == "タワー"


def test_loose_results_page_like_exact_ones(owl):
    store, bot, say = owl
    for number in range(3):
        say(f"Σημείωση {number}: ελέγχω το πρόγραμμα")
    first = search(store, bot, "ελεγχω", limit=2)
    assert first["match"] == "loose" and len(first["messages"]) == 2
    rest = search(store, bot, "ελεγχω", limit=2, before=first["next_before"])
    assert rest["match"] == "loose" and len(rest["messages"]) == 1
    assert rest["next_before"] is None
    assert [m["preview"][:12] for m in first["messages"] + rest["messages"]] == [
        "Σημείωση 2: ",
        "Σημείωση 1: ",
        "Σημείωση 0: ",
    ]


def test_the_last_page_of_exact_results_stays_exact(owl):
    store, bot, say = owl
    say("ελεγχω χωρίς τόνο")
    say("ελέγχω πρώτα")
    say("ελέγχω ξανά")
    first = search(store, bot, "ελέγχω", limit=2)
    assert first["match"] == "exact" and first["next_before"] is None
    # Past the last exact match lies only a message without accents: it isn't mixed in.
    past = search(store, bot, "ελέγχω", limit=2, before=first["messages"][-1]["position"])
    assert past == {"messages": [], "next_before": None, "match": "exact"}


def test_the_owls_own_search_reads_previews_without_excerpts(owl):
    store, bot, say = owl
    say("Ορόσημα του έργου")
    result = store.search_history("alice", bot["id"], HistoryQuery(query="ορόσημα"))
    assert "excerpt" not in result["messages"][0]
    assert result["messages"][0]["preview"] == "Ορόσημα του έργου"


def test_excerpts_cut_around_the_match_on_whole_characters():
    text = "α" * 100 + " Ο Γιώργος έφυγε " + "β" * 300
    part = excerpt(text, "γιωργος", loose=True)
    assert part["match"] == "Γιώργος"
    assert part["before"].startswith("…") and part["before"].endswith(" ")
    assert part["after"].endswith("…")
    assert len(part["before"]) <= 62 and len(part["after"]) <= 143
    decomposed = "é" * 3 + " x"
    assert excerpt(decomposed, "x", loose=False)["before"] == "é" * 3 + " "
    assert excerpt("no match here", "zzz", loose=False) == {
        "before": "",
        "match": "",
        "after": "no match here",
    }


@pytest.mark.parametrize(
    "text, folded",
    [
        ("Καλημέρα ΚΌΣΜΕ", "καλημερα κοσμε"),
        ("ガギグ　ｶﾀｶﾅ", "カキク カタカナ"),
        ("Straße ﬁle İstanbul", "strasse file istanbul"),
        ("  many\t\nspaces  ", "many spaces"),
        ("مَرْحَبًا", "مرحبا"),
    ],
)
def test_loose_folding_drops_marks_forms_and_extra_spacing(text, folded):
    assert loose_fold(text) == folded
