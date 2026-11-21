"""The shipped owls benchmark task set passes its schema and composition rules."""

from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.qa.owls_bench import tasks as task_files
from scripts.qa.owls_bench.documents import inspect_file
from scripts.qa.owls_bench.language import expected_scripts


@pytest.fixture(scope="module")
def shipped():
    return task_files.load_tasks()


def test_every_shipped_task_passes_the_schema_check(shipped):
    assert len(shipped) == 24
    assert task_files.check_task_set(shipped) == []


def test_eight_categories_of_three(shipped):
    assert Counter(task.category for task in shipped) == dict.fromkeys(task_files.CATEGORIES, 3)


def test_at_least_a_third_are_not_in_english(shipped):
    other = [task for task in shipped if task.primary_language != "en"]
    assert len(other) * 3 >= len(shipped)
    # Several scripts, not only Latin, so the script check is exercised for real.
    scripts = {expected_scripts(task.expect.reply_language)[0] for task in shipped}
    assert {"Latin", "Greek", "Han"} <= scripts


def test_each_category_is_split_between_dev_and_heldout(shipped):
    splits = defaultdict(Counter)
    for task in shipped:
        splits[task.category][task.split] += 1
    for category, counts in splits.items():
        assert counts["dev"] >= 1 and counts["heldout"] >= 1, category
        assert abs(counts["dev"] - counts["heldout"]) <= 1, category
    overall = Counter(task.split for task in shipped)
    assert overall["dev"] == overall["heldout"] == 12


def test_category_rules_hold(shipped):
    for task in shipped:
        if task.category == "data_file":
            assert task.attachments and task.expect.chart and task.expect.values
        if task.category == "repeat_job":
            assert task.expect.repeat >= 2
        if task.category == "long_job":
            assert task.expect.min_sources >= 20
        if task.category == "website_task":
            assert task.expect.deliverable == "none"
        if task.category == "deck":
            assert task.expect.min_slides == task.expect.max_slides


def test_some_attachments_hold_non_ascii_text(shipped):
    attached = {path for task in shipped for path in task.attachments}
    assert len(attached) == 3
    texts = [inspect_file(path).text for path in attached]
    assert all(text and any(ord(ch) > 127 for ch in text) for text in texts)


def _column(path: Path, key: int, value: int) -> dict[str, Decimal]:
    """Sums of one column grouped by another, read with the documents reader."""
    table = inspect_file(path).tables[0]
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for row in table.nonempty_rows()[1:]:
        cell = row[value]
        number = Decimal(cell.text.replace(",", ".")) if cell.number is None else Decimal(cell.text)
        sums[row[key].text] += number
    return sums


def test_expected_values_match_the_attached_data(shipped):
    by_id = {task.id: task for task in shipped}
    sales = by_id["data-el-store-sales"]
    per_store = _column(sales.attachments[0], 1, 4)
    assert [v.value for v in sales.expect.values] == [
        float(sum(per_store.values())),
        float(max(per_store.values())),
    ]
    tickets = by_id["data-en-support-tickets"]
    per_category = _column(tickets.attachments[0], 1, 2)
    assert [v.value for v in tickets.expect.values] == [
        float(sum(per_category.values())),
        float(max(per_category.values())),
    ]
    rainfall = by_id["data-zh-city-rainfall"]
    per_city = _column(rainfall.attachments[0], 0, 2)
    assert [v.value for v in rainfall.expect.values] == [
        float(min(per_city.values())),
        float(max(per_city.values())),
        float(sum(per_city.values())),
    ]


def _write(folder: Path, tasks: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "set.json").write_text(json.dumps({"schema": 1, "tasks": tasks}), encoding="utf-8")


def _valid(**overrides) -> dict:
    task = {
        "id": "a-task",
        "category": "deck",
        "language": "es",
        "split": "dev",
        "prompt": "Haz una presentación de 3 diapositivas.",
        "expect": {
            "deliverable": "pptx",
            "min_slides": 3,
            "max_slides": 3,
            "reply_language": "es",
        },
    }
    task.update(overrides)
    return task


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"surprise": 1}, "unknown key"),
        ({"language": "Spanish!"}, "BCP-47"),
        ({"split": "test"}, "dev or heldout"),
        ({"category": "misc"}, "category must be"),
        ({"prompt": ""}, "prompt must be"),
        (
            {"expect": {"deliverable": "xlsx", "min_slides": 3, "reply_language": "es"}},
            "slide counts",
        ),
        (
            {"expect": {"deliverable": "pptx", "repeat": 2, "reply_language": "es"}},
            "only for repeat_job",
        ),
        (
            {"category": "long_job", "expect": {"deliverable": "docx", "reply_language": "es"}},
            "long_job must need",
        ),
        ({"attachments": ["../outside.csv"]}, "inside the task folder"),
        ({"attachments": ["missing.csv"]}, "does not exist"),
        (
            {
                "expect": {
                    "deliverable": "pptx",
                    "reply_language": "es",
                    "values": [{"meaning": "x", "value": "1"}],
                }
            },
            "values[0]",
        ),
    ],
)
def test_schema_rejects_bad_tasks(tmp_path, change, problem):
    _write(tmp_path, [_valid(**change)])
    with pytest.raises(task_files.TaskError) as error:
        task_files.load_tasks(tmp_path)
    assert any(problem in item for item in error.value.problems), error.value.problems


def test_schema_accepts_one_task_per_file_and_rejects_duplicate_ids(tmp_path):
    (tmp_path / "one.json").write_text(
        json.dumps({"schema": 1, "task": _valid()}), encoding="utf-8"
    )
    assert [task.id for task in task_files.load_tasks(tmp_path)] == ["a-task"]
    (tmp_path / "two.json").write_text(
        json.dumps({"schema": 1, "task": _valid()}), encoding="utf-8"
    )
    with pytest.raises(task_files.TaskError, match="more than once"):
        task_files.load_tasks(tmp_path)


def test_set_check_catches_an_uneven_split(tmp_path):
    folder = tmp_path / "tasks"
    shutil.copytree(task_files.TASKS_DIR, folder)
    path = folder / "deck.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for task in document["tasks"]:
        task["split"] = "dev"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    problems = task_files.check_task_set(task_files.load_tasks(folder))
    assert any("deck needs both" in problem for problem in problems)


def test_select_by_split_and_ids(shipped):
    dev = task_files.select(shipped, split="dev")
    assert {task.split for task in dev} == {"dev"} and len(dev) == 12
    assert len(task_files.select(shipped, split="all")) == 24
    only = task_files.select(shipped, split="all", only=["deck-zh-solar-terms"])
    assert [task.id for task in only] == ["deck-zh-solar-terms"]
    with pytest.raises(ValueError, match="unknown task ids"):
        task_files.select(shipped, only=["no-such-task"])


def test_task_hash_changes_when_a_task_changes(tmp_path):
    _write(tmp_path, [_valid()])
    before = task_files.load_tasks(tmp_path)[0].sha256
    _write(tmp_path, [_valid(prompt="Otra cosa.")])
    assert task_files.load_tasks(tmp_path)[0].sha256 != before
