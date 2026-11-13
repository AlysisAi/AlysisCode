"""What compaction removes first and what it keeps word for word.

Neither is decided by the vocabulary of a message. The oldest turns are
summarised first, by position. What the user's side of the conversation said is
pinned by role, in any language. What matters in the rest is the summarising
model's judgement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_conversation_compaction import (
    _compaction_session_dir,
    _FakeCompactorClient,
    _make_cfg,
    _summary,
)

from alysis_code.agent.llm_calls import TOOL_CONTEXT_MESSAGE_KEY
from alysis_code.agent.steering import build_steer_messages
from alysis_code.agent_loop import AgentSession, create_session
from alysis_code.compaction.conversation_compactor import PINS_MARKER, USER_MESSAGE_PIN_KIND
from alysis_code.compaction.spans import HOST_CONTEXT_KEYS

# The same four kinds of message. The first three state a rule the work must
# keep; only the English one contains the words the old scoring looked for.
RULE_EN = "You MUST NOT change the public API. Acceptance criteria: pytest -q passes."
RULE_GR = "Δεν πρέπει να αλλάξεις το δημόσιο API. Όλοι οι έλεγχοι πρέπει να περνούν."
RULE_ZH = "绝对不要修改公共接口。所有测试都必须通过。"
SMALL_TALK = "ok thanks, sounds good"
# Long enough that summarising a turn frees more than its pin and summary cost.
PADDING = " " + ("x" * 6000)


def _session(
    tmp_path: Path, name: str, *, profile: str | None = None, **compaction: Any
) -> AgentSession:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"].update({"recent_user_turns_to_keep": 1, **compaction})
    extra = {"compaction_profile": profile} if profile else {}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "logs",
        session_id_override=name,
        **extra,
    )
    assert session.conversation_compactor is not None
    session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
        [_summary(decisions=["d1"])]
    )
    return session


def _compact(session: AgentSession) -> list[dict[str, Any]]:
    assert session.conversation_compactor is not None
    compacted, changed = session.conversation_compactor.compact_now(
        messages=session.messages,
        tool_list=session.tool_list,
        main_model=session.client.model,
        focus=None,
    )
    assert changed is True
    return compacted


def _pins(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for msg in messages:
        content = str(msg.get("content") or "")
        if msg.get("role") == "user" and content.startswith(PINS_MARKER):
            return json.loads(content.split("\n", 1)[1])["pins"]
    return []


def _first_chunk(session: AgentSession) -> str:
    chunk = _compaction_session_dir(session) / "history" / "chunk_0001.jsonl"
    return chunk.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "first,second",
    [
        (RULE_EN, SMALL_TALK),
        (SMALL_TALK, RULE_EN),
        (RULE_GR, SMALL_TALK),
        (SMALL_TALK, RULE_GR),
        (RULE_ZH, RULE_EN),
    ],
)
def test_the_oldest_turn_goes_first_whatever_it_says(tmp_path: Path, first, second) -> None:
    session = _session(tmp_path, "oldest-first", max_chunk_messages=2)
    try:
        session.messages.extend(
            [
                {"role": "user", "content": first + PADDING},
                {"role": "assistant", "content": "first reply"},
                {"role": "user", "content": second + PADDING},
                {"role": "assistant", "content": "second reply"},
                {"role": "user", "content": "latest request" + PADDING},
                {"role": "assistant", "content": "latest reply"},
            ]
        )
        compacted = _compact(session)
        archived = json.dumps(
            [json.loads(line) for line in _first_chunk(session).splitlines() if line.strip()],
            ensure_ascii=False,
        )
        kept = json.dumps(compacted, ensure_ascii=False)
    finally:
        session.close()
    assert "first reply" in archived and "second reply" not in archived
    assert "latest reply" in kept


@pytest.mark.parametrize("said", [RULE_EN, RULE_GR, RULE_ZH, SMALL_TALK])
def test_what_the_person_wrote_is_pinned_in_any_language(tmp_path: Path, said) -> None:
    session = _session(tmp_path, "pinned-by-role")
    try:
        session.messages.extend(
            [
                {"role": "user", "content": said + PADDING},
                # Words the old scoring rewarded, written by the model and a tool.
                {"role": "assistant", "content": "MUST never fail: error, traceback, pytest -q"},
                {"role": "user", "content": "latest request" + PADDING},
                {"role": "assistant", "content": "latest reply"},
            ]
        )
        pins = _pins(_compact(session))
    finally:
        session.close()
    assert [pin["kind"] for pin in pins] == [USER_MESSAGE_PIN_KIND]
    assert pins[0]["text"].startswith(said)
    assert set(pins[0]) == {"kind", "text", "source"}
    assert pins[0]["source"]["idx_range"][0] == pins[0]["source"]["idx_range"][1]


def test_a_long_run_pins_nothing_the_person_did_not_write(tmp_path: Path) -> None:
    """Output is not pinned for containing the word "error"; the summary carries it."""
    session = _session(
        tmp_path,
        "execution-pins",
        profile="execution",
        max_chunk_messages=8,
        execution_min_removable_messages=1,
        execution_min_removable_tokens=1,
    )
    try:
        session.messages.append({"role": "user", "content": "Implement the task safely."})
        noisy = "Traceback: error, failed. MUST fix. pytest -q " + ("e" * 6000)
        session.messages.append({"role": "assistant", "content": noisy})
        for idx in range(8):
            session.messages.append({"role": "assistant", "content": f"recent step {idx}"})
        compacted = _compact(session)
        archived = _first_chunk(session)
    finally:
        session.close()
    assert _pins(compacted) == []
    assert "Traceback: error, failed." in archived
    # The person's request is still there in full, ahead of the summary.
    assert any(m.get("content") == "Implement the task safely." for m in compacted)


PICTURE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def _tool_view(label: str) -> dict[str, Any]:
    """A picture a tool returned, as the engine adds it: user role, marked as its own."""
    return {
        "role": "user",
        TOOL_CONTEXT_MESSAGE_KEY: True,
        "content": [PICTURE, {"type": "text", "text": f"Browser view. MUST read: {label}"}],
    }


def test_what_the_host_puts_in_the_user_role_is_not_pinned(tmp_path: Path) -> None:
    assert TOOL_CONTEXT_MESSAGE_KEY in HOST_CONTEXT_KEYS
    session = _session(tmp_path, "pins-not-said")
    try:
        session.messages.extend(
            [
                {"role": "user", "content": [{"type": "text", "text": RULE_GR}, PICTURE]},
                {"role": "assistant", "content": "first reply " + PADDING},
                {"role": "user", "content": "receipt data", "_memory_evidence_context": True},
                {"role": "user", "content": [PICTURE]},
                {"role": "assistant", "content": "second reply " + PADDING},
                {"role": "user", "content": "latest request" + PADDING},
                {"role": "assistant", "content": "latest reply"},
                _tool_view("page title from the web"),
                {"role": "assistant", "content": "looked at the page"},
            ]
        )
        pins = _pins(_compact(session))
    finally:
        session.close()
    assert [pin["text"] for pin in pins] == [RULE_GR]


@pytest.mark.parametrize("typed", [RULE_EN, RULE_GR, RULE_ZH])
def test_the_typed_text_is_pinned_not_the_apps_wrapper(tmp_path: Path, typed) -> None:
    """The desktop app sends its own context ahead of what the person typed."""
    wrapped = (
        "[Alysis Desktop context - not typed by the user]\n"
        "Capability guidance the app adds to every message. " + ("g" * 900) + "\n\n"
        "User message:\n" + typed
    )
    session = _session(tmp_path, "pins-typed-text")
    try:
        session.messages.extend(
            [
                {"role": "user", "content": wrapped},
                {"role": "assistant", "content": "first reply " + PADDING},
                {"role": "user", "content": "latest request" + PADDING},
                {"role": "assistant", "content": "latest reply"},
            ]
        )
        pins = _pins(_compact(session))
    finally:
        session.close()
    assert [pin["text"] for pin in pins] == [typed]


def _long_running_turn(session: AgentSession, request: str, *, steer: bool) -> None:
    """One request, then many steps that each return a picture."""
    session.messages.append({"role": "user", "content": request})
    for step in range(12):
        call = {"id": f"c{step}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
        session.messages.extend(
            [
                {
                    "role": "assistant",
                    "content": f"step {step} " + "z" * 2500,
                    "tool_calls": [call],
                },
                {"role": "tool", "tool_call_id": f"c{step}", "content": "result " + "r" * 2500},
                _tool_view(f"page {step}"),
            ]
        )
        if steer and step == 0:
            session.messages.extend(build_steer_messages(["also keep the changelog updated"]))


@pytest.mark.parametrize("steer", [False, True])
@pytest.mark.parametrize("request_text", [RULE_EN + PADDING, RULE_GR + PADDING])
def test_the_request_being_worked_on_is_never_summarised(
    tmp_path: Path, request_text, steer
) -> None:
    """Pictures a tool returns do not push the running turn's request out of context."""
    session = _session(tmp_path, "active-request", recent_user_turns_to_keep=8)
    try:
        _long_running_turn(session, request_text, steer=steer)
        compacted = _compact(session)
        archived = _first_chunk(session)
    finally:
        session.close()
    assert any(m.get("content") == request_text for m in compacted)
    assert request_text[:60] not in archived
    # What went instead is the turn's own older tool context.
    assert "page 1" in archived
    said = "[Mid-turn message from the user] also keep the changelog updated"
    # What the user's side added mid-turn went with that context and is pinned whole.
    assert [pin["text"] for pin in _pins(compacted)] == ([said] if steer else [])


