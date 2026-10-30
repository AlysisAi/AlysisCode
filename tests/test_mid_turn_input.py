from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.agent.steering import (
    MAX_PENDING_OPS,
    MAX_PENDING_STEER_MESSAGES,
    MAX_STEER_MESSAGE_CHARS,
    OpsInbox,
    ResolvedOperation,
    SteerInbox,
    build_steer_messages,
    ops_inbox_for,
    steer_inbox_for,
)
from alysis_code.cli_impl.chat.mid_turn_policy import (
    DeferredTier,
    MidTurnAction,
    block_message,
    classify_mid_turn,
    defer_message,
    deferred_display_label,
    deferred_tier,
    is_command,
)

READ_ONLY_COMMANDS = [
    "/help",
    "/status",
    "/pwd",
    "/context",
    "/ctx",
    "/usage",
    "/usage hud on",
    "/model-info",
    "/model-info gpt-5",
    "/trace full",
    "/toolbar reset",
    "/images",
    "/image screenshot.png",
    "/paste-image",
    "/clear-images",
    "/terminals list",
    "/terminals kill 42",
    "/subagents",
]

TURN_MUTATING_COMMANDS = [
    "/clear",
    "/resume",
    "/compact",
    "/ask what is this",
    "/subagent explorer find x",
    "/forge",
    ":forge",
    "/login",
    "/logout",
    "/report",
    "/feedback",
    "/assets",
]

DEFERRED_COMMANDS = [
    "/model",
    "/model gpt-5",
    "/permissions",
    "/permissions auto",
    "/persona",
    "/persona architect",
    "/config",
    "/config set foo bar",
    "/cd subdir",
    "/plan markdown",
]


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_read_only_commands_run_mid_turn(command: str) -> None:
    assert classify_mid_turn(command) is MidTurnAction.ALLOW


@pytest.mark.parametrize("command", TURN_MUTATING_COMMANDS)
def test_turn_mutating_commands_are_blocked_mid_turn(command: str) -> None:
    assert classify_mid_turn(command) is MidTurnAction.BLOCK


@pytest.mark.parametrize("command", DEFERRED_COMMANDS)
def test_safe_configuration_commands_are_deferred_mid_turn(command: str) -> None:
    assert classify_mid_turn(command) is MidTurnAction.DEFER
    message = defer_message(command)
    if deferred_tier(command) is DeferredTier.STEP:
        assert message == f"{deferred_display_label(command)} - next step"
    else:
        assert message == f"{deferred_display_label(command)} - next message"


def test_defer_message_uses_compact_label_first_tier_text() -> None:
    assert defer_message("/stream off") == "stream: off - next step"
    assert defer_message("/persona ask") == "persona: ask - next message"


def test_old_defer_sentences_are_removed_from_runtime_source() -> None:
    source = Path("src/alysis_code/cli_impl/chat/mid_turn_policy.py").read_text(encoding="utf-8")

    assert "applies at the agent's next step" not in source
    assert "applies at your next message" not in source


def test_stream_status_remains_read_only_mid_turn() -> None:
    assert classify_mid_turn("/stream status") is MidTurnAction.ALLOW


def test_tui_step_operation_resolution_is_validated_without_mutation() -> None:
    from alysis_code.cli_impl.chat.loop import _resolve_tui_step_operation
    from alysis_code.config import AppConfig

    session = SimpleNamespace(mode="review", stream=True, cfg=AppConfig())

    mode = _resolve_tui_step_operation(
        session=session,
        text="/permissions fast",
    )
    stream = _resolve_tui_step_operation(
        session=session,
        text="/stream off",
    )

    assert mode == ResolvedOperation("mode", "auto", "permissions: fast")
    assert stream == ResolvedOperation("stream", False, "stream: off")
    assert session.mode == "review"
    assert session.stream is True
    with pytest.raises(ValueError, match="Invalid mode"):
        _resolve_tui_step_operation(
            session=session,
            text="/permissions impossible",
        )


def test_permissions_replaces_mode_in_mid_turn_policy_and_resolution() -> None:
    from alysis_code.cli_impl.chat.loop import _resolve_tui_step_operation
    from alysis_code.config import AppConfig

    session = SimpleNamespace(mode="review", stream=True, cfg=AppConfig())

    assert classify_mid_turn("/permissions fast") is MidTurnAction.DEFER
    assert classify_mid_turn("/mode fast") is MidTurnAction.BLOCK
    assert _resolve_tui_step_operation(
        session=session,
        text="/permissions fast",
    ) == ResolvedOperation("mode", "auto", "permissions: fast")
    assert (
        _resolve_tui_step_operation(
            session=session,
            text="/mode fast",
        )
        is None
    )


@pytest.mark.parametrize("text", ["exit", "quit", "/exit", "/quit", ":q", "  EXIT  "])
def test_exit_words_are_blocked_not_delivered_to_the_model(text: str) -> None:
    assert classify_mid_turn(text) is MidTurnAction.BLOCK
    assert "Esc" in block_message(text)


@pytest.mark.parametrize(
    "text",
    [
        "actually use pytest",
        "no, the other file",
        "stop and explain",
        "$code-review inspect this change",
        "",
    ],
)
def test_prose_is_delivered_as_a_message(text: str) -> None:
    assert classify_mid_turn(text) is MidTurnAction.MESSAGE


def test_bare_slash_remains_a_safe_command() -> None:
    assert classify_mid_turn("/") is MidTurnAction.ALLOW


def test_unknown_commands_fail_closed() -> None:
    assert classify_mid_turn("/some-command-added-next-year") is MidTurnAction.BLOCK
    assert classify_mid_turn(":unknown --force") is MidTurnAction.BLOCK


def test_bare_only_commands_split_on_arguments() -> None:
    assert classify_mid_turn("/skill") is MidTurnAction.ALLOW
    assert classify_mid_turn("/skill reviewer audit this") is MidTurnAction.BLOCK


def test_classification_is_case_and_whitespace_insensitive() -> None:
    assert classify_mid_turn("  /HELP  ") is MidTurnAction.ALLOW
    assert classify_mid_turn("  /MoDeL gpt-5 ") is MidTurnAction.DEFER


@pytest.mark.parametrize(
    "command", ["/resume", "/resume abc123", "/login", "/logout", "/clear", "/compact"]
)
def test_session_replacing_commands_stay_blocked(command: str) -> None:
    assert classify_mid_turn(command) is MidTurnAction.BLOCK


