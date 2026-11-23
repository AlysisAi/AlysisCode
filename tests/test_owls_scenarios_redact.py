"""Turning an exported owl chat into a scenario takes out everything personal, keeps the chat's
shape, and refuses to write when anything original would remain. Synthetic exports only: a
Greek and a Chinese turn with names, e-mail addresses, web addresses and ids."""

from __future__ import annotations

import copy
import json

import pytest

from scripts.qa.owls_scenarios import redact as redaction
from scripts.qa.owls_scenarios.redact import Leak, Redactor, leaks, personal_strings, redact
from scripts.qa.owls_scenarios.run import run_scenario
from scripts.qa.owls_scenarios.scenario import problems

OWL_ID = "8f14e45fceea167a5a36dedd4bea2543"
CHAT_ID = "c9f0f895fb98ab9159f51fd0297e236d"
FIRST = "45c48cce2e2d7fbdea1afc51c7c6ad26"
SECOND = "d3d9446802a44259755d38e6d163e820"
ROUTINE = "6512bd43d9caa6e02c990b0a82652dca"
EMAIL = "anna.papadopoulou@mail.example"
HOST = "booking.hotel-aigaio.example"
SECRET_WORDS = ("Άννα", "Παπαδοπούλου", "Νάξο", "Αιγαίο", "李伟", "li.wei@mail.example", "sk-live")


def event(kind, **data):
    return {"seq": 0, "task_id": FIRST, "kind": kind, "data": data, "created_at": 1.0}


EXPORT = {
    "owl": {"id": OWL_ID, "name": "Γλαύκα της Άννας", "purpose": "Κρατήσεις για την Άννα"},
    "chat": {"id": CHAT_ID, "bot_id": OWL_ID, "title": "Ταξίδι στη Νάξο", "messages": []},
    "tasks": [
        {
            "id": FIRST,
            "bot_id": OWL_ID,
            "chat_id": CHAT_ID,
            "message": f"Βρες ξενοδοχείο στη Νάξο και στείλε την κράτηση στο {EMAIL}",
            "status": "completed",
            "output": f"Βρήκα το Αιγαίο: https://{HOST}/rooms/42?ref=anna. Η Άννα έχει δωμάτιο.",
            "error": None,
            "failure": None,
            "events": [
                event("queued"),
                event("model_started", model="model-a"),
                event("tool_started", name="web_search", topic="ξενοδοχεία Νάξος Άννα"),
                event("tool_completed", name="web_search"),
                event("model_started"),
                event("tool_started", name="browser_navigate", host=HOST),
                event("tool_started", name="save_file", file="Κράτηση Άννας.txt"),
                event("model_started"),
                # Every model call starts with model_started: this one is the reply checker's.
                event("model_started"),
                event("reply_review_started"),
                event(
                    "reply_review_finished",
                    decision="repair",
                    issue="omission",
                    reason=f"Δεν ανέφερε το {EMAIL}",
                ),  # fmt: skip
                event("model_started"),
                event("model_started"),
                event("reply_review_started"),
                event("reply_review_finished", decision="ready", reason="Ταιριάζει."),
                event("completed"),
            ],
            "usage": [{"status": "completed", "model": "model-a"}],
        },
        {
            # A routine's run: the engine started it, so it is not one of the person's turns.
            "id": ROUTINE,
            "bot_id": OWL_ID,
            "message": "",
            "status": "completed",
            "output": "Τίποτα νέο για την Άννα.",
            "events": [event("model_started")],
        },
        {
            "id": SECOND,
            "bot_id": OWL_ID,
            "chat_id": CHAT_ID,
            "message": "预订确认了吗？请发到 li.wei@mail.example，我是李伟。",
            "status": "failed",
            "output": None,
            "error": "Provider rejected key sk-live-1234 at https://api.provider.example/v1",
            "failure": {"kind": "model", "status": 401, "code": "invalid_api_key"},
            "events": [
                event("model_started"),
                event("provider_retry", exception_type="LLMError", wait_seconds=2),
                event("failed", error="Provider rejected key sk-live-1234"),
            ],
        },
    ],
}


