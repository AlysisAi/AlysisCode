from __future__ import annotations

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code.cli_impl.tui.app import run_tui
from alysis_code.cli_impl.tui.prompt_history import PromptEntry, PromptHistory
from alysis_code.cli_impl.tui.state import TuiState


@pytest.fixture(autouse=True)
def profile(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "profile"))


def run(keys, root, *, reject=False, before_turn=None):
    calls, routed = [], []

    class Session:
        def run_turn(self, text, **kwargs):
            calls.append(text)

    def runner(session, text, width):
        routed.append(text)
        if text == "/exit":
            return "exit", "", None, None
        if reject:
            raise ValueError("rejected")
        return "run", "", text, {}

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        result = run_tui(
            TuiState(model_name="test"),
            input=pipe,
            output=DummyOutput(),
            owl_color=False,
            background_turns=False,
            workspace_root=root,
            session_builder=lambda _: Session(),
            command_runner=runner,
            before_turn=before_turn,
        )
    return calls, routed, result[1]


def test_actual_route_submit_up_enter_once(tmp_path):
    calls, routed, _ = run("probe\r\x1b[A\r/exit\r", tmp_path)
    assert calls == ["probe", "probe"]
    assert routed == ["probe", "probe", "/exit"]
    assert [e.text for e in PromptHistory(tmp_path).entries] == ["probe"]


def test_restart_scope_multiline_unicode_search(tmp_path):
    run("hello\x0aκόσμε\r/exit\r", tmp_path)
    calls, _, _ = run("draft\x12ΚΌΣ\r\r/exit\r", tmp_path)
    assert calls == ["hello\nκόσμε"]
    assert PromptHistory(tmp_path / "elsewhere").entries == []


def test_down_restores_multiline_attachment_draft(tmp_path):
    file = tmp_path / "notes.txt"
    file.write_text("notes")
    run("previous\r/exit\r", tmp_path)
    # Ctrl+A and Up reach history from first row; Down from recalled last row restores document.
    calls, _, _ = run(
        f"\x1b[200~{file}\x1b[201~draft\x0aline\x01\x1b[A\x1b[A\x1b[B\r/exit\r", tmp_path
    )
    assert calls == [f"{file.resolve()} draft\nline"]


def test_search_cancel_preserves_draft_and_payload(tmp_path):
    file = tmp_path / "notes.txt"
    file.write_text("notes")
    run("previous\r/exit\r", tmp_path)
    calls, _, _ = run(f"\x1b[200~{file}\x1b[201~draft\x12no match\x1b\r/exit\r", tmp_path)
    assert calls == [f"{file.resolve()} draft"]


def test_missing_attachment_blocks_recall_submission(tmp_path):
    file = tmp_path / "notes.txt"
    file.write_text("notes")
    run(f"\x1b[200~{file}\x1b[201~draft\r/exit\r", tmp_path)
    file.unlink()
    calls, routed, transcript = run("\x1b[A\r\x03", tmp_path)
    assert calls == routed == []
    assert any("attachment unavailable" in text for _, text in transcript)


def test_failed_dispatch_and_before_turn_not_accepted(tmp_path):
    run("rejected\r/exit\r", tmp_path, reject=True)
    assert PromptHistory(tmp_path).entries == []

    def fail(*args):
        raise ValueError("blocked")

    run("blocked\r/exit\r", tmp_path, before_turn=fail)
    assert PromptHistory(tmp_path).entries == []


def test_slash_commands_excluded(tmp_path):
    calls, _, _ = run("draft\r/help\r\x1b/exit\r", tmp_path)
    assert calls == ["draft"]
    assert [e.text for e in PromptHistory(tmp_path).entries] == ["draft"]


def test_retention_and_profile_isolation(monkeypatch, tmp_path):
    history = PromptHistory(tmp_path, limit=2, byte_limit=200)
    for text in ("one", "two", "three"):
        history.accept(PromptEntry(text))
    assert [e.text for e in PromptHistory(tmp_path).entries] == ["two", "three"]
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "other-profile"))
    assert PromptHistory(tmp_path).entries == []