@pytest.mark.parametrize("request_text", [RULE_EN + PADDING, RULE_GR + PADDING])
def test_a_hooks_lines_are_not_taken_for_the_request(tmp_path: Path, request_text) -> None:
    """A hook that adds a user-role line after every tool call does not displace the request."""
    session = _session(tmp_path, "hook-lines", recent_user_turns_to_keep=8)
    try:
        session.messages.append({"role": "user", "content": request_text})
        for step in range(12):
            call = {
                "id": f"c{step}",
                "type": "function",
                "function": {"name": "t", "arguments": "{}"},
            }
            session.messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": f"step {step} " + "z" * 2500,
                        "tool_calls": [call],
                    },
                    {"role": "tool", "tool_call_id": f"c{step}", "content": "result " + "r" * 2500},
                ]
            )
            session._append_hook_messages(
                event_name="tool_hook_context", user_messages=[f"hook line {step}: MUST lint"]
            )
        hook_lines = [m for m in session.messages if "hook line" in str(m.get("content"))]
        compacted = _compact(session)
        archived = _first_chunk(session)
    finally:
        session.close()
    assert len(hook_lines) == 12 and all(m.get(TOOL_CONTEXT_MESSAGE_KEY) for m in hook_lines)
    assert any(m.get("content") == request_text for m in compacted)
    assert request_text[:60] not in archived and "hook line 0:" in archived
    # A chunk can end at a completed tool pair. The following hook context
    # belongs to the next unit and remains live until that unit is archived.
    kept = json.dumps(compacted, ensure_ascii=False)
    for line in hook_lines:
        text = line["content"]
        assert (text in archived) != (text in kept)
    assert _pins(compacted) == []