def text_of(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def test_an_export_becomes_a_valid_scenario_with_nothing_personal_left():
    scenario = redact(EXPORT, "naxos-hotel-replay")
    assert problems(scenario) == []
    written = text_of(scenario)
    for original in (OWL_ID, CHAT_ID, FIRST, SECOND, EMAIL, HOST, *SECRET_WORDS):
        assert original.casefold() not in written.casefold(), original
    assert "rooms/42" not in written and "ref=anna" not in written
    # The chat's shape survives: two of the person's turns, their tools and verdicts.
    assert [s["do"] for s in scenario["steps"]] == ["send", "run", "send", "run"]
    owl = scenario["script"]["owl"]
    assert [list(r)[0] for r in owl] == ["call", "call", "say", "say", "error", "error"]
    assert [c["tool"] for c in owl[1]["call"]] == ["browser_navigate", "save_file"]
    assert owl[1]["call"][0]["arguments"] == {"url": "https://site-1.example/"}
    assert owl[1]["call"][1]["arguments"] == {"name": "file-1.txt"}
    assert owl[0]["call"][0]["arguments"]["query"].startswith("<text-")
    assert "greek" in owl[0]["call"][0]["arguments"]["query"]
    assert [v["decision"] for v in scenario["script"]["reply_checker"]] == ["repair", "ready"]
    assert scenario["script"]["reply_checker"][0]["issue"] == "omission"
    # The failed turn keeps its provider status and code, not its words.
    assert owl[5]["error"]["status"] == 401 and owl[5]["error"]["code"] == "invalid_api_key"
    assert scenario["owl"]["name"] == "Owl 1" and scenario["owl"]["chat"] == "Chat 1"
    assert "cjk" in scenario["steps"][2]["message"]


def test_placeholders_are_stable():
    redactor = Redactor()
    assert redactor.host("Booking.Shop.EXAMPLE") == redactor.host("booking.shop.example")
    assert redactor.url("https://a.shop.example/x/y") == redactor.url("https://a.shop.example/x/y")
    assert redactor.url("https://a.shop.example/x/z") != redactor.url("https://a.shop.example/x/y")
    first, again = redactor.text("Γεια σου Άννα"), redactor.text("Γεια σου Άννα")
    assert first == again != redactor.text("你好")
    assert redactor.file("Σημειώσεις.docx") == "file-1.docx"
    assert redactor.file("χωρίς κατάληξη") == "file-2"
    assert redactor.counts() == {"host": 2, "path": 2, "text": 2, "file": 2}


def test_the_redacted_chat_replays_through_the_runtime():
    scenario = redact(EXPORT, "naxos-hotel-replay")
    result = run_scenario(scenario)
    # The replay asks for the same tools in the same rounds; each turn ends as it did.
    assert result.problems == []
    assert all(v["passed"] for v in result.verdicts), [
        v for v in result.verdicts if not v["passed"]
    ]


def test_anything_original_left_refuses_the_scenario(monkeypatch):
    # A redactor that forgets free text: the leak scan catches it, naming only where.
    monkeypatch.setattr(Redactor, "text", lambda self, value: value or "")
    with pytest.raises(Leak) as leak:
        redact(EXPORT, "naxos-hotel-replay")
    assert "steps.0.message: an original value remains" in str(leak.value)
    assert EMAIL not in str(leak.value) and "Άννα" not in str(leak.value)


@pytest.mark.parametrize(
    "planted, says",
    [
        ("write to someone@else.example.org", "an e-mail address remains"),
        ("see http://intranet.local/hr", "a web address remains"),
        ("ticket 0123456789abcdef0123456789abcdef", "an id remains"),
    ],
)
def test_addresses_and_ids_that_are_not_placeholders_are_leaks(planted, says):
    scenario = {"steps": [{"do": "send", "message": planted}]}
    assert leaks(scenario, set()) == [f"steps.0.message: {says}"]
    assert leaks({"x": "https://site-3.example/page-2 person-1@example.invalid"}, set()) == []


def test_personal_strings_leave_out_only_the_engines_own_words():
    found = personal_strings(EXPORT)
    assert {EMAIL, f"https://{HOST}/rooms/42?ref=anna.", HOST, "ξενοδοχεία Νάξος Άννα"} <= found
    assert "web_search" not in found and "tool_started" not in found and "completed" not in found
    assert "invalid_api_key" not in found


def test_the_command_line_writes_only_a_clean_scenario(tmp_path, monkeypatch, capsys):
    export = tmp_path / "export.json"
    export.write_text(json.dumps(EXPORT, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "replay.json"
    assert redaction.main([str(export), "--id", "naxos-replay", "--out", str(out)]) == 0
    assert problems(json.loads(out.read_text(encoding="utf-8"))) == []
    with pytest.raises(SystemExit):
        redaction.main([str(export), "--id", "naxos-replay", "--out", str(out)])  # exists
    with pytest.raises(SystemExit):
        redaction.main([str(export), "--id", "Not_A_Good_Id", "--out", str(tmp_path / "x.json")])
    # A leak writes nothing and says why.
    monkeypatch.setattr(Redactor, "file", lambda self, value: value)
    leaky = tmp_path / "leaky.json"
    assert redaction.main([str(export), "--id", "leaky", "--out", str(leaky)]) == 2
    assert not leaky.exists()
    assert "Nothing was written" in capsys.readouterr().err


def test_a_single_task_or_a_list_of_tasks_is_an_export_too():
    task = copy.deepcopy(EXPORT["tasks"][0])
    for export in (task, [task]):
        scenario = redact(export, "one-turn")
        assert [s["do"] for s in scenario["steps"]] == ["send", "run"]
        assert scenario["owl"]["name"] == "Owl 1"
    with pytest.raises(ValueError):
        redact({"something": "else"}, "nothing")


def test_a_chat_the_runtime_really_ran_comes_back_the_same_shape_without_its_details():
    """Round trip: run a chat with personal details, export it as the API does, redact it, and
    replay it: the same tools in the same turns, the same endings, and none of the details."""
    secrets = (
        "Μαρία Κ.",
        "maria.k@mail.example",
        "intranet.office.example",
        "Μισθοί Μαρίας",
        "王芳",
    )
    original = {
        "id": "personal-chat",
        "pins": "A chat with personal details, to be exported and redacted.",
        "owl": {"name": "Γλαύκα της Μαρίας Κ.", "purpose": "Γραφείο της Μαρίας Κ."},
        "script": {
            "owl": [
                {
                    "call": [
                        {
                            "tool": "save_file",
                            "arguments": {
                                "name": "Μισθοί Μαρίας.txt",
                                "content": "maria.k@mail.example",
                            },
                        }
                    ]
                },  # fmt: skip
                {"call": [{"tool": "list_files", "arguments": {}}]},
                {"say": "Αποθήκευσα τους μισθούς για τη Μαρία Κ."},
                {"say": "Αποθήκευσα τους Μισθοί Μαρίας στη Βιβλιοθήκη, Μαρία Κ."},
                {
                    "error": {
                        "message": "LLM error 401: bad key for 王芳",
                        "status": 401,
                        "code": "invalid_api_key",
                    }
                },  # fmt: skip
            ],
            "reply_checker": [
                {"decision": "repair", "issue": "omission", "reason": "Λείπει το Μισθοί Μαρίας."},
                {"decision": "ready", "reason": "Ταιριάζει με maria.k@mail.example."},
            ],
        },
        "steps": [
            {
                "do": "send",
                "id": "ask",
                "message": "Γράψε τους μισθούς από https://intranet.office.example/hr/μισθοί για τη Μαρία Κ. (maria.k@mail.example)",
            },
            {"do": "run"},
            {"do": "send", "id": "again", "message": "王芳 也要一份。"},
            {"do": "run"},
        ],
        "checks": [{"kind": "turn", "turn": "again", "status": "failed"}],
    }
    ran = run_scenario(original)
    assert ran.passed, ran.problems
    scenario = redact(ran.export, "personal-chat-replay")
    written = text_of(scenario)
    for secret in secrets:
        assert secret.casefold() not in written.casefold(), secret
    replay = run_scenario(scenario)
    assert replay.problems == [] and replay.evidence["script"]["unused"] == {}

    def shape(result):
        return [
            (
                turn["status"],
                [
                    c["name"]
                    for c in result.evidence["requested_calls"]
                    if c["task_id"] == turn["task_id"]
                ],
                bool([r for r in result.evidence["replies"] if r["task_id"] == turn["task_id"]]),
            )
            for turn in result.evidence["turns"]
        ]

    assert (
        shape(replay)
        == shape(ran)
        == [
            ("completed", ["save_file", "list_files"], True),
            ("failed", [], False),
        ]
    )
    assert [c["decision"] for c in replay.evidence["reply_checks"]] == ["repair", "ready"]
