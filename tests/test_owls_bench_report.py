"""Owls benchmark report aggregation: medians, separate splits, and drift from a baseline."""

from __future__ import annotations

import json

from scripts.qa.owls_bench import report


def _score(
    split="dev",
    category="deck",
    task_id="deck-a",
    run=1,
    *,
    system="owls",
    passed=True,
    wall=10.0,
    calls=5,
    tokens=1000,
    cost=None,
    judge=None,
    first_wall=None,
    approvals=0,
    questions=0,
    failures=0,
    status="completed",
    digest="digest-1",
    sha="sha-1",
):
    checks = [
        {"id": "finished", "status": "pass" if status == "completed" else "fail"},
        {"id": "slides", "status": "pass" if passed else "fail"},
    ]
    passed_count = sum(check["status"] == "pass" for check in checks)
    return {
        "system": system,
        "label": "Owls" if system == "owls" else "Reference",
        "task_id": task_id,
        "task_sha256": sha,
        "recorded_task_sha256": sha,
        "split": split,
        "category": category,
        "language": "el",
        "run": run,
        "checks": checks,
        "deterministic": {
            "passed": passed_count,
            "applicable": len(checks),
            "unknown": 0,
            "all_passed": passed_count == len(checks),
            "pass_rate": passed_count / len(checks),
        },
        "metrics": {
            "status": status,
            "wall_s": wall,
            "model_calls": calls,
            "tokens": tokens,
            "cost_usd": cost,
            "approvals": approvals,
            "questions": questions,
            "failures": failures,
            "first_wall_s": first_wall,
        },
        "judge": {"status": "ok", "score": judge, "digest": digest} if judge is not None else None,
    }


def test_medians_totals_and_rates():
    block = report.aggregate(
        [
            _score(run=1, wall=10.0, calls=4, tokens=900, cost=0.02, judge=1.0, approvals=1),
            _score(run=2, wall=30.0, calls=6, tokens=None, cost=None, judge=0.5, questions=2),
            _score(run=3, wall=20.0, calls=100, tokens=1100, cost=0.04, passed=False, failures=3),
        ]
    )
    assert block["runs"] == 3 and block["tasks"] == 1
    assert block["median_wall_s"] == 20.0
    assert block["median_model_calls"] == 6
    assert block["median_tokens"] == 1000 and block["tokens_reported_runs"] == 2
    assert block["median_cost_usd"] == 0.03 and block["cost_reported_runs"] == 2
    assert block["deterministic_pass_rate"] == round(2 / 3, 4)
    assert block["check_pass_rate"] == round(5 / 6, 4)
    assert block["judge_score"] == 0.75 and block["judged_runs"] == 2
    assert (block["approvals_asked"], block["questions_asked"], block["failures"]) == (1, 2, 3)
    assert block["checks_not_passed"] == {"slides": 1}


def test_repeat_timing_compares_the_first_and_the_repeat():
    block = report.aggregate(
        [_score(wall=30.0, first_wall=60.0), _score(run=2, wall=50.0, first_wall=100.0)]
    )
    assert block["repeat"] == {
        "median_first_wall_s": 80.0,
        "median_repeat_wall_s": 40.0,
        "repeat_to_first_ratio": 0.5,
    }


def test_dev_and_heldout_are_reported_separately_never_pooled():
    scores = [
        _score("dev", "deck", "deck-a", wall=10.0, passed=True),
        _score("dev", "deck", "deck-a", run=2, wall=12.0, passed=True),
        _score("heldout", "deck", "deck-b", wall=99.0, passed=False),
        _score("dev", "deck", "deck-a", system="reference", wall=5.0),
    ]
    built = report.build_report(scores)
    owls = built["systems"]["owls"]["splits"]
    assert set(owls) == {"dev", "heldout"}
    assert owls["dev"]["summary"]["runs"] == 2
    assert owls["dev"]["summary"]["deterministic_pass_rate"] == 1.0
    assert owls["dev"]["summary"]["median_wall_s"] == 11.0
    assert owls["heldout"]["summary"]["deterministic_pass_rate"] == 0.0
    assert set(owls["heldout"]["tasks"]) == {"deck-b"}
    assert set(built["systems"]["reference"]["splits"]) == {"dev"}
    markdown = report.render_markdown(built)
    assert "### Dev split" in markdown and "### Held-out split" in markdown


def test_drift_per_category_with_notes_for_changed_judge_and_tasks():
    before = report.build_report(
        [_score(wall=10.0, judge=1.0), _score(run=2, wall=20.0, judge=1.0)]
    )
    now = report.build_report(
        [
            _score(wall=20.0, passed=False, judge=0.5, digest="digest-2", sha="sha-2"),
            _score(run=2, wall=40.0, judge=0.5, digest="digest-2", sha="sha-2"),
        ]
    )
    result = report.drift(now, before)
    row = result["changes"]["owls"]["dev"]["deck"]
    assert row["deterministic_pass_rate"] == {"now": 0.5, "before": 1.0, "change": -0.5}
    assert row["median_wall_s"]["change"] == 15.0
    assert row["judge_score"]["change"] == -0.5
    assert any("judge prompts differ" in note for note in result["notes"])
    assert any("deck-a" in note for note in result["notes"])
    assert "Drift from the baseline" in report.render_markdown(now, result)


def test_write_report_reads_scores_and_compares_with_a_baseline(tmp_path):
    for run, wall in ((1, 10.0), (2, 30.0)):
        folder = tmp_path / "runs" / "owls" / "deck-a" / f"run-{run}"
        folder.mkdir(parents=True)
        (folder / "score.json").write_text(json.dumps(_score(run=run, wall=wall)), encoding="utf-8")
    path = report.write_report(tmp_path)
    first = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert path.name == "report.md"
    assert first["systems"]["owls"]["splits"]["dev"]["summary"]["median_wall_s"] == 20.0
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(first), encoding="utf-8")
    report.write_report(tmp_path, baseline=baseline)
    second = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert second["drift"]["changes"]["owls"]["dev"]["deck"]["median_wall_s"]["change"] == 0
