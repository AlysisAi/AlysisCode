from __future__ import annotations

import threading

import pytest

from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.user_questions import UserQuestionBroker, parse_questions


def question_args(kind="single"):
    return {
        "questions": [
            {
                "id": "design",
                "question": "Which design?",
                "kind": kind,
                "options": [] if kind == "text" else ["Simple", "Detailed"],
            }
        ]
    }


@pytest.mark.parametrize("kind", ["single", "multiple", "text"])
def test_answer_is_explicit_and_delivered_once(kind):
    broker = UserQuestionBroker()
    result = broker.request(question_args(kind))
    identifier = result["request_id"]
    assert result["status"] == "pending"
    assert broker.collect(identifier, wait=False)["status"] == "pending"
    selected = [] if kind == "text" else ["Simple"]
    if kind == "multiple":
        selected.append("Detailed")
    answers = [{"id": "design", "selected": selected, "text": "My own preference"}]
    assert broker.submit(identifier, answers)
    answers[0]["selected"].clear()
    assert not broker.submit(identifier, answers)
    received = broker.collect(identifier)
    assert received["status"] == "answered"
    assert received["answers"][0]["text"] == "My own preference"
    assert len(received["answers"][0]["selected"]) == {"text": 0, "single": 1, "multiple": 2}[kind]
    assert broker.collect(identifier)["status"] == "already_delivered"
    assert broker.collect_outstanding() is None


@pytest.mark.parametrize(
    "answers",
    [
        [],
        [{"id": "design", "selected": [], "text": ""}],
        [{"id": "design", "selected": ["Unknown"], "text": ""}],
        [{"id": "design", "selected": ["Simple", "Detailed"], "text": ""}],
        [{"id": "wrong", "selected": ["Simple"], "text": ""}],
        [{"id": "design", "selected": [], "text": "x" * 4001}],
    ],
)
def test_invalid_answer_keeps_request_pending(answers):
    broker = UserQuestionBroker()
    identifier = broker.request(question_args())["request_id"]
    with pytest.raises(ValueError):
        broker.submit(identifier, answers)
    assert broker.active is not None


@pytest.mark.parametrize(
    "args",
    [{}, {"questions": []}, {"questions": [None]}, {"questions": question_args()["questions"] * 2}],
)
def test_invalid_questions_never_open_panel(args):
    broker = UserQuestionBroker()
    assert broker.request(args)["status"] == "invalid"
    assert broker.active is None


def test_wait_releases_for_dismissal_and_never_selects_a_default():
    broker = UserQuestionBroker()
    identifier = broker.request(question_args())["request_id"]
    results = []
    worker = threading.Thread(target=lambda: results.append(broker.collect(identifier)))
    worker.start()
    assert "error" in broker.request(question_args())
    assert broker.dismiss(identifier)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert results == [{"request_id": identifier, "status": "dismissed", "answers": []}]


@pytest.mark.parametrize("action", ["cancel", "reset"])
def test_cancellation_and_teardown_release_waiters(action):
    broker = UserQuestionBroker()
    token = InteractiveCancellationToken()
    identifier = broker.request(question_args())["request_id"]
    results = []
    worker = threading.Thread(
        target=lambda: results.append(broker.collect(identifier, cancellation_token=token))
    )
    worker.start()
    if action == "cancel":
        token.cancel()
    else:
        broker.reset()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert results[0]["status"] in {"cancelled", "unavailable"}
    assert not broker.submit(identifier, [{"id": "design", "selected": ["Simple"], "text": ""}])


def test_reset_expires_old_request_without_accepting_answer_for_new_request():
    broker = UserQuestionBroker()
    old = broker.request(question_args())["request_id"]
    broker.reset()
    new = broker.request(question_args())["request_id"]
    assert old != new
    assert not broker.submit(old, [{"id": "design", "text": "late"}])
    assert broker.collect(old)["status"] == "unavailable"
    assert broker.collect(new, wait=False)["status"] == "pending"


def test_unicode_is_preserved_and_terminal_control_sequences_are_rejected():
    args = question_args("text")
    args["questions"][0]["question"] = "Ποιο σχέδιο προτιμάς; 你喜欢什么？"
    assert parse_questions(args)[0].question == args["questions"][0]["question"]
    args["questions"][0]["question"] = "\x1b[2J"
    with pytest.raises(ValueError):
        parse_questions(args)


def test_timeout_starts_at_request_and_is_delivered_once_without_a_default():
    now = [100.0]
    notices = []
    broker = UserQuestionBroker(clock=lambda: now[0], on_timeout=notices.append)
    response = broker.request(question_args())
    identifier = response["request_id"]
    assert response["timeout_seconds"] == 60
    now[0] = 159.5
    assert broker.remaining_seconds == 1
    assert broker.collect(identifier, wait=False)["status"] == "pending"
    now[0] = 160.0
    assert broker.active is None
    assert not broker.touch(identifier)
    assert not broker.submit(identifier, [{"id": "design", "selected": ["Simple"]}])
    result = broker.collect(identifier)
    assert result["status"] == "timed_out"
    assert result["answers"] == []
    assert result["skipped_question_ids"] == ["design"]
    assert "never grants permission" in result["guidance"]
    assert broker.collect(identifier)["status"] == "already_delivered"
    assert notices == [result]
    assert not broker.outstanding