@pytest.mark.parametrize("command", [*TURN_MUTATING_COMMANDS, "/unknown", "exit"])
def test_every_block_message_explains_and_names_escape_hatch(command: str) -> None:
    message = block_message(command)
    assert message.strip()
    assert "Esc to interrupt" in message
    assert "wait for the turn to finish" in message


def test_is_command_distinguishes_commands_from_prose() -> None:
    assert is_command("/help")
    assert is_command(":forge")
    assert is_command("exit")
    assert not is_command("$code-review inspect this change")
    assert not is_command("please fix the parser")
    assert not is_command("")


def test_drain_is_exactly_once() -> None:
    inbox = SteerInbox()
    inbox.send("first")
    inbox.send("second")

    assert inbox.drain() == ["first", "second"]
    assert inbox.drain() == []


def test_blank_messages_are_ignored() -> None:
    inbox = SteerInbox()

    assert inbox.send("") == ""
    assert inbox.send("  \n  ") == ""
    assert inbox.pending_count() == 0


def test_long_messages_are_truncated_with_visible_marker() -> None:
    inbox = SteerInbox()

    sent = inbox.send("x" * (MAX_STEER_MESSAGE_CHARS + 1000))

    assert sent.endswith(" [truncated]")
    assert len(sent) <= MAX_STEER_MESSAGE_CHARS
    assert inbox.drain() == [sent]


def test_queue_bound_keeps_newest_and_counts_evictions() -> None:
    inbox = SteerInbox()
    overflow = 5

    for index in range(MAX_PENDING_STEER_MESSAGES + overflow):
        inbox.send(f"m{index}")

    drained = inbox.drain()
    assert len(drained) == MAX_PENDING_STEER_MESSAGES
    assert drained[0] == f"m{overflow}"
    assert drained[-1] == f"m{MAX_PENDING_STEER_MESSAGES + overflow - 1}"
    assert inbox.dropped_count() == overflow


def test_restore_front_preserves_chronology_with_newer_arrivals() -> None:
    inbox = SteerInbox()
    inbox.send("older first")
    inbox.send("older second")
    drained = inbox.drain()

    inbox.send("newer arrival")
    inbox.restore_front(drained)

    assert inbox.drain() == ["older first", "older second", "newer arrival"]


def test_restore_front_keeps_newest_messages_when_combined_queue_is_full() -> None:
    inbox = SteerInbox()
    inbox.send("oldest restored")
    drained = inbox.drain()
    for index in range(MAX_PENDING_STEER_MESSAGES):
        inbox.send(f"newer {index}")

    inbox.restore_front(drained)

    assert inbox.drain() == [f"newer {index}" for index in range(MAX_PENDING_STEER_MESSAGES)]
    assert inbox.dropped_count() == 1


def _exercise_concurrent_send_and_drain(total: int) -> tuple[list[str], int]:
    inbox = SteerInbox()
    collected: list[str] = []
    stop = threading.Event()

    def writer() -> None:
        for index in range(total):
            inbox.send(f"msg{index}")
            if index % 17 == 0:
                time.sleep(0)

    def reader() -> None:
        while not stop.is_set():
            collected.extend(inbox.drain())
            time.sleep(0)
        collected.extend(inbox.drain())

    reader_thread = threading.Thread(target=reader)
    writer_thread = threading.Thread(target=writer)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join()
    stop.set()
    reader_thread.join()
    return collected, inbox.dropped_count()


def test_concurrent_send_and_drain_conserve_every_message() -> None:
    total = 300

    for _ in range(20):
        collected, dropped = _exercise_concurrent_send_and_drain(total)
        assert len(set(collected)) == len(collected)
        assert len(collected) + dropped == total
        order = [int(message[3:]) for message in collected]
        assert order == sorted(order)


def test_inbox_attaches_lazily_and_idempotently() -> None:
    class Session:
        pass

    session = Session()
    assert steer_inbox_for(session) is None

    created = steer_inbox_for(session, create=True)
    assert created is not None
    assert steer_inbox_for(session) is created
    assert steer_inbox_for(session, create=True) is created


def test_sessions_that_cannot_take_an_attribute_opt_out() -> None:
    class SlottedSession:
        __slots__ = ()

    assert steer_inbox_for(SlottedSession(), create=True) is None


def test_ops_inbox_is_bounded_fifo_without_silent_eviction() -> None:
    inbox = OpsInbox()
    operations = [
        ResolvedOperation(kind="test", payload=index, display_label=f"op {index}")
        for index in range(MAX_PENDING_OPS + 1)
    ]

    for operation in operations[:-1]:
        assert inbox.send(operation)

    assert not inbox.send(operations[-1])
    assert inbox.pending_count() == MAX_PENDING_OPS
    assert inbox.drain() == operations[:-1]
    assert inbox.drain() == []


def test_ops_inbox_snapshot_preserves_pending_fifo_without_draining() -> None:
    inbox = OpsInbox()
    first = ResolvedOperation("mode", "auto", "permissions: fast")
    second = ResolvedOperation("stream", False, "stream: off")
    assert inbox.send(first)
    assert inbox.send(second)

    assert inbox.snapshot() == [first, second]
    assert inbox.pending_count() == 2
    assert inbox.drain() == [first, second]


def test_ops_inbox_attaches_lazily_and_idempotently() -> None:
    class Session:
        pass

    session = Session()
    assert ops_inbox_for(session) is None

    created = ops_inbox_for(session, create=True)
    assert created is not None
    assert ops_inbox_for(session) is created
    assert ops_inbox_for(session, create=True) is created


def test_steer_messages_are_durable_user_messages() -> None:
    built = build_steer_messages(["use pytest", "  ", "add type hints"])

    assert built == [
        {
            "role": "user",
            "content": "[Mid-turn message from the user] use pytest",
        },
        {
            "role": "user",
            "content": "[Mid-turn message from the user] add type hints",
        },
    ]


