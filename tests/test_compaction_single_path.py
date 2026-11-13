"""One way of cutting old activity, for a run and for the turn in progress of a chat.

Whole tool exchanges are removed oldest first. The request and a recent tail
stay. A picture or hook line the host adds in the user role does not stop it,
and the tail is sized to the model's input budget and given up only when that
is needed to get under a limit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_conversation_compaction import (
    _assert_valid_tool_transcript,
    _compaction_session_dir,
    _FakeCompactorClient,
    _make_cfg,
    _summary,
)

from alysis_code.agent.llm_calls import (
    HOST_MESSAGE_KEYS,
    TOOL_CONTEXT_MESSAGE_KEY,
    TURN_SETUP_MESSAGE_KEY,
    _request_messages_with_volatile_suffix,
    without_host_message_keys,
)
from alysis_code.agent.steering import build_steer_messages
from alysis_code.agent_loop import AgentSession, create_session
from alysis_code.compaction import spans
from alysis_code.compaction.conversation_compactor import (
    _RECENT_TAIL_BUDGET_SHARE,
    PINS_MARKER,
    _ExecutionBundle,
)
from alysis_code.compaction.spans import TURN_SETUP_KEY, TurnSpan
from alysis_code.request_estimation import estimate_request_tokens

REQUEST = "Refactor the billing module and keep every test passing."


def _session(tmp_path: Path, profile: str, **compaction: Any) -> AgentSession:
    cfg = _make_cfg()
    cfg.extra_fields["compaction"].update({"recent_user_turns_to_keep": 8, **compaction})
    extra = {"compaction_profile": profile} if profile != "chat" else {}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "logs",
        session_id_override=f"single-path-{profile}",
        **extra,
    )
    assert session.conversation_compactor is not None
    session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
        [_summary(decisions=["d"])]
    )
    return session


def _step(index: int, *, size: int = 5000) -> list[dict[str, Any]]:
    call = {"id": f"c{index}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    return [
        {"role": "assistant", "content": f"step {index}", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": f"c{index}", "content": f"result {index} " + "r" * size},
    ]


def _picture(index: int) -> dict[str, Any]:
    return {
        "role": "user",
        TOOL_CONTEXT_MESSAGE_KEY: True,
        "content": [{"type": "text", "text": f"view {index}"}],
    }


def _compact(session: AgentSession, how: str) -> tuple[list[dict[str, Any]], bool]:
    compactor = session.conversation_compactor
    assert compactor is not None
    kwargs: dict[str, Any] = {
        "messages": session.messages,
        "tool_list": session.tool_list,
        "main_model": session.client.model,
    }
    if how == "overflow":
        return compactor.compact_for_overflow(**kwargs)
    kwargs["focus"] = None
    return (compactor.maybe_compact if how == "auto" else compactor.compact_now)(**kwargs)


def _events(session: AgentSession, kind: str) -> list[dict[str, Any]]:
    return [
        event["payload"]
        for event in map(json.loads, session.store.path.read_text("utf-8").splitlines())
        if event.get("type") == kind
    ]


def _archived(session: AgentSession) -> str:
    history = _compaction_session_dir(session) / "history"
    return "\n".join(path.read_text("utf-8") for path in sorted(history.glob("chunk_*.jsonl")))


@pytest.mark.parametrize("how", ["auto", "overflow", "manual"])
@pytest.mark.parametrize("profile", ["chat", "execution"])
@pytest.mark.parametrize("pictures", [False, True])
def test_one_long_turn_is_compacted_in_a_chat_and_in_a_run(
    tmp_path: Path, profile, how, pictures
) -> None:
    """Neither a turn with no user-role message in it nor one with tool pictures is stuck."""
    session = _session(tmp_path, profile)
    try:
        session.messages.append({"role": "user", "content": REQUEST})
        for index in range(60):
            session.messages.extend(_step(index))
            if pictures and index % 5 == 0:
                session.messages.append(_picture(index))
        compacted, changed = _compact(session, how)
        archived = _archived(session)
    finally:
        session.close()
    assert changed is True
    _assert_valid_tool_transcript(compacted)
    assert any(m.get("content") == REQUEST for m in compacted)
    assert REQUEST not in archived
    assert "result 2 " in archived
    # The newest exchange is still there word for word.
    assert any(str(m.get("content", "")).startswith("result 59 ") for m in compacted)


def test_a_message_sent_mid_turn_goes_with_the_old_activity_and_is_pinned(tmp_path: Path) -> None:
    session = _session(tmp_path, "chat")
    try:
        session.messages.append({"role": "user", "content": REQUEST})
        session.messages.extend(_step(0))
        session.messages.extend(build_steer_messages(["also keep the changelog updated"]))
        for index in range(1, 40):
            session.messages.extend(_step(index))
        compacted, changed = _compact(session, "auto")
        pins = [
            json.loads(str(m["content"]).split("\n", 1)[1])["pins"]
            for m in compacted
            if str(m.get("content")).startswith(PINS_MARKER)
        ]
    finally:
        session.close()
    assert changed is True
    _assert_valid_tool_transcript(compacted)
    assert any(m.get("content") == REQUEST for m in compacted)
    assert [pin["text"] for pin in pins[0]] == [
        "[Mid-turn message from the user] also keep the changelog updated"
    ]


def test_a_later_request_in_a_run_is_kept_while_it_is_worked_on(tmp_path: Path) -> None:
    session = _session(tmp_path, "execution")
    later = "Now do the same for the invoices module."
    try:
        session.messages.append({"role": "user", "content": REQUEST})
        for index in range(20):
            session.messages.extend(_step(index))
        session.messages.append({"role": "user", "content": later})
        for index in range(20, 60):
            session.messages.extend(_step(index))
        compacted, changed = _compact(session, "overflow")
        strategies = [e["chunk_strategy"] for e in _events(session, "conversation_summary_updated")]
    finally:
        session.close()
    assert changed is True and len(strategies) >= 2
    _assert_valid_tool_transcript(compacted)
    kept = [m.get("content") for m in compacted if m.get("role") == "user"]
    assert REQUEST in kept and later in kept


def test_a_hook_line_inside_a_batch_of_calls_goes_with_that_batch(tmp_path: Path) -> None:
    """The cut never leaves a tool result without its call."""
    session = _session(tmp_path, "execution")
    try:
        session.messages.append({"role": "user", "content": REQUEST})
        for index in range(12):
            session.messages.extend(_step(index))
        calls = [
            {"id": "b1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            {"id": "b2", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]
        session.messages.extend(
            [
                {"role": "assistant", "content": "two calls", "tool_calls": calls},
                {"role": "tool", "tool_call_id": "b1", "content": "first " + "f" * 5000},
                {"role": "user", TOOL_CONTEXT_MESSAGE_KEY: True, "content": "hook: lint ok"},
                {"role": "tool", "tool_call_id": "b2", "content": "second " + "s" * 5000},
            ]
        )
        for index in range(12, 40):
            session.messages.extend(_step(index))
        compacted, changed = _compact(session, "overflow")
        archived = _archived(session)
    finally:
        session.close()
    assert changed is True
    _assert_valid_tool_transcript(compacted)
    # The batch went as a whole, its hook line with it.
    assert all(part in archived for part in ("two calls", "first ", "hook: lint ok", "second "))
    assert not any(m.get("content") in ("two calls", "hook: lint ok") for m in compacted)


def test_a_batch_with_a_hook_line_in_the_tail_does_not_block_the_cut(tmp_path: Path) -> None:
    session = _session(tmp_path, "execution")
    try:
        session.messages.append({"role": "user", "content": REQUEST})
        for index in range(40):
            session.messages.extend(_step(index))
        calls = [
            {"id": "b1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            {"id": "b2", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]
        session.messages.extend(
            [
                {"role": "assistant", "content": "two calls", "tool_calls": calls},
                {"role": "tool", "tool_call_id": "b1", "content": "first"},
                {"role": "user", TOOL_CONTEXT_MESSAGE_KEY: True, "content": "hook: lint ok"},
                {"role": "tool", "tool_call_id": "b2", "content": "second"},
            ]
        )
        compacted, changed = _compact(session, "auto")
        archived = _archived(session)
    finally:
        session.close()
    assert changed is True and "result 2 " in archived
    kept = [m.get("content") for m in compacted]
    assert kept[-4:] == ["two calls", "first", "hook: lint ok", "second"]


def _tail_start(
    session: AgentSession, sizes: list[int], *, budget: int | None, scale: float
) -> int:
    """Where the tail starts for one-message pieces of the given token sizes."""
    compactor = session.conversation_compactor
    assert compactor is not None
    bundles = [_ExecutionBundle(start=i, end=i + 1) for i in range(len(sizes))]
    spans = [TurnSpan(start=i, end=i + 1, token_estimate=size) for i, size in enumerate(sizes)]
    return compactor._execution_tail_bundle_index(
        bundles, spans, budget_tokens=budget, tail_scale=scale
    )


def test_the_tail_is_sized_to_the_models_input_budget(tmp_path: Path) -> None:
    session = _session(tmp_path, "execution", max_chunk_messages=60)  # at most 20 messages
    try:
        sizes = [1000] * 40
        by_count = _tail_start(session, sizes, budget=None, scale=1.0)
        large_window = _tail_start(session, sizes, budget=1_000_000, scale=1.0)
        small_window = _tail_start(session, sizes, budget=40_000, scale=1.0)
        halved = _tail_start(session, sizes, budget=40_000, scale=0.5)
        given_up = _tail_start(session, sizes, budget=40_000, scale=0.0)
        one_huge_piece = _tail_start(session, [1000] * 5 + [90_000], budget=40_000, scale=1.0)
    finally:
        session.close()
    assert by_count == large_window == 20  # twenty messages kept: a large window changes nothing
    assert 40 - small_window == int(40_000 * _RECENT_TAIL_BUDGET_SHARE) // 1000  # six of them
    assert 40 - halved == 3
    assert given_up == 40  # nothing kept
    assert one_huge_piece == 5  # the newest piece is kept even when it is over the share


def test_the_tail_is_given_up_only_when_that_is_needed(tmp_path: Path) -> None:
    session = _session(tmp_path, "execution")
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        messages = [{"role": "user", "content": REQUEST}]
        for index in range(6):  # fewer messages than the tail keeps
            messages.extend(_step(index, size=40_000))
        prefix = compactor.state.pinned_prefix_len
        messages = [*session.messages[:prefix], *messages]

        def tail_shares(**pressure: bool) -> list[float]:
            return [
                share
                for _plan, share, _prefix in compactor._candidate_plans(
                    messages,
                    protected_prefix_len=prefix,
                    budget_tokens=1_000_000,
                    tokens_to_free=None,
                    **pressure,
                )
            ]

        not_needed = tail_shares(relax=False, hard_pressure=False)
        fits_after_an_overflow = tail_shares(relax=False, hard_pressure=True)
        over_trigger = tail_shares(relax=True, hard_pressure=False)
        overflow = tail_shares(relax=True, hard_pressure=True)
    finally:
        session.close()
    assert not_needed == []  # everything is inside the tail: nothing is offered
    assert fits_after_an_overflow == []  # an overflow that was already relieved
    assert over_trigger == [0.5]  # over the trigger: half of the tail is offered
    assert overflow == [0.5, 0.0]  # after an overflow all of it, tried in that order


def test_a_cut_that_frees_nothing_is_followed_by_the_next_larger_one(tmp_path: Path) -> None:
    """The summary can cost more than a small cut frees; that must not end the attempt."""
    session = _session(tmp_path, "execution")
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        fat_summary = _summary(decisions=[f"decision {n} " + "detail " * 140 for n in range(10)])
        compactor.compactor_client = _FakeCompactorClient([fat_summary])  # type: ignore[assignment]
        session.messages.append({"role": "user", "content": REQUEST})
        for index in range(9):  # two steps lie outside the full tail (thirteen messages)
            call = {"id": f"c{index}", "type": "function", "function": {"name": "t"}}
            session.messages.extend(
                [
                    {"role": "assistant", "content": f"step {index}", "tool_calls": [call]},
                    {"role": "tool", "tool_call_id": f"c{index}", "content": "word " * 540},
                ]
            )
        compacted, changed = _compact(session, "overflow")
        calls = compactor.compactor_client.calls  # type: ignore[attr-defined]
        kept_tail = [
            event["recent_tail_share_kept"]
            for event in _events(session, "conversation_summary_updated")
        ]
        warnings = [e["warning"] for e in _events(session, "compaction_warning")]
    finally:
        session.close()
    assert "compaction_no_progress" in warnings  # the first, small cut
    assert changed is True and calls >= 2 and kept_tail and kept_tail[0] < 1.0
    _assert_valid_tool_transcript(compacted)


def test_an_overflow_stops_giving_up_the_tail_once_the_request_fits(tmp_path: Path) -> None:
    """After one step under the trigger the newest exchanges are still there."""
    session = _session(tmp_path, "execution")
    try:
        session.messages.append({"role": "user", "content": REQUEST})
        for index in range(60):
            session.messages.extend(_step(index, size=600))
        compacted, changed = _compact(session, "overflow")
        kept_tail = [
            event["recent_tail_share_kept"]
            for event in _events(session, "conversation_summary_updated")
        ]
    finally:
        session.close()
    assert changed is True and kept_tail and all(share == 1.0 for share in kept_tail)
    assert any(str(m.get("content", "")).startswith("result 59 ") for m in compacted)
    assert any(str(m.get("content", "")).startswith("result 52 ") for m in compacted)


def test_a_small_leftover_before_a_later_request_does_not_block_what_follows(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, "execution")
    later = "Now do the same for the invoices module."
    try:
        session.messages.extend(
            [
                {"role": "user", "content": REQUEST},
                {"role": "assistant", "content": "Done."},
                {"role": "user", "content": later},
            ]
        )
        for index in range(60):
            session.messages.extend(_step(index))
        compacted, changed = _compact(session, "auto")
        archived = _archived(session)
    finally:
        session.close()
    assert changed is True and "result 2 " in archived
    kept = [m.get("content") for m in compacted if m.get("role") == "user"]
    assert REQUEST in kept and later in kept


def test_a_chat_removes_older_turns_before_it_cuts_into_the_running_one(tmp_path: Path) -> None:
    """Three finished turns hold the bulk; the fourth is in progress."""
    session = _session(tmp_path, "chat")
    try:
        for turn in range(3):
            session.messages.append({"role": "user", "content": f"finished request {turn}"})
            for index in range(20):
                session.messages.extend(_step(turn * 100 + index))
        session.messages.append({"role": "user", "content": REQUEST})
        for index in range(900, 906):
            session.messages.extend(_step(index))
        compacted, changed = _compact(session, "auto")
        strategies = [e["chunk_strategy"] for e in _events(session, "conversation_summary_updated")]
        archived = _archived(session)
    finally:
        session.close()
    assert changed is True and strategies
    assert "finished request 0" in archived
    assert "result 900 " not in archived  # the running turn was not touched first
    assert any(m.get("content") == REQUEST for m in compacted)
    _assert_valid_tool_transcript(compacted)


def test_a_controller_note_inside_a_batch_goes_with_that_batch(tmp_path: Path) -> None:
    session = _session(tmp_path, "execution")
    try:
        session.messages.append({"role": "user", "content": REQUEST})
        calls = [
            {"id": "b1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            {"id": "b2", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]
        session.messages.extend(
            [
                {"role": "assistant", "content": "two calls", "tool_calls": calls},
                {"role": "tool", "tool_call_id": "b1", "content": "first " + "f" * 5000},
                {"role": "system", "content": "controller note about the first call"},
                {"role": "tool", "tool_call_id": "b2", "content": "second " + "s" * 5000},
            ]
        )
        for index in range(40):
            session.messages.extend(_step(index))
        compacted, changed = _compact(session, "auto")
        archived = _archived(session)
    finally:
        session.close()
    assert changed is True
    assert all(part in archived for part in ("two calls", "controller note", "second "))
    assert not any(m.get("content") == "two calls" for m in compacted)


def test_an_exchange_broken_in_an_earlier_turn_does_not_stop_the_cut(tmp_path: Path) -> None:
    """A turn that was stopped mid-call left a call without its result."""
    session = _session(tmp_path, "chat")
    try:
        stopped = {"id": "x1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
        session.messages.extend(
            [
                {"role": "user", "content": "an earlier request that was stopped"},
                {"role": "assistant", "content": "starting", "tool_calls": [stopped]},
                {"role": "user", "content": REQUEST},
            ]
        )
        for index in range(60):
            session.messages.extend(_step(index))
        compacted, changed = _compact(session, "auto")
        archived = _archived(session)
    finally:
        session.close()
    assert changed is True and "result 2 " in archived
    assert any(m.get("content") == REQUEST for m in compacted)


@pytest.mark.parametrize("call_id", ["call_0", ""])
@pytest.mark.parametrize("profile", ["chat", "execution"])
def test_reused_or_missing_call_ids_do_not_block_the_cut(tmp_path: Path, profile, call_id) -> None:
    """Some clients give every tool call the same id; results are told by their place."""
    session = _session(tmp_path, profile)
    try:
        session.messages.append({"role": "user", "content": REQUEST})
        for index in range(60):
            call = {"id": call_id, "type": "function", "function": {"name": "t", "arguments": "{}"}}
            session.messages.extend(
                [
                    {"role": "assistant", "content": f"step {index}", "tool_calls": [call]},
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": f"result {index} " + "r" * 5000,
                    },
                ]
            )
        compacted, changed = _compact(session, "auto")
        archived = _archived(session)
    finally:
        session.close()
    assert changed is True and "result 2 " in archived
    roles = [m.get("role") for m in compacted]
    # What is left still alternates call and result.
    assert all(roles[i + 1] == "tool" for i, m in enumerate(compacted) if m.get("tool_calls"))
    assert any(str(m.get("content", "")).startswith("result 59 ") for m in compacted)


def test_a_finished_turn_is_not_removed_whole_when_part_of_it_is_enough(tmp_path: Path) -> None:
    """A long finished turn, then a follow-up that has just started."""
    session = _session(tmp_path, "chat")
    try:
        session.messages.append({"role": "user", "content": "the first request"})
        for index in range(60):
            session.messages.extend(_step(index))
        session.messages.append({"role": "assistant", "content": "The first request is done."})
        session.messages.append({"role": "user", "content": REQUEST})
        compacted, changed = _compact(session, "auto")
        archived = _archived(session)
    finally:
        session.close()
    assert changed is True and "result 2 " in archived
    kept = [m.get("content") for m in compacted]
    assert "The first request is done." in kept and REQUEST in kept
    assert any(str(content).startswith("result 59 ") for content in kept)
    _assert_valid_tool_transcript(compacted)


@pytest.mark.parametrize("profile", ["chat", "execution"])
def test_the_notes_attached_to_a_request_stay_with_it(tmp_path: Path, profile) -> None:
    """The host writes the language to answer in right after the request."""
    directive = "The user explicitly requested a language override: answer in Greek."
    session = _session(tmp_path, profile)
    try:
        session.messages.extend(
            [
                {"role": "user", "content": REQUEST},
                {"role": "system", "content": directive, TURN_SETUP_MESSAGE_KEY: True},
                {"role": "system", "content": "a later controller note"},
            ]
        )
        for index in range(60):
            session.messages.extend(_step(index))
        compacted, changed = _compact(session, "overflow")
        archived = _archived(session)
        sent = _request_messages_with_volatile_suffix(messages=compacted)
    finally:
        session.close()
    assert TURN_SETUP_MESSAGE_KEY == TURN_SETUP_KEY
    assert changed is True and directive not in archived
    kept = [m.get("content") for m in compacted]
    assert kept.index(directive) == kept.index(REQUEST) + 1
    # A note that merely stands next to the request is ordinary activity.
    assert "a later controller note" in archived
    # The mark is the host's own and is not sent to a provider.
    assert not any(TURN_SETUP_MESSAGE_KEY in message for message in sent)


@pytest.mark.parametrize("profile", ["chat", "execution"])
def test_host_lines_do_not_pile_up_behind_the_request(tmp_path: Path, profile) -> None:
    """After a cut a picture can stand right after the request; it is still removable."""
    session = _session(tmp_path, profile)
    try:
        session.messages.append({"role": "user", "content": REQUEST})
        pictures_left: list[int] = []
        for round_number in range(6):
            for index in range(round_number * 40, round_number * 40 + 40):
                session.messages.extend(_step(index))
                session.messages.append(_picture(index))
            session.messages, changed = _compact(session, "auto")
            assert changed is True
            pictures_left.append(
                sum(1 for m in session.messages if m.get(TOOL_CONTEXT_MESSAGE_KEY))
            )
        kept = [m.get("content") for m in session.messages]
    finally:
        session.close()
    assert REQUEST in kept
    assert max(pictures_left) <= 20, pictures_left  # bounded by the tail, round after round


def test_the_hosts_own_marks_are_never_sent_to_a_provider(tmp_path: Path) -> None:
    assert HOST_MESSAGE_KEYS == spans.HOST_MESSAGE_KEYS
    marked = [
        {"role": "user", "content": REQUEST},
        {"role": "system", "content": "answer in Greek", TURN_SETUP_MESSAGE_KEY: True},
        _picture(1),
    ]
    assert not any(
        key in message for message in without_host_message_keys(marked) for key in HOST_MESSAGE_KEYS
    )
    assert TURN_SETUP_MESSAGE_KEY in marked[1]  # the conversation itself keeps them

    # A compaction started without a request builder (the app's manual one)
    # counts tokens on what a provider would be sent.
    session = _session(tmp_path, "chat")
    counted: list[dict[str, Any]] = []
    try:
        compactor = session.conversation_compactor
        assert compactor is not None

        def counter(messages: list[dict[str, Any]], _tools: Any) -> Any:
            counted.extend(messages)
            return None

        compactor._input_token_counter = counter
        session.messages.extend(marked)
        for index in range(40):
            session.messages.extend(_step(index))
            session.messages.append(_picture(index))
        _compact(session, "manual")
    finally:
        session.close()
    assert counted and not any(key in message for message in counted for key in HOST_MESSAGE_KEYS)


def test_a_later_request_and_its_notes_go_or_stay_together(tmp_path: Path) -> None:
    session = _session(tmp_path, "execution")
    later = "Now do the same for the invoices module."
    note = "note written for the later request"
    try:
        session.messages.append({"role": "user", "content": REQUEST})
        for index in range(10):
            session.messages.extend(_step(index))
        session.messages.extend(
            [
                {"role": "user", "content": later},
                {"role": "system", "content": note, TURN_SETUP_MESSAGE_KEY: True},
            ]
        )
        for index in range(10, 20):
            session.messages.extend(_step(index))
        session.messages.append({"role": "user", "content": "A third request, now in progress."})
        for index in range(20, 70):
            session.messages.extend(_step(index))
        compacted, changed = _compact(session, "overflow")
    finally:
        session.close()
    assert changed is True
    kept = [m.get("content") for m in compacted]
    assert (later in kept) == (note in kept)
    if later in kept:
        assert kept.index(note) == kept.index(later) + 1
    assert "A third request, now in progress." in kept and REQUEST in kept


def _small_window_session(tmp_path: Path, **compaction: Any) -> AgentSession:
    """A run on a model with a 40,000-token window, trigger 90% and target 70%."""
    cfg = _make_cfg()
    cfg.extra_fields["compaction"].update(
        {"trigger_ratio": 0.9, "target_ratio": 0.7, "max_chunk_messages": 400, **compaction}
    )
    cfg.extra_fields["model_metadata_overrides"] = {
        "models": {"gpt-5-nano": {"context_window_tokens": 40_000, "max_output_tokens": 2_000}}
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="x",
        session_log_dir_override=tmp_path / "logs",
        session_id_override="compaction-distance",
        compaction_profile="execution",
    )
    assert session.conversation_compactor is not None
    session.conversation_compactor.compactor_client = _FakeCompactorClient(  # type: ignore[assignment]
        [_summary(decisions=["d"])]
    )
    return session


def _steps_between_compactions(session: AgentSession, *, steps: int = 90) -> list[int]:
    """One long turn: after every step the host checks, as before each model call."""
    between, since = [], 0
    for index in range(steps):
        session.messages.extend(_step(index, size=4000))
        since += 1
        session.messages, changed = _compact(session, "auto")
        if changed:
            between.append(since)
            since = 0
    return between


@pytest.mark.parametrize("trigger", ["as configured", "lowered", "a picture of unknown size"])
def test_a_compaction_buys_room_for_many_steps_wherever_it_starts(tmp_path: Path, trigger) -> None:
    """A compaction costs a model call, and afterwards the provider reads the
    whole conversation again. The settings put the target 20% of the model's
    room below the trigger. Some conditions start compaction earlier: few cache
    hits, a conversation that is mostly tool output, a provider that counts
    more tokens than estimated, a picture whose size is not known. The cut
    then goes down by as much, so the next compaction is as far away."""
    session = _small_window_session(tmp_path, cache_aware_compaction=trigger == "lowered")
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        if trigger == "lowered":
            # What a provider without a prompt cache reports, on a tool-heavy turn.
            compactor._usage_summary.recent_calibration_snapshot = lambda **_: {  # type: ignore[method-assign]
                "records": 5,
                "prompt_estimate_error_ratio_p90": 1.0,
                "cache_hit_ratio": 0.0,
            }
        request: dict[str, Any] = {"role": "user", "content": REQUEST}
        if trigger == "a picture of unknown size":
            request["content"] = [
                {"type": "text", "text": REQUEST},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]
        session.messages.append(request)
        between = _steps_between_compactions(session)
        checks = [c for c in _events(session, "compaction_check") if c.get("budget_tokens")]
    finally:
        session.close()

    budget = checks[-1]["budget_tokens"]
    distance = int(budget * 0.9) - int(budget * 0.7)
    step_tokens = estimate_request_tokens(_step(0, size=4000), None)
    assert len(between) >= 3
    # After the first, each compaction comes about a whole distance later.
    assert min(between[1:]) >= int(0.7 * distance / step_tokens)
    started_at = {c["effective_trigger_tokens"] for c in checks}
    if trigger == "as configured":
        assert started_at == {int(budget * 0.9)}
    else:
        assert max(started_at) < int(budget * 0.9)
    for check in checks:
        assert check["cut_to_tokens"] == min(
            check["target_tokens"],
            max(check["target_tokens"] // 2, check["effective_trigger_tokens"] - distance),
        )


def test_the_cut_is_never_below_half_the_target(tmp_path: Path) -> None:
    """Trigger 60% and target 30% put the two 30% apart. With a picture of
    unknown size compaction starts at the target, and the same distance below
    that would be nothing at all."""
    session = _small_window_session(tmp_path, trigger_ratio=0.6, target_ratio=0.3)
    try:
        session.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": REQUEST},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        )
        assert _steps_between_compactions(session, steps=40)
        checks = [c for c in _events(session, "compaction_check") if c.get("budget_tokens")]
    finally:
        session.close()
    assert {c["effective_trigger_tokens"] for c in checks} == {checks[0]["target_tokens"]}
    assert {c["cut_to_tokens"] for c in checks} == {checks[0]["target_tokens"] // 2}


def test_a_cut_into_the_recent_tail_takes_only_what_is_needed(tmp_path: Path) -> None:
    """The deeper cut is for old activity. When only a cut into the recent tail
    can free anything, it is sized by what has to go, as it always was."""
    session = _session(tmp_path, "execution")
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        prefix = compactor.state.pinned_prefix_len
        messages = [*session.messages[:prefix], {"role": "user", "content": REQUEST}]
        for index in range(8):
            messages.extend(_step(index, size=40_000))
        asked: list[tuple[float, int | None]] = []
        build = compactor._build_execution_chunk_plans

        def recording(messages, **options):
            asked.append((options["tail_scale"], options["tokens_to_free"]))
            return build(messages, **options)

        compactor._build_execution_chunk_plans = recording  # type: ignore[method-assign]
        list(
            compactor._candidate_plans(
                messages,
                protected_prefix_len=prefix,
                budget_tokens=1_000_000,
                tokens_to_free=90_000,
                tokens_needed=7_000,
                relax=True,
                hard_pressure=True,
            )
        )
    finally:
        session.close()
    assert {ask for share, ask in asked if share == 1.0} == {90_000}
    assert {ask for share, ask in asked if share < 1.0} == {7_000}


def test_a_compaction_that_was_asked_for_is_not_sized_by_the_cut_level(tmp_path: Path) -> None:
    """Asked for by the person, or made after an overflow, with the request
    already under the target: it removes what it can, as before, and not the
    little that separates the request from the cut level."""
    session = _small_window_session(tmp_path)
    try:
        compactor = session.conversation_compactor
        assert compactor is not None
        request = {
            "role": "user",
            "content": [
                {"type": "text", "text": REQUEST},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
        session.messages.append(request)
        budget = compactor_budget(session)
        # Between the cut level (50% with a picture) and the target (70%).
        index = 0
        while estimate_request_tokens(session.messages, session.tool_list) < int(budget * 0.6):
            session.messages.extend(_step(index, size=4000))
            index += 1
        asked: list[int | None] = []
        plans = compactor._candidate_plans

        def recording(messages, **options):
            asked.append(options["tokens_to_free"])
            return plans(messages, **options)

        compactor._candidate_plans = recording  # type: ignore[method-assign]
        _messages, changed = _compact(session, "overflow")
    finally:
        session.close()
    assert changed and asked[0] is None


def compactor_budget(session: AgentSession) -> int:
    from alysis_code.compaction import conversation_compactor as module

    compactor = session.conversation_compactor
    assert compactor is not None
    return module.compute_input_budget(
        session.model_registry.get(session.client.model),
        safety_margin=compactor._settings.safety_margin_tokens,
    )