def test_an_overflow_removes_the_turns_activity_before_its_request(tmp_path: Path) -> None:
    session = _session(tmp_path, "overflow-activity-first", recent_user_turns_to_keep=8)
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        request = RULE_GR + PADDING
        session.messages.extend(
            [
                {"role": "user", "content": request},
                {"role": "assistant", "content": "worked on it " + ("work " * 3000)},
                _tool_view("only view"),
                {"role": "assistant", "content": "looked"},
            ]
        )
        untouched, changed_normally = compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus=None,
        )
        compacted, changed_on_overflow = compactor.compact_for_overflow(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
        )
        archived = _first_chunk(session)
    finally:
        session.close()
    # Everything is inside the recent tail, and nothing forces it to be given up.
    assert changed_normally is False and untouched == session.messages
    # The provider says it does not fit: the tail goes, the request stays.
    assert changed_on_overflow is True and "worked on it" in archived
    assert any(m.get("content") == request for m in compacted)
    assert _pins(compacted) == []


def test_the_request_itself_goes_only_when_an_overflow_leaves_nothing_else(tmp_path: Path) -> None:
    session = _session(tmp_path, "overflow-last-resort", recent_user_turns_to_keep=8)
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        request = RULE_GR + PADDING
        session.messages.append({"role": "user", "content": request})
        _, changed_normally = compactor.compact_now(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
            focus=None,
        )
        compacted, changed_on_overflow = compactor.compact_for_overflow(
            messages=session.messages,
            tool_list=session.tool_list,
            main_model=session.client.model,
        )
    finally:
        session.close()
    assert changed_normally is False
    assert changed_on_overflow is True
    assert not any(m.get("content") == request for m in compacted)
    assert [pin["text"][: len(RULE_GR)] for pin in _pins(compacted)] == [RULE_GR]