def test_status_line_describes_steer_queue_and_waiting_messages() -> None:
    from alysis_code.cli_impl.tui.app import _status_line_fragments

    def plain(fragments: list[tuple[str, str]]) -> str:
        return "".join(text for _style, text in fragments)

    assert "Esc or Ctrl+C to interrupt" in plain(_status_line_fragments(running=True))
    pending = plain(_status_line_fragments(running=True, input_pending=True))
    assert "Enter to steer" in pending
    assert "Ctrl+Q to queue" in pending
    queued = plain(_status_line_fragments(running=True, queued_count=2))
    assert "2 queued messages" in queued
    step_staged = plain(_status_line_fragments(running=True, step_staged_count=1))
    assert "1 applying next step" in step_staged
    turn_staged = plain(_status_line_fragments(running=True, turn_end_staged_count=2))
    assert "2 at next message" in turn_staged
    mixed = plain(
        _status_line_fragments(
            running=True,
            queued_count=2,
            step_staged_count=1,
            turn_end_staged_count=1,
        )
    )
    assert "1 applying next step" in mixed
    assert "1 at next message" in mixed
    assert "2 queued messages" in mixed


def test_pending_command_staging_is_bounded_and_preserves_every_command() -> None:
    from alysis_code.cli_impl.tui.app import (
        _MAX_PENDING_COMMANDS,
        _DeferredOperationKind,
        _stage_pending_command,
    )

    pending: list[Any] = []
    for index in range(_MAX_PENDING_COMMANDS):
        assert _stage_pending_command(pending, f"/verb-{index} value")

    assert not _stage_pending_command(pending, "/one-too-many value")
    assert len(pending) == _MAX_PENDING_COMMANDS
    assert all(operation.kind is _DeferredOperationKind.COMMAND for operation in pending)
    assert [operation.text for operation in pending] == [
        f"/verb-{index} value" for index in range(_MAX_PENDING_COMMANDS)
    ]


def test_independent_config_commands_are_not_coalesced() -> None:
    from alysis_code.cli_impl.tui.app import _stage_pending_command

    pending: list[Any] = []
    assert _stage_pending_command(pending, "/config set model model-a")
    assert _stage_pending_command(pending, "/config set default_mode auto")

    assert [operation.text for operation in pending] == [
        "/config set model model-a",
        "/config set default_mode auto",
    ]


def _run_live_tui(
    *,
    session_builder: Any,
    feed: Any,
    command_runner: Any | None = None,
    persona_cycle: Any | None = None,
    persona_stage_target: Any | None = None,
    step_operation_resolver: Any | None = None,
    step_operation_apply: Any | None = None,
    panel_providers: Any | None = None,
    before_turn: Any | None = None,
) -> tuple[Any, list[tuple[str, str]]]:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    with create_pipe_input() as pipe:
        feed_errors: list[BaseException] = []

        def run_feed() -> None:
            try:
                feed(pipe)
            except BaseException as exc:  # noqa: BLE001 - surface feeder failures in the test
                feed_errors.append(exc)
                # Ctrl-C is context-dependent (copy/cancel/modal handling), so
                # it cannot guarantee shutdown after a feeder assertion fails.
                pipe.close()

        feeder = threading.Thread(target=run_feed, daemon=True)
        feeder.start()
        try:
            result, transcript = run_tui(
                TuiState(model_name="test-model", username="tester"),
                owl_color=False,
                input=pipe,
                output=DummyOutput(),
                session_builder=session_builder,
                command_runner=command_runner,
                persona_cycle=persona_cycle,
                persona_stage_target=persona_stage_target,
                step_operation_resolver=step_operation_resolver,
                step_operation_apply=step_operation_apply,
                panel_providers=panel_providers,
                before_turn=before_turn,
                background_turns=True,
            )
        except EOFError:
            if feed_errors:
                raise feed_errors[0] from None
            raise
        finally:
            feeder.join(timeout=2)

    assert not feeder.is_alive()
    assert feed_errors == []
    return result, transcript


def _capture_tui_application(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any]]:
    from prompt_toolkit.application import Application as PromptToolkitApplication

    from alysis_code.cli_impl.tui import app as app_module

    captured: dict[str, Any] = {}

    def capture_application(*args: Any, **kwargs: Any) -> Any:
        application = PromptToolkitApplication(*args, **kwargs)
        captured["application"] = application
        return application

    monkeypatch.setattr(app_module, "Application", capture_application)
    return app_module, captured


def _pending_messages_area_text(application: Any) -> str:
    from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text

    for window in application.layout.find_all_windows():
        if getattr(window, "style", "") != "class:tui.pending-messages":
            continue
        source = getattr(window.content, "text", "")
        value = source() if callable(source) else source
        return fragment_list_to_text(to_formatted_text(value))
    return ""


def _wait_until(predicate: Any, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_pending_steer_moves_from_area_to_transcript_only_when_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module, captured = _capture_tui_application(monkeypatch)
    started = threading.Event()
    drain_at_boundary = threading.Event()
    delivered = threading.Event()
    appended_users: list[str] = []
    original_append_user = app_module.TuiTranscript.append_user

    def record_append_user(transcript: Any, text: str) -> None:
        appended_users.append(text)
        original_append_user(transcript, text)

    monkeypatch.setattr(app_module.TuiTranscript, "append_user", record_append_user)

    class BoundaryDrainSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, _text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            started.set()
            assert drain_at_boundary.wait(timeout=3)
            inbox = steer_inbox_for(self)
            assert inbox is not None
            assert inbox.drain() == ["use the runtime path"]
            self.surface.on_assistant_message_done("continued after steering")
            delivered.set()
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("use the runtime path\r")
        assert _wait_until(
            lambda: (
                "  > use the runtime path" in _pending_messages_area_text(captured["application"])
            )
        ), "pending steer did not appear in the dedicated area"
        assert appended_users == ["work"]
        pending_text = _pending_messages_area_text(captured["application"])
        assert (
            "message to be submitted after the next tool call - "
            "ctrl+c interrupts and sends it immediately"
        ) in pending_text
        drain_at_boundary.set()
        assert delivered.wait(timeout=2)
        assert _wait_until(lambda: appended_users == ["work", "use the runtime path"])
        assert _wait_until(lambda: not _pending_messages_area_text(captured["application"]))
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=BoundaryDrainSession, feed=feed)

    assert result == "/exit"
    delivered_index = transcript.index(("user", "use the runtime path"))
    output_index = transcript.index(("assistant", "continued after steering"))
    assert delivered_index < output_index


