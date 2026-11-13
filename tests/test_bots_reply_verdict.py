from __future__ import annotations

import pytest

from alysis_code.bots.reply_review import parse_verdict


@pytest.mark.parametrize(
    "content",
    [
        '{"decision":"ready","reason":"Answers the question."}',
        '```json\n{"decision":"ready","reason":"Answers the question."}\n```',
        'Verdict: {"decision":"ready","reason":"Answers the question."}',
        '{"decision":"ready","reason":"Answers the question.","confidence":0.9}',
        '<think>Check the claim first.</think>\n{"decision":"ready","reason":"Answers the question."}',
    ],
)
def test_verdict_is_read_the_way_models_write_it(content):
    verdict = parse_verdict(content)
    assert (verdict.decision, verdict.reason) == ("ready", "Answers the question.")


def test_long_reason_is_shortened_not_rejected():
    verdict = parse_verdict('{"decision":"repair","reason":"' + "x" * 3000 + '"}')
    assert verdict.decision == "repair" and len(verdict.reason) == 1600


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "Looks good to me.",
        '{"decision":"maybe","reason":"Unsure."}',
        '{"decision":"ready"}',
        '["ready"]',
    ],
)
def test_a_verdict_without_a_clear_decision_stays_invalid(content):
    with pytest.raises(ValueError):
        parse_verdict(content)


def test_a_repair_says_what_it_is_about():
    verdict = parse_verdict(
        '```json\n{"decision":"repair","reason":"Contradicted.","issue":"contradiction",'
        '"false_claim":"  The file exists. "}\n```'
    )
    assert (verdict.issue, verdict.false_claim) == ("contradiction", "The file exists.")
    assert verdict.withholds_final_status
    omission = parse_verdict(
        '{"decision":"repair","reason":"Weekdays unmentioned.","issue":"omission"}'
    )
    assert not omission.withholds_final_status
    # An omission that also quotes a false sentence still keeps that sentence from the person.
    assert parse_verdict(
        '{"decision":"repair","reason":"Not saved.","issue":"omission",'
        '"false_claim":"My private computer is unavailable."}'
    ).withholds_final_status


@pytest.mark.parametrize(
    "content",
    [
        '{"decision":"repair","reason":"Unclear."}',
        '{"decision":"repair","reason":"Unclear.","issue":"style"}',
        'Note: {"decision":"repair","reason":"Unclear.","issue":7}',
    ],
)
def test_an_unclassified_repair_keeps_withholding(content):
    verdict = parse_verdict(content)
    assert verdict.issue is None and verdict.withholds_final_status


def test_ready_never_withholds():
    assert not parse_verdict(
        '{"decision":"ready","reason":"Fine.","issue":"contradiction"}'
    ).withholds_final_status
