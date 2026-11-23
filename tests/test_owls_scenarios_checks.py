"""The scenario tier's checks are pure functions on frozen evidence; here on hand-written
evidence, each passing and failing, with texts in Greek, Chinese and Japanese."""

from __future__ import annotations

import pytest

from scripts.qa.owls_scenarios import checks
from scripts.qa.owls_scenarios.checks import evaluate, matches, problems
from scripts.qa.owls_scenarios.scenario import problems as scenario_problems

EVIDENCE = {
    "turns": [
        {
            "ref": "ask",
            "task_id": "t1",
            "status": "completed",
            "error": None,
            "failure": None,
            "output": "Έτοιμη η λίστα.",
            "events": [
                {"kind": "tool_started", "data": {"name": "save_file"}},
                {"kind": "progress_note", "data": {"text": "Γράφω τη λίστα"}},
                {"kind": "routine_refreshed", "data": {"response_discarded": True}},
            ],
            "cards": {"action_reviews": [{"effect": "pay", "status": "pending"}]},
        },
        {
            "ref": "more",
            "task_id": "t2",
            "status": "failed",
            "error": "The model reached its response limit.",
            "failure": {"kind": "model"},
            "output": None,
            "events": [],
            "cards": {},
        },
    ],
    "replies": [{"task_id": "t1", "turn": "ask", "text": "Έτοιμη η λίστα."}],
    "tool_runs": [
        {"task_id": "t1", "name": "save_file", "arguments": {"name": "λίστα.txt", "content": "水"}},
        {"task_id": "t1", "name": "list_files", "arguments": {}},
        {"task_id": "t2", "name": "read_memory", "arguments": {"memory_id": "記憶"}},
    ],
    "requested_calls": [{"owl_call": 1, "name": "save_file", "arguments": {}}],
    "model_calls": [
        {
            "channel": "owl",
            "number": 1,
            "tools": ["save_file", "list_files"],
            "messages": [
                {"role": "system", "content": 'Routines note: {"state":"active"}'},
                {"role": "user", "content": "Φτιάξε λίστα"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "save_file", "arguments": '{"name": "λίστα.txt"}'}}
                    ],
                },
                {"role": "tool", "content": '{"page_view": "- button \\"支付\\" [ref=e4]"}'},
            ],
        },
        {"channel": "reply_checker", "number": 1, "tools": [], "messages": []},
    ],
    "routines": [{"name": "Πρωινά", "state": "active", "nothing_new": False}],
    "browser": {"inputs": []},
}


def verdict(check):
    [result] = evaluate(EVIDENCE, [check])
    return result["passed"], result["detail"]


@pytest.mark.parametrize(
    "expected, actual, result",
    [
        ({"a": 1}, {"a": 1, "b": 2}, True),
        ({"a": {"b": "β"}}, {"a": {"b": "β", "c": 3}}, True),
        ({"a": 1}, {"a": 2}, False),
        ([{"a": 1}], [{"a": 1, "b": 2}], True),
        ([{"a": 1}], [{"a": 1}, {"a": 1}], False),
        ({"$contains": "λίστ"}, "Η λίστα", True),
        ({"$contains": "清单"}, ["购物清单", "x"], True),
        ({"$contains": "x"}, 5, False),
        ({"a": None}, {"b": 1}, True),
        ({"a": None}, {"a": 1}, False),
        ({"a": {"$exists": True}}, {"a": 0}, True),
        ({"a": {"$exists": False}}, {"b": 0}, True),
        ("text", "text", True),
    ],
)
def test_subset_matching(expected, actual, result):
    assert matches(expected, actual) is result


def test_turn():
    assert verdict({"kind": "turn", "turn": "ask", "status": "completed"})[0]
    assert verdict({"kind": "turn", "turn": "turn:2", "status": "failed", "failure_kind": "model"})[
        0
    ]
    assert verdict({"kind": "turn", "turn": "more", "error_contains": "response limit"})[0]
    passed, detail = verdict({"kind": "turn", "turn": "more", "status": "completed"})
    assert not passed and "'failed'" in detail
    assert not verdict({"kind": "turn", "turn": "nobody"})[0]


def test_reply_reads_published_replies_and_everything_shown():
    assert verdict({"kind": "reply", "turn": "ask", "equals": "Έτοιμη η λίστα."})[0]
    assert verdict({"kind": "reply", "turn": "ask", "contains": ["λίστα"]})[0]
    assert verdict({"kind": "reply", "turn": "more", "published": False})[0]
    assert not verdict({"kind": "reply", "turn": "more", "published": True})[0]
    # A progress line is shown to the person, so excludes reads it too.
    passed, detail = verdict({"kind": "reply", "turn": "ask", "excludes": ["Γράφω"]})
    assert not passed and "Γράφω" in detail
    assert verdict({"kind": "reply", "excludes": ["πρόχειρη"]})[0]


def test_tools_in_order_with_arguments():
    expect = [{"name": "save_file", "arguments": {"name": "λίστα.txt"}}, {"name": "list_files"}]
    assert verdict({"kind": "tools", "turn": "ask", "order": "exact", "expect": expect})[0]
    assert verdict({"kind": "tools", "expect": [{"name": "read_memory"}]})[0]
    assert verdict({"kind": "tools", "order": "prefix", "expect": expect})[0]
    # Out of order, wrong arguments, or not exactly these.
    assert not verdict({"kind": "tools", "expect": list(reversed(expect))})[0]
    wrong = [{"name": "save_file", "arguments": {"name": "清单.txt"}}]
    assert not verdict({"kind": "tools", "expect": wrong})[0]
    assert not verdict({"kind": "tools", "order": "exact", "expect": expect})[0]
    asked = {
        "kind": "tools",
        "source": "requested",
        "order": "exact",
        "expect": [{"name": "save_file"}],
    }
    assert verdict(asked)[0]


