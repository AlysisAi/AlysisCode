"""Offline checks of the owls benchmark's deterministic scorers on small fixture outputs."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from scripts.qa.owls_bench import builders, judge, score
from scripts.qa.owls_bench.documents import Cell, Table, count_sources, inspect_bytes, numbers_in
from scripts.qa.owls_bench.language import check_reply_script, expected_scripts
from scripts.qa.owls_bench.tasks import parse_task


def _task(tmp_path: Path, **overrides):
    raw = {
        "id": "sample-task",
        "category": "data_file",
        "language": "el",
        "split": "dev",
        "prompt": "Φτιάξε έναν πίνακα με τα σύνολα.",
        "attachments": ["input.csv"],
        "expect": {
            "deliverable": "xlsx",
            "min_rows": 3,
            "columns": ["store", "revenue"],
            "totals_reconcile": True,
            "chart": True,
            "values": [{"meaning": "total revenue", "value": 3703.5, "decimals": 2}],
            "min_sources": 0,
            "reply_language": "el",
        },
    }
    raw.update(overrides)
    (tmp_path / "input.csv").write_text("a;b\n1;2\n", encoding="utf-8")
    task, problems = parse_task(raw, base=tmp_path)
    assert not problems, problems
    return task


def _record(tmp_path: Path, files: dict[str, bytes], reply: str, status: str = "completed"):
    record_dir = tmp_path / "runs" / "owls" / "sample-task" / "run-1"
    folder = record_dir / "files" / "attempt-1"
    folder.mkdir(parents=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    record = {
        "system": "owls",
        "label": "Owls",
        "run": 1,
        "attempts": [
            {
                "status": status,
                "reply": reply,
                "wall_s": 12.0,
                "model_calls": 4,
                "tokens": {"total": 900, "unreported_calls": 0},
                "cost_usd": 0.01,
                "approvals": 0,
                "questions": 0,
                "failures": 0,
                "files": [f"files/attempt-1/{name}" for name in files],
            }
        ],
    }
    (record_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    return record_dir


GREEK_REPLY = "Τα συνολικά έσοδα είναι 3.703,50 € και το κατάστημα με τα περισσότερα είναι η Πάτρα."


def _statuses(result):
    return {check["id"]: check["status"] for check in result["checks"]}


# --- Reply language ------------------------------------------------------------------------


def test_greek_reply_to_greek_task_passes_the_script_check():
    result = check_reply_script(GREEK_REPLY, "el")
    assert result["status"] == "pass"
    assert result["language_identity"] == "confirmed_by_script"


def test_english_reply_to_greek_task_fails_the_script_check():
    result = check_reply_script("The total revenue is 3,703.50 euros; Patras sold the most.", "el")
    assert result["status"] == "fail"
    assert result["dominant"] == "Latin"


def test_links_and_code_do_not_outweigh_a_greek_reply():
    reply = (
        "Βρήκα τα στοιχεία στις σελίδες https://example.org/very/long/english/path/words/here "
        "και `some_english_code_identifier()` όπως ζήτησες."
    )
    assert check_reply_script(reply, "el")["status"] == "pass"


def test_han_reply_passes_and_leaves_the_language_to_the_judge():
    result = check_reply_script("演示文稿共五页，介绍了二十四节气的由来、习俗和代表性节气。", "zh")
    assert result["status"] == "pass"
    assert result["dominant"] == "Han"
    assert result["language_identity"] == "left_to_judge"
    assert check_reply_script("Die Präsentation hat fünf Folien über die Jahreszeiten.", "zh")[
        "status"
    ] == ("fail")


def test_languages_sharing_a_script_are_left_to_the_judge():
    result = check_reply_script("The deck has five slides about the solar terms.", "es")
    assert result["status"] == "pass"
    assert result["language_identity"] == "left_to_judge"
    assert "judge" in result["detail"]


def test_script_subtags_and_unknown_languages():
    assert expected_scripts("sr-Latn") == ("Latin",)
    assert expected_scripts("sr") == ("Cyrillic",)
    assert expected_scripts("zh-Hant-TW") == ("Han",)
    assert check_reply_script("x" * 50, "qaa")["status"] == "unknown"
    assert check_reply_script("Γεια", "el")["status"] == "unknown"


# --- Links -----------------------------------------------------------------------------------


def test_distinct_links_are_counted_by_page_and_by_site():
    links = [
        "https://el.wikipedia.org/wiki/Αθήνα",
        "https://el.wikipedia.org/wiki/Πάτρα",
        "https://el.wikipedia.org/wiki/Πάτρα/",
        "https://www.museum.example/",
        "https://museum.example/visit",
    ]
    counted = count_sources(links)
    assert counted["distinct_urls"] == 4
    assert counted["distinct_hosts"] == 2


# --- Totals ----------------------------------------------------------------------------------


def _table(rows):
    return Table(
        "t",
        [
            [
                Cell(text=str(v), number=float(v), decimals=len(str(v).partition(".")[2]))
                if isinstance(v, (int, float))
                else Cell(text=v)
                for v in row
            ]
            for row in rows
        ],
    )


def test_totals_reconcile_exactly_and_within_rounding():
    exact = _table([["store", "revenue"], ["A", 1000.25], ["B", 2703.25], ["total", 3703.5]])
    assert score.reconcile_table(exact)["total_row"] == 4
    rounded = _table([["store", "revenue"], ["A", 10.4], ["B", 20.4], ["total", 31]])
    assert score.reconcile_table(rounded) is not None
    wrong = _table([["store", "revenue"], ["A", 10.4], ["B", 20.4], ["total", 32]])
    assert score.reconcile_table(wrong) is None


def test_totals_reconcile_with_decimal_commas_in_text_cells():
    table = Table(
        "t",
        [
            [Cell(text="Κατάστημα"), Cell(text="Έσοδα")],
            [Cell(text="Πάτρα"), Cell(text="1.234,50 €")],
            [Cell(text="Ηράκλειο"), Cell(text="2.469,00 €")],
            [Cell(text="Σύνολο"), Cell(text="3.703,50 €")],
        ],
    )
    assert score.reconcile_table(table)["decimal_mark"] == ","


def test_a_total_row_followed_by_an_average_still_reconciles():
    table = _table([["k", "v"], ["a", 5], ["b", 7], ["c", 8], ["total", 20], ["average", 6.67]])
    assert score.reconcile_table(table)["total"] == 20


def test_a_numbering_column_is_not_a_total():
    assert score.reconcile_table(_table([["#", "name"], [1, "a"], [2, "b"], [3, "c"]])) is None


def test_sum_formula_without_a_saved_value_is_evaluated(tmp_path):
    data = builders.xlsx_bytes(
        {
            "S": [
                ["store", "revenue"],
                ["A", 1000.25],
                ["B", 2703.25],
                ["total", builders.Formula("SUM(B2:B3)")],
            ]
        }
    )
    table = inspect_bytes("a.xlsx", data).tables[0]
    assert score.reconcile_table(table)["total"] == pytest.approx(3703.5)
    off_by_one = builders.xlsx_bytes(
        {
            "S": [
                ["store", "revenue"],
                ["A", 1000.25],
                ["B", 2703.25],
                ["C", 1.0],
                ["total", builders.Formula("SUM(B2:B3)")],
            ]
        }
    )
    assert score.reconcile_table(inspect_bytes("b.xlsx", off_by_one).tables[0]) is None


# --- Values ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "found"),
    [
        ("219.877,70 €", True),
        ("219,877.70", True),
        ("219877.7", True),
        ("≈ 219.878 €", True),
        ("219 877,70", True),
        ("219,800", False),
        ("219.877,07", False),
    ],
)
def test_expected_values_match_at_their_written_precision(written, found):
    assert score.value_found(219877.70, 2, numbers_in(written)) is found


# --- Whole runs --------------------------------------------------------------------------


def _good_workbook() -> bytes:
    return builders.xlsx_bytes(
        {
            "Σύνολα": [
                ["Κατάστημα", "Έσοδα"],
                ["Πάτρα", 1234.5],
                ["Ηράκλειο", 2469.0],
                ["Σύνολο", builders.Formula("SUM(B2:B3)", cached=3703.5)],
            ]
        },
        chart=True,
    )


def test_a_good_run_passes_every_deterministic_check(tmp_path):
    task = _task(tmp_path)
    record_dir = _record(tmp_path, {"σύνολα.xlsx": _good_workbook()}, GREEK_REPLY)
    result = score.score_record(task, record_dir)
    assert _statuses(result) == {
        "finished": "pass",
        "deliverable_opens": "pass",
        "min_rows": "pass",
        "columns": "pass",
        "totals_reconcile": "pass",
        "value_1": "pass",
        "chart": "pass",
        "reply_script": "pass",
    }
    assert result["deterministic"]["all_passed"]
    assert json.loads((record_dir / "score.json").read_text(encoding="utf-8"))["task_id"] == task.id


def test_a_bad_run_fails_the_checks_it_misses(tmp_path):
    task = _task(tmp_path)
    workbook = builders.xlsx_bytes(
        {"S": [["Κατάστημα", "Έσοδα"], ["Πάτρα", 1234.5], ["Σύνολο", 9999]]}
    )
    record_dir = _record(
        tmp_path,
        {"wrong.xlsx": workbook, "notes.docx": builders.docx_bytes(["x"])},
        "Done: the totals are in the workbook.",
    )
    statuses = _statuses(score.score_record(task, record_dir))
    assert statuses["deliverable_opens"] == "pass"
    for failing in ("min_rows", "totals_reconcile", "value_1", "chart", "reply_script"):
        assert statuses[failing] == "fail", failing


def test_a_missing_or_broken_deliverable_fails(tmp_path):
    task = _task(tmp_path)
    record_dir = _record(tmp_path, {"report.xlsx": b"PK\x03\x04 not really"}, GREEK_REPLY)
    result = score.score_record(task, record_dir)
    check = next(c for c in result["checks"] if c["id"] == "deliverable_opens")
    assert check["status"] == "fail" and "does not open" in check["detail"]


def test_reply_only_task_counts_items_and_links(tmp_path):
    task = _task(
        tmp_path,
        category="website_task",
        attachments=[],
        expect={
            "deliverable": "none",
            "min_items": 3,
            "min_sources": 1,
            "reply_language": "el",
        },
    )
    reply = (
        "Οι τρεις φθηνότερες επιλογές:\n- Εταιρεία Α, 07:00, 5 ώρες, 40 €\n"
        "- Εταιρεία Β, 08:30, 4 ώρες, 45 €\n- Εταιρεία Γ, 12:00, 6 ώρες, 47 €\n"
        "Σελίδα αποτελεσμάτων: https://ferries.example/results?from=a&to=b"
    )
    record_dir = _record(tmp_path, {}, reply)
    statuses = _statuses(score.score_record(task, record_dir))
    assert statuses == {
        "finished": "pass",
        "reply_present": "pass",
        "min_items": "pass",
        "links_are_web": "pass",
        "min_sources": "pass",
        "reply_script": "pass",
    }


def test_runs_stopped_on_a_card_or_timed_out_do_not_finish(tmp_path):
    task = _task(tmp_path)
    record_dir = _record(tmp_path, {"σύνολα.xlsx": _good_workbook()}, "", status="blocked")
    result = score.score_record(task, record_dir)
    assert _statuses(result)["finished"] == "fail"
    assert not result["deterministic"]["all_passed"]


def test_the_judge_sees_the_task_reply_files_and_receipts(tmp_path):
    prompts = judge.verify()
    config = judge.JudgeConfig(url="https://judge.example/v1", key="k", model="any-model")
    sent = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    def opener(request, timeout):
        sent["user"] = json.loads(request.data)["messages"][1]["content"]
        items = [
            {"id": item["id"], "score": 1, "reason": "partly"}
            for item in judge.rubric_items(prompts, "data_file")
        ]
        content = json.dumps({"items": items})
        return Response(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    task = _task(tmp_path)
    record_dir = _record(tmp_path, {"σύνολα.xlsx": _good_workbook()}, GREEK_REPLY)
    result = score.score_record(task, record_dir, judge=(prompts, config), opener=opener)
    assert result["judge"]["status"] == "ok" and result["judge"]["score"] == 0.5
    assert result["judge"]["digest"] == prompts.digest
    assert "numbers_match_data" in result["judge"]["items"]
    for expected in (task.prompt, "3.703,50", "Πάτρα", "deterministic_checks", "total revenue"):
        assert expected in sent["user"], expected