def test_an_earlier_request_goes_before_the_running_turns_context(tmp_path: Path) -> None:
    session = _session(tmp_path, "older-turn-first", recent_user_turns_to_keep=8)
    try:
        session.messages.extend(
            [
                {"role": "user", "content": RULE_GR + PADDING},
                {"role": "assistant", "content": "worked on the earlier request " + "w " * 4000},
                {"role": "assistant", "content": "finished the earlier request"},
            ]
        )
        _long_running_turn(session, "current request" + PADDING, steer=False)
        compacted = _compact(session)
        archived = _first_chunk(session)
    finally:
        session.close()
    assert "finished the earlier request" in archived and "page 1" not in archived
    assert [pin["text"][: len(RULE_GR)] for pin in _pins(compacted)] == [RULE_GR]
    assert any(m.get("content") == "current request" + PADDING for m in compacted)


def test_pins_give_way_when_they_would_cancel_what_a_step_frees(tmp_path: Path) -> None:
    """Messages with one-word replies: pinning every one would leave nothing gained."""
    said = "Please keep the billing module stable and do not rename any public function. "
    session = _session(tmp_path, "pins-yield")
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        for turn in range(6):
            session.messages.extend(
                [
                    {"role": "user", "content": f"Rule {turn}. " + said * 6},
                    {"role": "assistant", "content": "ok"},
                ]
            )
        session.messages.extend(
            [
                {"role": "user", "content": "latest request"},
                {"role": "assistant", "content": "latest reply"},
            ]
        )
        compacted = _compact(session)
        calls = compactor.compactor_client.calls  # type: ignore[attr-defined]
        archived = _first_chunk(session)
        updates = [
            event["payload"]
            for event in map(json.loads, session.store.path.read_text("utf-8").splitlines())
            if event.get("type") == "conversation_summary_updated"
        ]
    finally:
        session.close()
    assert "Rule 0." in archived and "Rule 5." in archived
    assert calls == 1  # the summary is asked for once, however the pins are settled
    pinned = [pin["text"].split(".")[0] for pin in _pins(compacted)]
    # The older ones were left to the summary; the newest are kept, in order.
    assert 0 < len(pinned) < 6
    assert pinned == [f"Rule {turn}" for turn in range(6)][-len(pinned) :]
    assert updates[0]["pins_left_to_summary"] == 6 - len(pinned)


@pytest.mark.parametrize("said", [RULE_EN, RULE_GR, RULE_ZH])
def test_a_pin_keeps_the_same_amount_of_text_in_every_script(tmp_path: Path, said) -> None:
    long_message = (said + " ") * 40
    session = _session(tmp_path, "pin-length")
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        limit = compactor._settings.pin_snippet_chars
        session.messages.extend(
            [
                {"role": "user", "content": long_message},
                {"role": "assistant", "content": "reply " + "r" * 9000},
                {"role": "user", "content": "latest request"},
                {"role": "assistant", "content": "latest reply"},
            ]
        )
        pins = _pins(_compact(session))
    finally:
        session.close()
    assert len(pins) == 1 and pins[0]["text"].startswith(said)
    assert limit <= len(pins[0]["text"]) <= limit + 3  # cut at the limit, then "..."