def test_queue_recall_and_steering_record_only_at_acceptance(tmp_path, monkeypatch):
    import threading

    from alysis_code.agent.steering import steer_inbox_for

    accepted = []
    changed = threading.Condition()
    original = PromptHistory.accept

    def record(self, entry):
        original(self, entry)
        with changed:
            accepted.append(entry.text)
            changed.notify_all()

    monkeypatch.setattr(PromptHistory, "accept", record)
    release, finished = threading.Event(), threading.Event()
    calls, steers, errors = [], [], []

    class Session:
        def run_turn(self, text, **kwargs):
            calls.append(text)
            if text == "work":
                assert release.wait(5)
                steers.extend(steer_inbox_for(self).drain())
            else:
                finished.set()

    def runner(session, text, width):
        return ("exit", "", None, None) if text == "/exit" else ("run", "", text, {})

    def wait_count(count):
        with changed:
            assert changed.wait_for(lambda: len(accepted) >= count, 3)

    with create_pipe_input() as pipe:

        def feed():
            try:
                pipe.send_text("work\r")
                wait_count(1)
                pipe.send_text("steer\rqueued\x11")
                wait_count(3)
                pipe.send_text("\x1b[1;2D edited\r")
                wait_count(4)
                release.set()
                assert finished.wait(3)
                pipe.send_text("/exit\r")
            except BaseException as exc:
                errors.append(exc)
                release.set()
                pipe.send_text("\x03\x03/exit\r")

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        run_tui(
            TuiState(model_name="test"),
            input=pipe,
            output=DummyOutput(),
            owl_color=False,
            workspace_root=tmp_path,
            session_builder=lambda _: Session(),
            command_runner=runner,
        )
        feeder.join(5)
    assert not errors
    assert calls == ["work", "queued edited"]
    assert steers == ["steer"]
    assert accepted == ["work", "steer", "queued", "queued edited"]
    assert [e.text for e in PromptHistory(tmp_path).entries] == accepted


def test_large_paste_survives_restart_without_opaque_token(tmp_path):
    payload = "\n".join(f"line {i}" for i in range(20))
    run(f"\x1b[200~{payload}\x1b[201~\r/exit\r", tmp_path)
    calls, _, _ = run("\x12line 19\r\r/exit\r", tmp_path)
    assert calls == [payload]


def test_opaque_paste_attachment_token_collision_survives_restart(tmp_path):
    file = tmp_path / "notes.txt"
    file.write_text("notes")
    payload = "[File #1]\n" + "\n".join(f"opaque {i}" for i in range(20))
    first, _, _ = run(f"\x1b[200~{file}\x1b[201~\x1b[200~{payload}\x1b[201~\r/exit\r", tmp_path)
    recalled, _, _ = run("\x12opaque\r\r/exit\r", tmp_path)
    assert first == recalled == [f"{file.resolve()} {payload}"]


def test_down_restores_opaque_paste_payload(tmp_path):
    run("previous\r/exit\r", tmp_path)
    payload = "\n".join(f"draft {i}" for i in range(20))
    calls, _, _ = run(f"\x1b[200~{payload}\x1b[201~\x1b[A\x1b[B\r/exit\r", tmp_path)
    assert calls == [payload]


def test_readonly_profile_warns_once_and_keeps_recall(tmp_path, monkeypatch):
    from pathlib import Path

    def fail_replace(self, target):
        raise PermissionError("read only")

    monkeypatch.setattr(Path, "replace", fail_replace)
    calls, _, transcript = run("one\rtwo\r\x1b[A\r/exit\r", tmp_path)
    assert calls == ["one", "two", "two"]
    assert sum("could not be saved" in text for _, text in transcript) == 1


def test_oversized_entry_preserves_prior_history(tmp_path):
    history = PromptHistory(tmp_path, byte_limit=200)
    history.accept(PromptEntry("small"))
    history.accept(PromptEntry("x" * 201))
    assert [e.text for e in PromptHistory(tmp_path).entries] == ["small"]


def test_independently_loaded_histories_merge_alternating_writes(tmp_path):
    first, second = PromptHistory(tmp_path), PromptHistory(tmp_path)
    first.accept(PromptEntry("first"))
    second.accept(PromptEntry("second"))
    first.accept(PromptEntry("third"))
    second.accept(PromptEntry("fourth"))
    assert [e.text for e in PromptHistory(tmp_path).entries] == [
        "first",
        "second",
        "third",
        "fourth",
    ]


def test_process_lock_timeout_preserves_pending_for_next_accept(tmp_path):
    import subprocess
    import sys
    import time

    history = PromptHistory(tmp_path)
    script = """import sys, time
from pathlib import Path
from alysis_code.cli_impl.tui.prompt_history import _history_lock
with _history_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    time.sleep(0.6)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(history.path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        started = time.monotonic()
        history.accept(PromptEntry("pending"))
        assert time.monotonic() - started < 0.5
        assert history.persistence_error
        assert [e.text for e in history.entries] == ["pending"]
        _, error = process.communicate(timeout=5)
        assert process.returncode == 0, error
        history.accept(PromptEntry("later"))
        assert history.persistence_error is None
        assert [e.text for e in PromptHistory(tmp_path).entries] == ["pending", "later"]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
