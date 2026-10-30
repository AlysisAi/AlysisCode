from __future__ import annotations

import json
import re

import pytest

from alysis_code.agent import computer_use_completion
from alysis_code.agent.computer_use_completion import computer_use_completion_report
from alysis_code.agent.computer_use_tools import (
    COMPUTER_USE_SYSTEM_PROMPT,
    build_computer_use_tools,
)

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

LIMIT = 65_536


def _report(outcome: str = "blocked", summary: str = "The save button is disabled.") -> str:
    return json.dumps({"outcome": outcome, "summary": summary}, ensure_ascii=False)


def _undeclared(text: object) -> tuple[str, object, bool]:
    return ("incomplete", text, False)


def _sized(target: int, unit: str, *, fence: bool = False) -> str:
    """A valid report of exactly ``target`` UTF-8 bytes, padded with ``unit`` then ASCII."""

    def wrap(summary: str) -> str:
        body = _report("incomplete", summary)
        return f"```json\n{body}\n```" if fence else body

    room = target - len(wrap("x").encode("utf-8"))
    width = len(unit.encode("utf-8"))
    text = wrap("x" + unit * (room // width) + "x" * (room % width))
    assert len(text.encode("utf-8")) == target
    return text


def test_finish_guidance_uses_only_the_report_outcomes() -> None:
    paragraph = next(
        block
        for block in COMPUTER_USE_SYSTEM_PROMPT.split("\n\n")
        if block.startswith("Finish with the outcome supported by the evidence")
    )
    for outcome in ("completed_unverified", "incomplete", "blocked"):
        assert outcome in paragraph
    for retired in ("completed", "partial", "inconclusive", "verified_success"):
        assert not re.search(rf"\b{retired}\b", paragraph), retired
    assert "with the summary saying which and what remains" in paragraph
    assert "semantic/model assessment separately from deterministic fact checks" in paragraph
    assert "Never claim a host completion certificate for an arbitrary goal." in paragraph


def test_guidance_keeps_literal_controls_distinct_from_named_keys() -> None:
    tools = build_computer_use_tools(lambda _name, _arguments: {})
    text = tools["computer_action"].parameters["properties"]["text"]["description"]
    assert "Line breaks, tabs and other controls in text are typed literally, never as a" in text
    assert "Return or Tab key press, and a field may not accept them" in text
    assert "send Return or Tab as its own key action" in text
    assert "at most 5,000 UTF-16 code units per action" in text


@pytest.mark.parametrize(
    "text",
    [
        _report(),
        " \n\t" + _report() + "\r\n ",
        "```json\n" + _report() + "\n```",
        "```\n" + _report() + "\n```",
        "\n\n```json  \n" + json.dumps(json.loads(_report()), indent=2) + "\n  ```\n",
        "```json\r\n" + _report() + "\r\n```",
    ],
    ids=[
        "bare",
        "bare-json-whitespace",
        "json-fence",
        "plain-fence",
        "indented-fence",
        "crlf-fence",
    ],
)
def test_whole_message_object_or_one_entire_fence_is_declared(text: str) -> None:
    assert computer_use_completion_report(text) == ("blocked", "The save button is disabled.", True)


@pytest.mark.parametrize(
    "summary",
    [
        "Saved draft 7.",
        "Gespeichert: \u00e9t\u00e9 \u4e2d\u6587",
        "Done \U0001f600 \U0001f1e9\U0001f1ea",
        "Ran ```ls``` in the Terminal window.",
    ],
    ids=["ascii", "multibyte", "astral", "inline-backticks"],
)
def test_fenced_summaries_are_returned_exactly(summary: str) -> None:
    fenced = "```json\n" + _report("completed_unverified", summary) + "\n```"
    assert computer_use_completion_report(fenced) == ("completed_unverified", summary, True)


@pytest.mark.parametrize(
    "text",
    [
        "Here is my report:\n```json\n" + _report() + "\n```",
        "```json\n" + _report() + "\n```\nLet me know if you need more.",
        "```json\n" + _report() + "\n```\n```json\n" + _report() + "\n```",
        "```python\n" + _report() + "\n```",
        "```JSON\n" + _report() + "\n```",
        "~~~json\n" + _report() + "\n~~~",
        "```json " + _report() + "```",
        "```json\n" + _report() + "\n",
        "```json\n```json\n" + _report() + "\n```\n```",
        "The outcome is blocked. " + _report(),
        _report() + "\n" + _report(),
        _report() + _report(),
        json.dumps({"outcome": "blocked", "summary": "Stopped.", "evidence": "x"}),
        "```json\n"
        + json.dumps({"outcome": "blocked", "summary": "Stopped.", "extra": 1})
        + "\n```",
        "Task completed successfully.",
        "",
    ],
    ids=[
        "prose-before-fence",
        "prose-after-fence",
        "two-fences",
        "other-language",
        "uppercase-marker",
        "tilde-fence",
        "single-line-fence",
        "unclosed-fence",
        "nested-fence",
        "prose-before-object",
        "two-objects-lines",
        "two-objects",
        "extra-key",
        "fenced-extra-key",
        "plain-prose",
        "empty",
    ],
)
def test_prose_extra_blocks_other_languages_and_extra_keys_stay_undeclared(text: str) -> None:
    assert computer_use_completion_report(text) == _undeclared(text)


@pytest.mark.parametrize(
    "outcome",
    [
        "partial",
        "inconclusive",
        "completed",
        "verified_success",
        "success",
        "done",
        "Completed_Unverified",
        " blocked",
    ],
)
@pytest.mark.parametrize("fence", [False, True], ids=["bare", "fenced"])
def test_old_outcome_words_are_not_aliases(outcome: str, fence: bool) -> None:
    text = _report(outcome, "Only the first form was saved.")
    text = f"```json\n{text}\n```" if fence else text
    assert computer_use_completion_report(text) == _undeclared(text)


@pytest.mark.parametrize(
    "text",
    [
        '{"outcome": "blocked", "outcome": "completed_unverified", "summary": "Saved."}',
        '{"outcome": "completed_unverified", "summary": "Not saved.", "summary": "Saved."}',
        '```json\n{"outcome": "blocked", "summary": "a", "summary": "b"}\n```',
        '{"outcome": "blocked", "summary": "Stopped.", "x": NaN}',
        '{"outcome": "blocked", "summary": "Stopped.", "x": Infinity}',
        '{"outcome": "blocked", "summary": "Stopped.", "x": -Infinity}',
        "[" * 30_000 + "]" * 30_000,
        '{"a":' * 10_000 + "1" + "}" * 10_000,
        '```json\n{"outcome": "blocked", "summary": ' + "[" * 20_000 + "]" * 20_000 + "}\n```",
    ],
    ids=[
        "duplicate-outcome",
        "duplicate-summary",
        "fenced-duplicate",
        "nan",
        "infinity",
        "negative-infinity",
        "deep-arrays",
        "deep-objects",
        "fenced-deep",
    ],
)
def test_duplicate_keys_non_finite_constants_and_deep_nesting_are_refused(text: str) -> None:
    assert len(text.encode("utf-8")) <= LIMIT
    assert computer_use_completion_report(text) == _undeclared(text)


@pytest.mark.parametrize(
    "unit",
    ["x", "\u00e9", "\u4e2d", "\U0001f600"],
    ids=["ascii", "two-byte", "three-byte", "astral"],
)
@pytest.mark.parametrize("fence", [False, True], ids=["bare", "fenced"])
def test_size_bound_is_65536_utf8_bytes_checked_before_parsing(
    unit: str, fence: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_limit = _sized(LIMIT, unit, fence=fence)
    outcome, summary, declared = computer_use_completion_report(at_limit)
    assert (outcome, declared) == ("incomplete", True)
    assert (
        summary == json.loads(at_limit.removeprefix("```json\n").removesuffix("\n```"))["summary"]
    )

    over = _sized(LIMIT + 1, unit, fence=fence)

    class _NoParse:
        @staticmethod
        def loads(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("an oversized report reached the JSON parser")

    monkeypatch.setattr(computer_use_completion, "json", _NoParse)
    assert computer_use_completion_report(over) == _undeclared(over)


def test_character_bound_is_checked_before_encoding() -> None:
    class _NoEncode(str):
        def encode(self, *_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("the encoding was allocated before the length bound")

    huge = _NoEncode(_report(summary="x" * (4 * LIMIT)))
    assert computer_use_completion_report(huge) == _undeclared(huge)


@pytest.mark.parametrize(
    "text",
    [
        '{"outcome": "blocked", "summary": "Stopped \ud800 here."}',
        '{"outcome": "blocked", "summary": "Stopped \udfff."}',
        '{"outcome": "blocked", "summary": "Stopped \\ud800 here."}',
        '{"outcome": "blocked", "summary": "Stopped \\ude00\\ud83d."}',
        '```json\n{"outcome": "blocked", "summary": "Stopped \\udbff"}\n```',
        "Prose with a lone \ud83d surrogate.",
    ],
    ids=["raw-high", "raw-low", "escaped-lone", "escaped-reversed", "fenced-escaped", "prose"],
)
def test_unpaired_surrogates_are_refused_deterministically(text: str) -> None:
    assert computer_use_completion_report(text) == _undeclared(text)
    assert computer_use_completion_report(text) == _undeclared(text)


def test_valid_pairs_and_unnormalized_text_are_preserved() -> None:
    escaped = '{"outcome": "blocked", "summary": "Paused \\ud83d\\ude00 here."}'
    assert computer_use_completion_report(escaped) == ("blocked", "Paused \U0001f600 here.", True)
    decomposed = "Cafe\u0301 and \u212b"
    assert computer_use_completion_report(_report(summary=decomposed)) == (
        "blocked",
        decomposed,
        True,
    )


def test_existing_report_semantics_are_unchanged() -> None:
    assert computer_use_completion_report(_report("incomplete", "  Saved one of two.  ")) == (
        "incomplete",
        "Saved one of two.",
        True,
    )
    assert computer_use_completion_report(
        _report("incomplete", "Task completed successfully, all verified.")
    ) == ("incomplete", "Task completed successfully, all verified.", True)
    for rejected in (
        _report("blocked", "   "),
        json.dumps({"outcome": 1, "summary": "x"}),
        json.dumps({"outcome": "blocked", "summary": None}),
        json.dumps({"outcome": "blocked"}),
        json.dumps([{"outcome": "blocked", "summary": "x"}]),
        "\u00a0" + _report(),
        _report() + "\u2028",
    ):
        assert computer_use_completion_report(rejected) == _undeclared(rejected)
    assert computer_use_completion_report(None) == _undeclared(None)  # type: ignore[arg-type]