def test_two_pending_steers_render_and_deliver_in_fifo_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app_module, captured = _capture_tui_application(monkeypatch)
    started = threading.Event()
    drain_at_boundary = threading.Event()

    class BoundaryDrainSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, _text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            started.set()
            assert drain_at_boundary.wait(timeout=3)
            inbox = steer_inbox_for(self)
            assert inbox is not None
            assert inbox.drain() == ["first correction", "second correction"]
            self.surface.on_assistant_message_done("after both")
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("first correction\r")
        pipe.send_text("second correction\r")
        assert _wait_until(
            lambda: "  > second correction" in _pending_messages_area_text(captured["application"])
        )
        pending_text = _pending_messages_area_text(captured["application"])
        assert (
            "messages to be submitted after the next tool call - "
            "ctrl+c interrupts and sends them immediately"
        ) in pending_text
        assert pending_text.index("  > first correction") < pending_text.index(
            "  > second correction"
        )
        drain_at_boundary.set()
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=BoundaryDrainSession, feed=feed)

    assert result == "/exit"
    assert transcript.index(("user", "first correction")) < transcript.index(
        ("user", "second correction")
    )
    assert transcript.index(("user", "second correction")) < transcript.index(
        ("assistant", "after both")
    )


def test_rolled_back_visible_steer_runs_once_without_duplicate_user_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app_module, captured = _capture_tui_application(monkeypatch)
    first_started = threading.Event()
    restore_now = threading.Event()
    queued_started = threading.Event()
    calls: list[str] = []

    class RollbackSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            if text != "work":
                queued_started.set()
                return 0
            first_started.set()
            assert restore_now.wait(timeout=3)
            inbox = steer_inbox_for(self)
            assert inbox is not None
            drained = inbox.drain()
            assert drained == ["keep this steer"]
            # A retry notice makes the delivered steer visible before the
            # provider ultimately fails and the core restores it.
            self.surface.on_progress_update("retrying model request")
            inbox.restore_front(drained)
            self.surface.on_steer_messages_restored(drained)
            raise RuntimeError("provider failed after retry")

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("keep this steer\r")
        assert _wait_until(
            lambda: "  > keep this steer" in _pending_messages_area_text(captured["application"])
        )
        restore_now.set()
        assert queued_started.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=RollbackSession, feed=feed)

    assert result == "/exit"
    assert calls == ["work", "keep this steer"]
    assert transcript.count(("user", "keep this steer")) == 1


def test_ctrl_q_message_stays_in_pending_area_until_queued_turn_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module, captured = _capture_tui_application(monkeypatch)
    first_started = threading.Event()
    release_first = threading.Event()
    queued_started = threading.Event()
    appended_users: list[str] = []
    original_append_user = app_module.TuiTranscript.append_user

    def record_append_user(transcript: Any, text: str) -> None:
        appended_users.append(text)
        original_append_user(transcript, text)

    monkeypatch.setattr(app_module.TuiTranscript, "append_user", record_append_user)

    class QueuedSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            if text == "work":
                first_started.set()
                assert release_first.wait(timeout=3)
            elif text == "queued follow-up":
                queued_started.set()
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("queued follow-up\x11")
        assert _wait_until(
            lambda: (
                "queued follow-up inputs" in _pending_messages_area_text(captured["application"])
            )
        )
        assert "  > queued follow-up" in _pending_messages_area_text(captured["application"])
        assert appended_users == ["work"]
        release_first.set()
        assert queued_started.wait(timeout=2)
        assert _wait_until(lambda: appended_users == ["work", "queued follow-up"])
        assert _wait_until(lambda: not _pending_messages_area_text(captured["application"]))
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=QueuedSession, feed=feed)

    assert result == "/exit"
    assert transcript.count(("user", "queued follow-up")) == 1


def test_shift_left_recalls_and_requeues_latest_queued_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app_module, captured = _capture_tui_application(monkeypatch)
    first_started = threading.Event()
    release_first = threading.Event()
    edited_started = threading.Event()
    calls: list[str] = []

    class QueuedSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            if text == "work":
                first_started.set()
                assert release_first.wait(timeout=3)
            elif text == "queued follow-up edited":
                edited_started.set()
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("queued follow-up\x11")
        assert _wait_until(
            lambda: "  > queued follow-up" in _pending_messages_area_text(captured["application"])
        )
        assert "shift + ← edit last queued message" in _pending_messages_area_text(
            captured["application"]
        )

        pipe.send_text("\x1b[1;2D")
        assert _wait_until(
            lambda: captured["application"].layout.current_buffer.text == "queued follow-up"
        )
        assert "queued follow-up" not in _pending_messages_area_text(captured["application"])

        pipe.send_text(" edited\r")
        assert _wait_until(
            lambda: (
                "  > queued follow-up edited"
                in _pending_messages_area_text(captured["application"])
            )
        )
        release_first.set()
        assert edited_started.wait(timeout=2)
        pipe.send_text("/exit\r")

    result, _transcript = _run_live_tui(session_builder=QueuedSession, feed=feed)

    assert result == "/exit"
    assert calls == ["work", "queued follow-up edited"]


def test_pending_message_panel_has_distinct_surface_and_arrow_hint() -> None:
    from alysis_code.cli_impl.tui import app as app_module

    panel = app_module._STYLE.get_attrs_for_style_str("class:tui.pending-messages")
    heading = app_module._STYLE.get_attrs_for_style_str("class:tui.pending-messages.head")

    assert panel.bgcolor
    assert heading.bgcolor == panel.bgcolor
    assert heading.bold is True
    assert app_module._PENDING_EDIT_HINT == "shift + ← edit last queued message"


def test_shift_left_recalls_latest_queue_without_retracting_newer_steer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app_module, captured = _capture_tui_application(monkeypatch)
    first_started = threading.Event()
    release_first = threading.Event()
    session_box: dict[str, Any] = {}
    calls: list[str] = []

    class SteeringSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface
            session_box["session"] = self

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            if text == "work":
                first_started.set()
                assert release_first.wait(timeout=3)
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("older queued message\x11")
        pipe.send_text("steer now\r")
        assert _wait_until(
            lambda: "  > steer now" in _pending_messages_area_text(captured["application"])
        )
        inbox = steer_inbox_for(session_box["session"])
        assert inbox is not None
        assert inbox.pending_count() == 1

        pipe.send_text("\x1b[1;2D")
        assert _wait_until(
            lambda: captured["application"].layout.current_buffer.text == "older queued message"
        )
        assert inbox.pending_count() == 1
        assert "steer now" in _pending_messages_area_text(captured["application"])
        assert "older queued message" not in _pending_messages_area_text(captured["application"])

        pipe.send_text(" edited\r")
        assert _wait_until(
            lambda: (
                "  > older queued message edited"
                in _pending_messages_area_text(captured["application"])
            )
        )
        assert inbox.pending_count() == 1
        release_first.set()
        assert _wait_until(
            lambda: calls == ["work", "older queued message edited", "steer now"],
            timeout=2,
        )
        pipe.send_text("/exit\r")

    result, _transcript = _run_live_tui(session_builder=SteeringSession, feed=feed)

    assert result == "/exit"
    assert calls == ["work", "older queued message edited", "steer now"]


