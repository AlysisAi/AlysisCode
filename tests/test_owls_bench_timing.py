"""Where an owl turn's time went: parts that never overlap, labelled by purpose and tool kind."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.qa.owls_bench import report, score
from scripts.qa.owls_bench.run import task_receipts
from scripts.qa.owls_bench.tasks import parse_task
from scripts.qa.owls_bench.timing import time_split

T0 = 1_800_000_000.0


def _event(kind, at, **data):
    return {"kind": kind, "created_at": T0 + at, "data": data}


def _call(call_id, began, ended):
    return {"id": call_id, "created_at": T0 + began, "updated_at": T0 + ended, "usage": None}


def _row(events, calls, status="completed"):
    return {"status": status, "events": events, "usage": calls, "updated_at": T0 + 100}


def _parts_sum(split):
    return sum(split["model_s"].values()) + sum(split["tools_s"].values()) + split["service_s"]


def test_a_website_turn_splits_into_steps_reviews_checks_and_service():
    """A click reviewed by the action reviewer: the review counts as review, not as browser."""
    row = _row(
        [
            _event("queued", -2.0),
            _event("running", 0.0),
            _event("model_started", 0.5, model="m", call_id="c1", purpose="turn"),
            _event("tool_started", 3.1, name="browser_click"),
            _event("model_started", 3.2, model="m", call_id="c2", purpose="action_review"),
            _event("tool_completed", 5.0, name="browser_click"),
            _event("model_started", 5.1, model="m", call_id="c3", purpose="turn"),
            _event("model_started", 6.0, model="m", call_id="c4", purpose="reply_check"),
            _event("completed", 7.5),
        ],
        [
            _call("c1", 0.5, 3.0),
            _call("c2", 3.2, 4.2),
            _call("c3", 5.1, 5.9),
            _call("c4", 6.0, 7.0),
        ],
    )
    split = time_split(row)
    assert split["wall_s"] == 7.5
    assert split["queue_s"] == 2.0
    assert split["model_s"] == {"turn": 3.3, "action_review": 1.0, "reply_check": 1.0}
    assert split["tools_s"] == {"browser": 0.9}
    assert split["service_s"] == pytest.approx(1.3)
    assert _parts_sum(split) == pytest.approx(split["wall_s"])
    assert split["before_first_step_s"] == 0.5
    assert split["first_computer_step_s"] == pytest.approx(1.9)


def test_a_summary_before_the_first_step_is_the_person_waiting():
    row = _row(
        [
            _event("running", 0.0),
            _event("model_started", 0.1, model="m", call_id="s", purpose="history_summary"),
            _event("model_started", 9.0, model="m", call_id="t", purpose="turn"),
            _event("completed", 12.0),
        ],
        [_call("s", 0.1, 8.8), _call("t", 9.0, 11.5)],
    )
    split = time_split(row)
    assert split["before_first_step_s"] == 9.0
    assert split["model_s"] == {"turn": 2.5, "history_summary": 8.7}
    assert _parts_sum(split) == pytest.approx(12.0)


def test_helpers_side_by_side_are_counted_once():
    """Three research helpers thinking at the same time take three seconds, not nine."""
    row = _row(
        [
            _event("running", 0.0),
            _event("model_started", 0.0, model="m", call_id="t", purpose="turn"),
            _event("tool_started", 1.0, name="research_helpers"),
            *(
                _event("model_started", 1.0, model="m", call_id=f"h{n}", purpose="research_helper")
                for n in range(3)
            ),
            _event("tool_completed", 4.5, name="research_helpers"),
            _event("completed", 5.0),
        ],
        [_call("t", 0.0, 1.0), *(_call(f"h{n}", 1.0, 4.0) for n in range(3))],
    )
    split = time_split(row)
    assert split["model_s"] == {"turn": 1.0, "research_helper": 3.0}
    assert split["tools_s"] == {"web": 0.5}
    assert _parts_sum(split) == pytest.approx(5.0)


def test_records_from_older_services_still_name_reviews_and_checks():
    """Before calls carried their purpose, only the reviewer's and the checker's own events
    named their call; every other call is unlabelled."""
    row = _row(
        [
            _event("running", 0.0),
            _event("model_started", 0.0, model="m"),
            _event("model_started", 2.0, model="m"),
            _event("action_review_call", 2.0, call_id="r", model="m"),
            _event("model_started", 3.0, model="m"),
            _event("reply_review_started", 3.0, call_id="k"),
            _event("completed", 4.0),
        ],
        [_call("a", 0.0, 2.0), _call("r", 2.0, 3.0), _call("k", 3.0, 4.0)],
    )
    split = time_split(row)
    assert split["model_s"] == {"action_review": 1.0, "reply_check": 1.0, "unlabelled": 2.0}
    assert split["before_first_step_s"] is None


def test_a_step_cut_off_by_the_end_runs_to_the_end_and_nested_steps_go_to_the_inner_one():
    row = _row(
        [
            _event("running", 0.0),
            _event("tool_started", 1.0, name="computer_run"),
            _event("tool_started", 2.0, name="read_file"),
            _event("tool_completed", 3.0, name="read_file"),
            _event("failed", 6.0),
        ],
        [],
        status="failed",
    )
    split = time_split(row)
    assert split["tools_s"] == {"computer": 4.0, "files": 1.0}
    assert split["first_computer_step_s"] == 5.0
    assert _parts_sum(split) == pytest.approx(6.0)


def test_no_clock_no_split():
    assert time_split({"status": "completed", "events": [], "usage": []}) is None
    assert time_split(_row([_event("queued", 0.0)], [], status="running")) is None


def test_the_runner_records_the_split():
    row = _row(
        [
            _event("queued", 0.0),
            _event("running", 0.5),
            _event("model_started", 0.5, model="m", call_id="c", purpose="turn"),
            _event("completed", 2.0),
        ],
        [_call("c", 0.5, 1.5)],
    )
    receipts = task_receipts(row)
    assert receipts["time_split"]["model_s"] == {"turn": 1.0}
    assert receipts["time_split"]["service_s"] == 0.5


def _task(tmp_path: Path):
    (tmp_path / "input.csv").write_text("a;b\n1;2\n", encoding="utf-8")
    task, problems = parse_task(
        {
            "id": "sample-task",
            "category": "website_task",
            "language": "el",
            "split": "dev",
            "prompt": "Βρες τα δρομολόγια του τρένου για αύριο.",
            "attachments": [],
            "expect": {"deliverable": "none", "reply_language": "el", "min_sources": 0},
        },
        base=tmp_path,
    )
    assert not problems, problems
    return task


def test_rescoring_an_older_run_works_the_split_out_from_its_saved_task(tmp_path):
    """Runs recorded before the split existed get it from the task record they saved."""
    record_dir = tmp_path / "runs" / "owls" / "sample-task" / "run-1"
    record_dir.mkdir(parents=True)
    attempt = {
        "index": 1,
        "status": "completed",
        "reply": "Το τρένο φεύγει στις 07:10.",
        "files": [],
    }
    (record_dir / "record.json").write_text(
        json.dumps({"system": "owls", "label": "Owls", "run": 1, "attempts": [attempt]}),
        encoding="utf-8",
    )
    saved = _row(
        [
            _event("running", 0.0),
            _event("model_started", 0.0, model="m", call_id="c", purpose="turn"),
            _event("tool_started", 1.0, name="browser_navigate"),
            _event("tool_completed", 3.0, name="browser_navigate"),
            _event("completed", 4.0),
        ],
        [_call("c", 0.0, 1.0)],
    )
    (record_dir / "task-attempt-1.json").write_text(json.dumps(saved), encoding="utf-8")
    result = score.score_record(_task(tmp_path), record_dir)
    assert result["metrics"]["time_split"]["tools_s"] == {"browser": 2.0}
    assert result["metrics"]["time_split"]["first_computer_step_s"] == 2.0


def _scored(category, split):
    return {
        "system": "owls",
        "label": "Owls",
        "task_id": f"{category}-a",
        "task_sha256": "sha",
        "recorded_task_sha256": "sha",
        "split": "dev",
        "category": category,
        "language": "el",
        "run": 1,
        "checks": [{"id": "finished", "status": "pass"}],
        "deterministic": {
            "passed": 1,
            "applicable": 1,
            "unknown": 0,
            "all_passed": True,
            "pass_rate": 1.0,
        },
        "metrics": {"status": "completed", "wall_s": split["wall_s"], "time_split": split},
        "judge": None,
    }


def test_the_report_shows_each_parts_share_of_the_time():
    web = {
        "wall_s": 10.0,
        "model_s": {"turn": 4.0, "action_review": 3.0, "reply_check": 1.0},
        "tools_s": {"browser": 1.5},
        "service_s": 0.5,
        "before_first_step_s": 0.2,
        "first_computer_step_s": 1.0,
    }
    deck = {
        "wall_s": 30.0,
        "model_s": {"turn": 18.0, "history_summary": 6.0},
        "tools_s": {"files": 6.0},
        "service_s": 0.0,
        "before_first_step_s": 6.0,
        "first_computer_step_s": None,
    }
    built = report.build_report([_scored("website_task", web), _scored("deck", deck)])
    dev = built["systems"]["owls"]["splits"]["dev"]
    columns = dev["summary"]["time"]["columns"]
    assert columns["Turn steps"]["share"] == pytest.approx(22 / 40)
    assert columns["Action reviews"]["share"] == pytest.approx(3 / 40)
    assert columns["Summaries & memory"]["share"] == pytest.approx(6 / 40)
    assert sum(column["share"] for column in columns.values()) == pytest.approx(1.0, abs=1e-3)
    assert dev["categories"]["website_task"]["time"]["columns"]["Browser"]["median_s"] == 1.5
    assert dev["summary"]["time"]["median_before_first_step_s"] == pytest.approx(3.1)
    markdown = report.render_markdown(built)
    assert "Where the time went" in markdown
    assert "| website_task | 1 | 40% | 30% | 10% |" in markdown


def test_runs_without_a_split_leave_the_time_table_out():
    plain = _scored("deck", {"wall_s": 5.0})
    plain["metrics"].pop("time_split")
    built = report.build_report([plain])
    assert built["systems"]["owls"]["splits"]["dev"]["summary"]["time"] is None
    assert "Where the time went" not in report.render_markdown(built)