def test_tool_not_run():
    assert verdict({"kind": "tool_not_run", "name": "browser_click"})[0]
    assert verdict({"kind": "tool_not_run", "turn": "more", "name": "save_file"})[0]
    passed, detail = verdict({"kind": "tool_not_run", "name": "save_file"})
    assert not passed and "ran 1 time" in detail


def test_card():
    assert verdict({"kind": "card", "card": "action_review", "fields": {"effect": "pay"}})[0]
    assert verdict({"kind": "card", "card": "action_review", "equals": 1})[0]
    assert not verdict({"kind": "card", "card": "action_review", "fields": {"effect": "send"}})[0]
    assert verdict({"kind": "card", "card": "question", "present": False})[0]
    assert not verdict({"kind": "card", "card": "action_review", "turn": "more", "min": 1})[0]


def test_event():
    assert verdict(
        {"kind": "event", "event": "routine_refreshed", "data": {"response_discarded": True}}
    )[0]
    assert verdict({"kind": "event", "turn": "ask", "event": "tool_started", "equals": 1})[0]
    assert verdict({"kind": "event", "turn": "more", "event": "tool_started", "present": False})[0]
    assert not verdict({"kind": "event", "event": "output_cut_off"})[0]
    assert not verdict({"kind": "event", "event": "tool_started", "max": 0})[0]


def test_count():
    assert verdict(
        {"kind": "count", "of": "tool_runs", "where": {"name": "save_file"}, "equals": 1}
    )[0]
    assert verdict({"kind": "count", "of": "model_calls", "where": {"channel": "owl"}, "min": 1})[0]
    assert not verdict({"kind": "count", "of": "replies", "max": 0})[0]
    assert verdict({"kind": "count", "of": "turns", "equals": 2})[0]


def test_request_reads_messages_tool_results_and_arguments():
    base = {"kind": "request", "owl_call": 1}
    assert verdict({**base, "contains": ['"state":"active"', "Φτιάξε"]})[0]
    # Text inside a tool result's JSON strings and a call's arguments, as written.
    assert verdict({**base, "contains": ['- button "支付" [ref=e4]', "λίστα.txt"]})[0]
    assert not verdict({**base, "excludes": ["支付"]})[0]
    assert verdict({**base, "tail": [{"role": "assistant"}, {"role": "tool"}]})[0]
    assert not verdict({**base, "tail": [{"role": "user"}]})[0]
    assert verdict({**base, "offered": ["save_file"], "not_offered": ["browser_click"]})[0]
    assert not verdict({**base, "offered": ["browser_click"]})[0]
    assert not verdict({"kind": "request", "owl_call": 2})[0]  # only one owl call


def test_routine_and_path():
    assert verdict({"kind": "routine", "name": "Πρωινά", "state": "active"})[0]
    assert not verdict({"kind": "routine", "state": "paused"})[0]
    assert verdict({"kind": "path", "path": "browser.inputs", "equals": []})[0]
    assert verdict({"kind": "path", "path": "turns.1.failure", "matches": {"kind": "model"}})[0]
    assert verdict({"kind": "path", "path": "routines.0.state", "equals": "active"})[0]
    assert not verdict({"kind": "path", "path": "turns.0.status", "equals": "failed"})[0]


def test_malformed_checks_say_what_is_wrong():
    assert problems({"kind": "nope"})[0].startswith("Unknown check kind 'nope'")
    assert problems({"kind": "tools"}) == ["tools check needs 'expect'."]
    assert "Unknown card" in problems({"kind": "card", "card": "postcard"})[0]
    assert "Unknown count" in problems({"kind": "count", "of": "owls"})[0]
    [result] = evaluate(EVIDENCE, [{"kind": "event"}])
    assert not result["passed"] and "needs 'event'" in result["detail"]
    assert set(checks.CHECKS) >= {"tools", "tool_not_run", "reply", "card", "event", "count"}


def test_malformed_scenarios_say_what_is_wrong():
    found = scenario_problems(
        {
            "id": "x",
            "pins": "p",
            "extra": 1,
            "script": {
                "owl": [{"say": "a", "empty": True}, {"call": [{}]}],
                "oracle": [],
                "reply_checker": "always",
            },
            "steps": [
                {"do": "send"},
                {"do": "run", "during": [{"do": "steer", "message": "m"}]},
                {"do": "mark_read", "during": [{"do": "send", "owl_call": 1, "message": "m"}]},
            ],
            "checks": [{"kind": "reply"}],
        }
    )
    assert "Unknown key 'extra'." in found
    assert any("Unknown script channel 'oracle'" in p for p in found)
    assert any("script.owl[1]: one of" in p for p in found)
    assert any("script.owl[2]: each call names its tool." in p for p in found)
    assert 'script.reply_checker is a list of replies or "ready".' in found
    assert "steps[1]: send needs 'message'." in found
    assert "steps[2]: each during action has owl_call or tool." in found
    assert "steps[3]: only a run step has during." in found