def test_shift_left_does_not_recall_a_pending_steer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app_module, captured = _capture_tui_application(monkeypatch)
    first_started = threading.Event()
    drain_now = threading.Event()
    drained = threading.Event()
    release_first = threading.Event()

    class BoundarySession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, _text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            first_started.set()
            assert drain_now.wait(timeout=3)
            inbox = steer_inbox_for(self)
            assert inbox is not None
            assert inbox.drain() == ["already delivered"]
            drained.set()
            assert release_first.wait(timeout=3)
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("already delivered\r")
        assert _wait_until(
            lambda: "  > already delivered" in _pending_messages_area_text(captured["application"])
        )
        pipe.send_text("\x1b[1;2D")
        time.sleep(0.1)
        assert captured["application"].layout.current_buffer.text == ""
        assert "already delivered" in _pending_messages_area_text(captured["application"])
        drain_now.set()
        assert drained.wait(timeout=2)
        release_first.set()
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=BoundarySession, feed=feed)

    assert result == "/exit"
    assert transcript.count(("user", "already delivered")) == 1


def test_old_mid_turn_sent_system_line_is_removed_from_runtime_source() -> None:
    source = Path("src/alysis_code/cli_impl/tui/app.py").read_text(encoding="utf-8")

    assert "Sent to the running turn - lands at its next step." not in source


@pytest.mark.parametrize(
    "path",
    [
        "docs/architecture.md",
        "docs/personas.md",
        "docs/personas_manual_qa.md",
        "docs/quickstart.md",
        "docs/security_model.md",
    ],
)
def test_current_cli_docs_use_permissions_command(path: str) -> None:
    source = Path(path).read_text(encoding="utf-8")

    assert "/permissions" in source
    assert re.search(r"/mode(?:\s|`|<|$)", source) is None


def test_read_only_command_runs_while_turn_is_active() -> None:
    started = threading.Event()
    release = threading.Event()
    status_seen = threading.Event()
    finished = threading.Event()
    calls: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            started.set()
            assert release.wait(timeout=3)
            finished.set()
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        if text.strip() == "/status":
            status_seen.set()
            return "handled", "STATUS DURING TURN", None, None
        if text.strip() == "/exit":
            return "exit", "", None, None
        return "run", "", text, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("/status\r")
        assert status_seen.wait(timeout=2)
        release.set()
        assert finished.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/exit"
    assert calls == ["work"]
    assert any(role == "system" and "STATUS DURING TURN" in text for role, text in transcript)


def test_status_panel_lists_both_pending_operation_tiers_in_application_order() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    status_seen = threading.Event()
    holder: dict[str, Any] = {}
    observed_labels: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface
            holder["session"] = self

        def run_turn(self, _text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            started.set()
            assert release.wait(timeout=3)
            finished.set()
            return 0

    def status_provider(_arg: str = "") -> dict[str, Any]:
        observed_labels.extend(holder["session"].pending_operation_labels())
        status_seen.set()
        return {"title": "Status", "sections": [("Pending", [])]}

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        if stripped == "/model next-model":
            return "handled", "MODEL APPLIED", None, None
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def resolve_step(_session: Any, text: str) -> ResolvedOperation | None:
        if text == "/permissions fast":
            return ResolvedOperation("mode", "auto", "permissions: fast")
        return None

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("/permissions fast\r")
        pipe.send_text("/model next-model\r")
        pipe.send_text("/status\r")
        assert status_seen.wait(timeout=2)
        pipe.send_text("q")
        release.set()
        assert finished.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, _transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
        step_operation_resolver=resolve_step,
        step_operation_apply=lambda session, operation: setattr(
            session, "pending_permissions_mode", str(operation.payload)
        ),
        panel_providers={"/status": status_provider},
    )

    assert result == "/exit"
    assert observed_labels == ["permissions: auto", "model: next-model"]


def test_allowed_command_cannot_start_a_second_concurrent_turn() -> None:
    started = threading.Event()
    command_seen = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            started.set()
            assert release.wait(timeout=3)
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        if text.strip() == "/status":
            command_seen.set()
            return "run", "", "must-not-start", {}
        if text.strip() == "/exit":
            return "exit", "", None, None
        return "run", "", text, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("/status\r")
        assert command_seen.wait(timeout=2)
        release.set()
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    _result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert calls == ["work"]
    assert any("already running" in text for role, text in transcript if role == "warn")


def test_deferred_commands_apply_in_submission_order_before_queued_turn() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    queued_started = threading.Event()
    events: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            events.append(f"turn:{text}")
            if text == "work":
                first_started.set()
                assert release_first.wait(timeout=3)
            elif text == "queued follow-up":
                queued_started.set()
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        events.append(f"command:{stripped}")
        if stripped == "/exit":
            return "exit", "", None, None
        if stripped.startswith("/"):
            return "handled", f"APPLIED {stripped}", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("/persona architect\r")
        pipe.send_text("/model gpt-5\r")
        pipe.send_text("/cd src\r")
        pipe.send_text("queued follow-up\x11")
        time.sleep(0.1)
        release_first.set()
        assert queued_started.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/exit"
    assert events.index("command:/persona architect") < events.index("command:/model gpt-5")
    assert events.index("command:/model gpt-5") < events.index("command:/cd src")
    assert events.index("command:/cd src") < events.index("turn:queued follow-up")
    assert transcript.count(("user", "/persona architect")) == 1
    assert transcript.count(("user", "/cd src")) == 1
    assert transcript.count(("user", "/model gpt-5")) == 1
    assert any(
        "model: gpt-5 - next message" in text for role, text in transcript if role == "system"
    )


