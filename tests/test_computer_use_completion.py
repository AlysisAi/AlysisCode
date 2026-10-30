import json

import pytest

from alysis_code.agent.computer_use_completion import computer_use_completion_report


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        123,
        {"outcome": [], "summary": "x"},
        {"outcome": {}, "summary": "x"},
        {"outcome": "verified_success", "summary": "x"},
        {"outcome": "completed_unverified", "summary": ""},
        {"outcome": "blocked", "summary": "x", "extra": True},
    ],
)
def test_malformed_reports_are_incomplete(value):
    text = json.dumps(value)
    assert computer_use_completion_report(text) == ("incomplete", text, False)


@pytest.mark.parametrize("outcome", ["completed_unverified", "incomplete", "blocked"])
def test_typed_reports_do_not_classify_summary_keywords(outcome):
    prose = "Blocked and incomplete are words on the screen; complete is another word."
    assert computer_use_completion_report(json.dumps({"outcome": outcome, "summary": prose})) == (
        outcome,
        prose,
        True,
    )


def test_plain_completion_prose_cannot_certify_goal():
    assert computer_use_completion_report("Task completed successfully.") == (
        "incomplete",
        "Task completed successfully.",
        False,
    )