@pytest.mark.parametrize("said", [RULE_EN, RULE_GR, RULE_ZH])
def test_pins_take_a_bounded_share_of_the_models_input_budget(tmp_path: Path, said) -> None:
    """Text that costs many tokens cannot crowd a small window; a large one is unaffected."""
    from alysis_code.token_budget import estimate_tokens

    session = _session(tmp_path, "pin-share")
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        pins = [
            {
                "kind": USER_MESSAGE_PIN_KIND,
                "text": f"{index}. " + (said + " ") * 12,
                "source": {"history_path": "history/chunk_0001.jsonl", "idx_range": [index, index]},
            }
            for index in range(20)
        ]
        settings = compactor._settings
        by_characters = compactor._bounded_pins(pins)
        large_window = compactor._bounded_pins(pins, budget_tokens=1_000_000)
        small_window = compactor._bounded_pins(pins, budget_tokens=32_000)
        payload = json.dumps(
            {"pins": [compactor._public_pin(pin) for pin in small_window]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    finally:
        session.close()
    assert large_window == by_characters and by_characters
    assert small_window and small_window == by_characters[-len(small_window) :]
    # A twentieth of the budget, or what the character limit costs in English.
    assert estimate_tokens(payload) <= max(32_000 // 20, settings.max_pins_chars // 4)
    if said == RULE_EN:
        assert small_window == by_characters  # English is bounded by characters alone


def test_the_newest_pins_are_kept_when_a_limit_is_reached(tmp_path: Path) -> None:
    session = _session(tmp_path, "pin-limits", max_pins=2)
    try:
        for label in ("first", "second", "third"):
            session.messages.extend(
                [
                    {"role": "user", "content": f"{label} request" + PADDING},
                    {"role": "assistant", "content": f"{label} reply"},
                ]
            )
        session.messages.extend(
            [
                {"role": "user", "content": "latest request" + PADDING},
                {"role": "assistant", "content": "latest reply"},
            ]
        )
        pins = _pins(_compact(session))
        compactor = session.conversation_compactor
        assert compactor is not None
        # The size limit is applied the same way: the oldest goes first.
        one_pin = len(json.dumps({"pins": [pins[-1]]}, ensure_ascii=False, separators=(",", ":")))
        object.__setattr__(compactor._settings, "max_pins_chars", one_pin + 10)
        by_size = compactor._bounded_pins(pins)
    finally:
        session.close()
    assert [pin["text"].split(" ")[0] for pin in pins] == ["second", "third"]
    assert [pin["text"].split(" ")[0] for pin in by_size] == ["third"]


def test_a_repeated_message_is_pinned_once_at_its_latest_place(tmp_path: Path) -> None:
    session = _session(tmp_path, "pin-repeat")
    try:
        for said in ("continue", "use the staging database", "Continue"):
            session.messages.extend(
                [
                    {"role": "user", "content": said},
                    {"role": "assistant", "content": "working " + "w" * 1600},
                ]
            )
        session.messages.extend(
            [
                {"role": "user", "content": "latest request" + PADDING},
                {"role": "assistant", "content": "latest reply"},
            ]
        )
        pins = _pins(_compact(session))
    finally:
        session.close()
    assert [pin["text"] for pin in pins] == ["use the staging database", "Continue"]


def test_pins_saved_by_the_old_scoring_still_load(tmp_path: Path) -> None:
    session = _session(tmp_path, "legacy-pins")
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        legacy = [
            {
                "kind": "constraint",
                "text": "newer saved excerpt",
                "reasons": ["requirements_or_constraints"],
                "score": 9.5,
                "source": {"history_path": "history/chunk_0002.jsonl", "idx_range": [4, 9]},
            },
            {"kind": "error", "text": "older saved excerpt", "reasons": ["errors_or_failures"]},
            {"kind": "context", "text": "  "},
        ]
        kept = compactor._bounded_pins(legacy)
        shown = [compactor._public_pin(pin) for pin in kept]
    finally:
        session.close()
    # Ordered by where they were said, not by the stored score; no reasons shown.
    assert [pin["text"] for pin in shown] == ["older saved excerpt", "newer saved excerpt"]
    assert all(set(pin) == {"kind", "text", "source"} for pin in shown)