def test_deferred_command_cannot_start_a_turn() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    deferred_seen = threading.Event()
    calls: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            first_started.set()
            assert release_first.wait(timeout=3)
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        if stripped == "/persona ask":
            deferred_seen.set()
            return "run", "", "must-not-start", {}
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("/persona ask\r")
        time.sleep(0.05)
        release_first.set()
        assert deferred_seen.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/exit"
    assert calls == ["work"]
    assert any(
        "Deferred command was rejected" in text for role, text in transcript if role == "warn"
    )


@pytest.mark.parametrize("command", ["/persona", "/persona ask", "/cd src", "/persona architect"])
def test_next_message_commands_apply_at_idle_before_the_next_turn(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alysis_code.cli_impl.tui.transcript import TuiTranscript

    first_started = threading.Event()
    release_first = threading.Event()
    applied = threading.Event()
    staged = threading.Event()
    events: list[str] = []
    staged_label = f"{deferred_display_label(command)} - next message"
    original_append = TuiTranscript.append

    def observe_append(transcript: TuiTranscript, role: str, text: str) -> None:
        original_append(transcript, role, text)
        if role == "system" and text == staged_label:
            staged.set()

    monkeypatch.setattr(TuiTranscript, "append", observe_append)

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            events.append(f"turn:{text}")
            if text == "work":
                first_started.set()
                assert release_first.wait(timeout=3)
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        events.append(f"command:{stripped}")
        if stripped == command:
            applied.set()
            return "handled", f"APPLIED {stripped}", None, None
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text(command + "\r")
        assert staged.wait(timeout=2)
        assert not applied.is_set()
        release_first.set()
        assert applied.wait(timeout=2)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/exit"
    assert events.index(f"command:{command}") > events.index("turn:work")
    assert any(text == staged_label for _role, text in transcript)


def test_staged_persona_mid_turn_echoes_exact_compact_label() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    applied = threading.Event()

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            if text == "work":
                first_started.set()
                assert release_first.wait(timeout=3)
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        if stripped == "/persona ask":
            applied.set()
            return "handled", "PERSONA APPLIED", None, None
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("/persona ask\r")
        time.sleep(0.05)
        release_first.set()
        assert applied.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/exit"
    assert transcript.count(("system", "persona: ask - next message")) == 1


def test_deferred_exit_stops_before_a_queued_turn_starts() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            first_started.set()
            assert release_first.wait(timeout=3)
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        if stripped == "/config set base_url https://new.example/v1":
            return "exit", "Connection changed; restart required.", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("/config set base_url https://new.example/v1\r")
        pipe.send_text("must not start\x11")
        time.sleep(0.05)
        release_first.set()

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/config set base_url https://new.example/v1"
    assert calls == ["work"]
    assert any(
        "Discarded 1 pending item because this session is closing" in text
        for role, text in transcript
        if role == "warn"
    )


def test_enter_steers_the_running_turn() -> None:
    started = threading.Event()
    received = threading.Event()
    calls: list[str] = []
    notes: list[str] = []

    class SteeringSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                inbox = steer_inbox_for(self)
                if inbox is not None and inbox.pending_count():
                    notes.extend(inbox.drain())
                    received.set()
                    return 0
                time.sleep(0.005)
            raise AssertionError("steering message was not delivered")

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("use the parser instead\r")
        assert received.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=SteeringSession, feed=feed)

    assert result == "/exit"
    assert calls == ["work"]
    assert notes == ["use the parser instead"]
    assert ("user", "use the parser instead") in transcript
    assert not any(
        "Sent to the running turn" in text for role, text in transcript if role == "system"
    )


def test_tui_echoes_the_truncated_text_that_was_actually_steered() -> None:
    started = threading.Event()
    received = threading.Event()
    notes: list[str] = []

    class SteeringSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = text, cancellation_token
            started.set()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                inbox = steer_inbox_for(self)
                if inbox is not None and inbox.pending_count():
                    notes.extend(inbox.drain())
                    received.set()
                    return 0
                time.sleep(0.005)
            raise AssertionError("steering message was not delivered")

    oversized = "x" * (MAX_STEER_MESSAGE_CHARS + 500)

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        # Large synthetic key streams take longer on Windows terminals. Keep
        # the truncation assertions independent of input parsing throughput.
        pipe.send_text(oversized + "\r")
        assert received.wait(timeout=10)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    _result, transcript = _run_live_tui(session_builder=SteeringSession, feed=feed)

    assert len(notes) == 1
    assert len(notes[0]) <= MAX_STEER_MESSAGE_CHARS
    assert notes[0].endswith(" [truncated]")
    assert ("user", notes[0]) in transcript
    assert ("user", oversized) not in transcript


def test_ctrl_q_queues_follow_up_in_order() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    third_started = threading.Event()
    calls: list[str] = []

    class QueueSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            if len(calls) == 1:
                first_started.set()
                assert release_first.wait(timeout=3)
            elif len(calls) == 3:
                third_started.set()
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("first\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("second\x11")
        pipe.send_text("third\x11")
        time.sleep(0.05)
        release_first.set()
        assert third_started.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=QueueSession, feed=feed)

    assert result == "/exit"
    assert calls == ["first", "second", "third"]
    assert transcript.count(("user", "second")) == 1
    assert transcript.count(("user", "third")) == 1
    assert not any("Queued - runs when this turn finishes" in text for _role, text in transcript)
    assert any("Running queued message" in text for _role, text in transcript)


def test_ctrl_q_queue_warns_when_full(monkeypatch: pytest.MonkeyPatch) -> None:
    _app_module, captured = _capture_tui_application(monkeypatch)
    started = threading.Event()
    calls: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            calls.append(text)
            started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if cancellation_token is not None and cancellation_token.is_cancelled:
                    return 0
                time.sleep(0.005)
            raise AssertionError("turn was not cancelled")

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        for index in range(MAX_PENDING_STEER_MESSAGES + 1):
            pipe.send_text(f"queued {index}\x11")
        time.sleep(0.2)
        application = captured["application"]
        application.loop.call_soon_threadsafe(application.exit)

    result, transcript = _run_live_tui(session_builder=BlockingSession, feed=feed)

    assert result is None
    assert calls[0] == "work"
    assert all(call.startswith("queued ") for call in calls[1:])
    assert any("Queue is full (16)" in text for role, text in transcript if role == "warn")