def test_activity_resets_idle_deadline_and_answer_before_expiry_wins():
    now = [0.0]
    broker = UserQuestionBroker(clock=lambda: now[0])
    identifier = broker.request(question_args())["request_id"]
    now[0] = 59.0
    assert broker.touch(identifier)
    assert broker.remaining_seconds == 60
    now[0] = 118.5
    assert broker.remaining_seconds == 1
    assert broker.submit(identifier, [{"id": "design", "selected": ["Detailed"]}])
    now[0] = 500.0
    assert broker.collect(identifier)["status"] == "answered"


def test_timeout_keeps_only_confirmed_answers_in_question_order():
    now = [0.0]
    broker = UserQuestionBroker(clock=lambda: now[0])
    args = question_args()
    args["questions"] += [
        {"id": "name", "question": "What name?", "kind": "text", "options": []},
        {"id": "extra", "question": "Anything else?", "kind": "text", "options": []},
    ]
    identifier = broker.request(args)["request_id"]
    now[0] = 30.0
    assert broker.confirm(identifier, {"id": "name", "text": "Project name"})
    answer = {"id": "design", "selected": ["Simple"], "text": ""}
    assert broker.confirm(identifier, answer)
    answer["selected"].clear()
    assert broker.confirm(identifier, {"id": "design", "selected": ["Detailed"]})
    now[0] = 90.0
    result = broker.collect_outstanding()
    assert result["status"] == "timed_out"
    assert result["answers"] == [
        {"id": "design", "selected": ["Detailed"], "text": ""},
        {"id": "name", "selected": [], "text": "Project name"},
    ]
    assert result["skipped_question_ids"] == ["extra"]
    assert not broker.confirm(identifier, {"id": "extra", "text": "Late answer"})


def test_waiter_expires_without_ui_polling_or_a_new_wait_deadline():
    broker = UserQuestionBroker(timeout_seconds=0.04)
    identifier = broker.request(question_args())["request_id"]
    results = []
    worker = threading.Thread(
        target=lambda: results.append(broker.collect(identifier)), daemon=True
    )
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert results[0]["status"] == "timed_out"


def test_cancellation_wins_over_timeout_and_reset_clears_confirmed_answers():
    now = [0.0]
    broker = UserQuestionBroker(clock=lambda: now[0])
    identifier = broker.request(question_args())["request_id"]
    assert broker.confirm(identifier, {"id": "design", "selected": ["Simple"]})
    now[0] = 60.0
    token = InteractiveCancellationToken()
    token.cancel()
    assert broker.collect(identifier, cancellation_token=token)["status"] == "cancelled"
    broker.reset()
    new = broker.request(question_args())["request_id"]
    assert not broker.touch(identifier)
    now[0] = 120.0
    assert broker.collect(new)["answers"] == []


@pytest.mark.parametrize("outcome", ["answered", "dismissed", "timed_out"])
def test_repeated_decisions_do_not_reopen_without_new_user_direction(outcome):
    now = [0.0]
    broker = UserQuestionBroker(clock=lambda: now[0])
    original = broker.request(question_args())["request_id"]
    if outcome == "answered":
        broker.submit(original, [{"id": "design", "selected": ["Simple"]}])
    elif outcome == "dismissed":
        broker.dismiss(original)
    else:
        now[0] = 60.0
    assert broker.collect(original)["status"] == outcome
    repeated = question_args()
    repeated["questions"][0].update(
        id="new_id", question="  WHICH   DESIGN?! ", options=["DETAILED", "simple"]
    )
    result = broker.request(repeated)
    assert result["status"] == "duplicate_questions"
    assert result["question_ids"] == ["new_id"]
    assert result["previous_request_ids"] == [original]
    assert broker.active is None
    assert not broker.outstanding
    broker.note_user_input()
    reopened = broker.request(repeated)
    assert reopened["status"] == "pending"
    assert reopened["request_id"] != original


def test_duplicate_decisions_in_one_batch_are_rejected_but_new_decisions_are_allowed():
    broker = UserQuestionBroker()
    args = question_args()
    args["questions"].append({**args["questions"][0], "id": "duplicate"})
    assert broker.request(args)["status"] == "invalid"
    assert broker.active is None
    identifier = broker.request(question_args())["request_id"]
    broker.dismiss(identifier)
    broker.collect(identifier)
    followup = question_args()
    followup["questions"][0]["question"] = "Which design for the new admin view?"
    assert broker.request(followup)["status"] == "pending"


def test_duplicate_matching_preserves_meaningful_technical_punctuation():
    args = {
        "questions": [
            {"id": "one", "question": "How do you use C#?", "kind": "text", "options": []},
            {"id": "two", "question": "How do you use C++?", "kind": "text", "options": []},
        ]
    }
    assert UserQuestionBroker().request(args)["status"] == "pending"


def test_new_user_direction_does_not_discard_an_active_question_or_answer():
    broker = UserQuestionBroker()
    identifier = broker.request(question_args())["request_id"]
    broker.note_user_input()
    assert broker.active.id == identifier
    assert "error" in broker.request(question_args())
    assert broker.submit(identifier, [{"id": "design", "selected": ["Simple"]}])
    broker.note_user_input()
    assert broker.collect(identifier)["answers"][0]["selected"] == ["Simple"]