def test_undelivered_steering_is_rescued_as_follow_up() -> None:
    first_started = threading.Event()
    third_started = threading.Event()
    calls: list[str] = []

    class LateSteerSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            if len(calls) == 1:
                first_started.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    inbox = steer_inbox_for(self)
                    if inbox is not None and inbox.pending_count():
                        return 0
                    time.sleep(0.005)
                raise AssertionError("late steering was not queued")
            if len(calls) == 3:
                third_started.set()
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("first\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("queued before late steer\x11")
        pipe.send_text("late correction\r")
        assert third_started.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=LateSteerSession, feed=feed)

    assert result == "/exit"
    assert calls == ["first", "queued before late steer", "late correction"]
    assert any("Running queued message" in text for _role, text in transcript)


def test_escape_rescues_pending_steers_as_ordered_next_turn_prompts() -> None:
    first_started = threading.Event()
    first_cancelled = threading.Event()
    third_started = threading.Event()
    calls: list[str] = []

    class InterruptSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            calls.append(text)
            if text == "work":
                first_started.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if cancellation_token is not None and cancellation_token.is_cancelled:
                        first_cancelled.set()
                        return 0
                    time.sleep(0.005)
                raise AssertionError("turn was not cancelled")
            if text == "second pending":
                third_started.set()
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("first pending\r")
        pipe.send_text("second pending\r")
        time.sleep(0.05)
        pipe.send_text("\x1b")
        assert first_cancelled.wait(timeout=2)
        assert third_started.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=InterruptSession, feed=feed)

    assert result == "/exit"
    assert calls == ["work", "first pending", "second pending"]
    assert (
        "warn",
        "interrupted - 2 pending messages will start the next turn",
    ) in transcript
    assert not any("Discarded" in text for role, text in transcript if role == "warn")


def test_escape_without_pending_messages_keeps_plain_interrupt_voice() -> None:
    started = threading.Event()
    cancelled = threading.Event()
    calls: list[str] = []

    class InterruptSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            calls.append(text)
            started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if cancellation_token is not None and cancellation_token.is_cancelled:
                    cancelled.set()
                    return 0
                time.sleep(0.005)
            raise AssertionError("turn was not cancelled")

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("\x1b")
        assert cancelled.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=InterruptSession, feed=feed)

    assert result == "/exit"
    assert calls == ["work"]
    assert transcript.count(("warn", "Interrupted.")) == 1
    assert not any("will start the next turn" in text for _role, text in transcript)


def test_interrupt_queues_work_until_retiring_worker_fully_unwinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app_module, captured = _capture_tui_application(monkeypatch)
    first_started = threading.Event()
    first_cancelled = threading.Event()
    release_first = threading.Event()
    model_applied = threading.Event()
    second_started = threading.Event()
    active_lock = threading.Lock()
    active_turns = 0
    peak_active_turns = 0
    calls: list[str] = []

    class RetiringSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            nonlocal active_turns, peak_active_turns
            with active_lock:
                active_turns += 1
                peak_active_turns = max(peak_active_turns, active_turns)
            calls.append(text)
            try:
                if text == "first":
                    first_started.set()
                    assert _wait_until(
                        lambda: bool(
                            cancellation_token is not None and cancellation_token.is_cancelled
                        )
                    )
                    first_cancelled.set()
                    assert release_first.wait(timeout=3)
                elif text == "second":
                    second_started.set()
                return 0
            finally:
                with active_lock:
                    active_turns -= 1

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        if stripped == "/model next-model":
            model_applied.set()
            return "handled", "MODEL APPLIED", None, None
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("first\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("\x1b")
        assert first_cancelled.wait(timeout=2)

        # Session-replacing commands and ordinary prompts remain accepted by
        # the composer, but neither may touch the session while the cancelled
        # worker is still retiring.
        pipe.send_text("/model next-model\r")
        assert _wait_until(lambda: captured["application"].layout.current_buffer.text == "")
        pipe.send_text("second\r")
        assert _wait_until(
            lambda: "  > second" in _pending_messages_area_text(captured["application"])
        )
        assert not model_applied.is_set()
        assert not second_started.is_set()

        release_first.set()
        assert model_applied.wait(timeout=2)
        assert second_started.wait(timeout=2)
        pipe.send_text("/exit\r")

    result, _transcript = _run_live_tui(
        session_builder=RetiringSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/exit"
    assert calls == ["first", "second"]
    assert peak_active_turns == 1


def test_tui_teardown_waits_for_worker_before_restoring_turn_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app_module, captured = _capture_tui_application(monkeypatch)
    first_started = threading.Event()
    cancellation_seen = threading.Event()
    release_worker = threading.Event()
    cleanup_called = threading.Event()
    authority = {"value": "broad"}

    class RetiringSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, _text: str, *, cancellation_token: Any = None) -> int:
            assert authority["value"] == "narrow"
            first_started.set()
            assert _wait_until(
                lambda: bool(cancellation_token is not None and cancellation_token.is_cancelled)
            )
            cancellation_seen.set()
            assert authority["value"] == "narrow"
            assert release_worker.wait(timeout=3)
            assert authority["value"] == "narrow"
            return 0

    def before_turn(_session: Any, _run_kwargs: dict[str, Any]) -> Any:
        authority["value"] = "narrow"

        def restore() -> None:
            authority["value"] = "broad"
            cleanup_called.set()

        return restore

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        app = captured["application"]
        app.loop.call_soon_threadsafe(lambda: app.exit(result="forced-exit"))
        assert cancellation_seen.wait(timeout=2)
        assert not cleanup_called.is_set()
        assert authority["value"] == "narrow"
        release_worker.set()

    result, _transcript = _run_live_tui(
        session_builder=RetiringSession,
        before_turn=before_turn,
        feed=feed,
    )

    assert result == "forced-exit"
    assert cleanup_called.is_set()
    assert authority["value"] == "broad"


def test_interrupt_rescues_messages_and_applies_both_operation_tiers() -> None:
    started = threading.Event()
    cancelled = threading.Event()
    rescued_finished = threading.Event()
    calls: list[str] = []
    applied: list[str] = []

    class InterruptSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            calls.append(text)
            if text != "work":
                if text == "steer me":
                    rescued_finished.set()
                return 0
            started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if cancellation_token is not None and cancellation_token.is_cancelled:
                    cancelled.set()
                    return 0
                time.sleep(0.005)
            raise AssertionError("turn was not cancelled")

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("steer me\r")
        time.sleep(0.05)
        pipe.send_text("queue me\x11")
        time.sleep(0.05)
        pipe.send_text("/permissions auto\r")
        pipe.send_text("/model next-model\r")
        time.sleep(0.05)
        pipe.send_text("\x03")
        assert cancelled.wait(timeout=2)
        assert rescued_finished.wait(timeout=2)
        pipe.send_text("/exit\r")

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        if stripped == "/model next-model":
            applied.append("model")
            return "handled", "MODEL APPLIED", None, None
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def resolve_step(_session: Any, text: str) -> ResolvedOperation | None:
        if text == "/permissions auto":
            return ResolvedOperation("mode", "auto", "permissions: auto")
        return None

    def apply_step(_session: Any, operation: ResolvedOperation) -> None:
        applied.append(f"{operation.kind}:{operation.payload}")

    result, transcript = _run_live_tui(
        session_builder=InterruptSession,
        command_runner=command_runner,
        feed=feed,
        step_operation_resolver=resolve_step,
        step_operation_apply=apply_step,
    )

    assert result == "/exit"
    assert calls == ["work", "queue me", "steer me"]
    assert applied == ["mode:auto", "model"]
    assert (
        "warn",
        "interrupted - 1 pending messages will start the next turn",
    ) in transcript
    assert not any("Discarded" in text for role, text in transcript if role == "warn")
    assert transcript.count(("system", "permissions: auto - next message")) == 1


def test_full_step_inbox_falls_back_to_turn_end_without_dropping_command() -> None:
    started = threading.Event()
    release = threading.Event()
    all_commands_resolved = threading.Event()
    applied: list[int] = []
    fallback_commands: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = text, cancellation_token
            started.set()
            assert release.wait(timeout=10)
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        if stripped.startswith("/step "):
            fallback_commands.append(stripped)
            return "handled", "FALLBACK APPLIED", None, None
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def resolve_step(_session: Any, text: str) -> ResolvedOperation | None:
        if not text.startswith("/step "):
            return None
        value = int(text.split(maxsplit=1)[1])
        if value == MAX_PENDING_OPS:
            all_commands_resolved.set()
        return ResolvedOperation("test", value, f"step: {value}")

    def apply_step(_session: Any, operation: ResolvedOperation) -> None:
        applied.append(int(operation.payload))

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        for index in range(MAX_PENDING_OPS + 1):
            pipe.send_text(f"/step {index}\r")
        # Keep the turn running until the UI has received the overflowing command.
        # A fixed sleep lets a loaded runner finish the turn with a partial inbox.
        try:
            assert all_commands_resolved.wait(timeout=5)
        finally:
            release.set()
        assert _wait_until(lambda: bool(fallback_commands))
        pipe.send_text("/exit\r")

    _result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
        step_operation_resolver=resolve_step,
        step_operation_apply=apply_step,
    )

    assert applied == list(range(MAX_PENDING_OPS))
    assert fallback_commands == [f"/step {MAX_PENDING_OPS}"]
    assert (
        "system",
        f"step: {MAX_PENDING_OPS} - next message",
    ) in transcript


def test_failing_idle_step_operation_is_dropped_and_tui_stays_alive() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = text, cancellation_token
            started.set()
            assert release.wait(timeout=3)
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        if text.strip() == "/exit":
            return "exit", "", None, None
        return "run", "", text, {}

    def resolve_step(_session: Any, text: str) -> ResolvedOperation | None:
        if text == "/step fail":
            return ResolvedOperation("test", "fail", "step: fail")
        return None

    def apply_step(_session: Any, _operation: ResolvedOperation) -> None:
        raise RuntimeError("expected apply failure")

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("/step fail\r")
        time.sleep(0.05)
        release.set()
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
        step_operation_resolver=resolve_step,
        step_operation_apply=apply_step,
    )

    assert result == "/exit"
    assert (
        "error",
        "step: fail - failed to apply: expected apply failure",
    ) in transcript


def test_tab_stages_and_advances_persona_mid_turn_while_shift_tab_stays_live() -> None:
    started = threading.Event()
    release = threading.Event()
    persona_calls: list[bool] = []
    staged_from: list[str | None] = []
    applied_personas: list[str] = []
    mode_calls: list[bool] = []
    sessions: list[Any] = []

    class Store:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        def append(self, event_type: str, payload: dict[str, object]) -> None:
            self.events.append((event_type, dict(payload)))

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface
            self.store = Store()
            sessions.append(self)

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = text, cancellation_token
            started.set()
            assert release.wait(timeout=3)
            return 0

    def persona_cycle() -> list[tuple[str, str]]:
        persona_calls.append(True)
        return []

    def mode_cycle() -> list[tuple[str, str]]:
        mode_calls.append(True)
        return []

    def persona_stage_target(current: str | None) -> str:
        staged_from.append(current)
        return "architect" if current is None else "ask"

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        if stripped.startswith("/persona "):
            applied_personas.append(stripped.split(maxsplit=1)[1])
            return "handled", "PERSONA APPLIED", None, None
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("\t")
        time.sleep(0.05)
        pipe.send_text("\t")
        time.sleep(0.05)
        pipe.send_text("\x1b[Z")
        time.sleep(0.05)
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not applied_personas:
            time.sleep(0.01)
        pipe.send_text("/exit\r")

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    state = TuiState(model_name="test-model", username="tester")
    with create_pipe_input() as pipe:
        feeder = threading.Thread(target=lambda: feed(pipe), daemon=True)
        feeder.start()
        result, transcript = run_tui(
            state,
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=BlockingSession,
            command_runner=command_runner,
            persona_cycle=persona_cycle,
            persona_stage_target=persona_stage_target,
            mode_cycle=mode_cycle,
            background_turns=True,
        )
        feeder.join(timeout=2)

    assert result == "/exit"
    assert not feeder.is_alive()
    assert persona_calls == []
    assert staged_from == [None, "architect"]
    assert applied_personas == ["ask"]
    assert mode_calls == [True]
    assert sessions[0].store.events == [
        ("chat_local_interaction", {"action": "persona_cycle"}),
        ("chat_local_interaction", {"action": "persona_cycle"}),
        ("chat_local_interaction", {"action": "permissions_cycle"}),
        ("chat_local_command", {"command": "/exit", "has_argument": False}),
    ]
    assert any("persona: architect - next message" in text for _role, text in transcript)
    assert any("persona: ask - next message" in text for _role, text in transcript)
    assert not any("/persona is unavailable" in text for role, text in transcript if role == "warn")
