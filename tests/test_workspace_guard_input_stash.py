"""Startup-picker input stash.

Input processed within a short grace window of a workspace-guard app starting
to run was typed at a blank screen (the kernel had it buffered before the
picker existed). It must not act on the picker - a buffered digit used to
quick-pick, and on the candidate step BIND A WORKSPACE, sight-unseen - and
printable characters must survive into the chat-input prefill instead of
being dropped.

The gate is a real-terminal phenomenon, so these tests activate it for
injected pipe inputs via ALYSIS_STARTUP_INPUT_STASH_FORCE=1 and pin the gate
state by monkeypatching the grace window; the pre-existing guard tests
(test_tui_workspace_guard.py) run ungated with their preloaded-keys style.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.cli_impl.tui import workspace_guard as wg


@pytest.fixture(autouse=True)
def _clean_stash():
    wg.drain_startup_input_stash()
    yield
    wg.drain_startup_input_stash()


def _binding(path: str = "/home/user"):
    return SimpleNamespace(requested_path=Path(path))


def _candidates(*names: str):
    return tuple(
        SimpleNamespace(path=Path("/home/user") / n, summary=f"{n} project") for n in names
    )


def _gate_never_opens(monkeypatch):
    """Permanent blank-screen state: the grace window never elapses."""
    monkeypatch.setattr(wg, "_STASH_GRACE_SECONDS", 3600.0)


def _drive(fn, keys: str, **kwargs):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return fn(input=pipe, output=DummyOutput(), **kwargs)


# --------------------------- in-grace gating ---------------------------


def test_grace_digit_is_stashed_not_selected(monkeypatch):
    monkeypatch.setenv("ALYSIS_STARTUP_INPUT_STASH_FORCE", "1")
    _gate_never_opens(monkeypatch)

    # "ab2cd" typed at a blank screen; Ctrl-C is the always-on abort.
    value, available = _drive(
        wg.select_guarded_workspace_action,
        "ab2cd\x03",
        binding=_binding(),
        candidates=_candidates("app"),
        allow_use_current_action=True,
    )

    assert available is True
    assert value is None  # the buffered "2" selected nothing
    assert wg.drain_startup_input_stash() == "ab2cd"


def test_grace_enter_and_escape_do_not_act(monkeypatch):
    monkeypatch.setenv("ALYSIS_STARTUP_INPUT_STASH_FORCE", "1")
    _gate_never_opens(monkeypatch)

    # Buffered Enter must not confirm the default row; buffered Esc must not
    # cancel. Ctrl-C still aborts (result None), proving neither acted first.
    value, available = _drive(
        wg.select_guarded_workspace_action,
        "\r\x1b\x03",
        binding=_binding(),
        candidates=_candidates("app"),
        allow_use_current_action=True,
    )

    assert available is True
    assert value is None
    assert wg.drain_startup_input_stash() == ""  # control keys are not text


def test_candidate_picker_grace_digit_does_not_bind(monkeypatch):
    # The quick-pick hazard: on the candidate step a buffered digit used to BIND a
    # workspace sight-unseen. Gated, it selects nothing and survives as text.
    monkeypatch.setenv("ALYSIS_STARTUP_INPUT_STASH_FORCE", "1")
    _gate_never_opens(monkeypatch)

    path, available = _drive(
        wg.select_workspace_candidate,
        "3\x03",
        base_path=Path("/home/user"),
        candidates=_candidates("app", "web", "cli"),
    )

    assert available is True
    assert path is None
    assert wg.drain_startup_input_stash() == "3"


def test_prompt_grace_enter_does_not_accept_default(monkeypatch):
    monkeypatch.setenv("ALYSIS_STARTUP_INPUT_STASH_FORCE", "1")
    _gate_never_opens(monkeypatch)

    # A buffered Enter would previously return the default path sight-unseen;
    # gated, the prompt stays open until the always-on Ctrl-C cancels (which
    # raises KeyboardInterrupt per the resolver's "go back" contract).
    with pytest.raises(KeyboardInterrupt):
        _drive(
            wg.workspace_guard_prompt_text,
            "\r\x03",
            text="Enter a path",
            default="/home/user/project",
        )


# --------------------------- post-grace behavior ---------------------------


def test_post_grace_digit_still_quick_picks(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    monkeypatch.setenv("ALYSIS_STARTUP_INPUT_STASH_FORCE", "1")
    monkeypatch.setattr(wg, "_STASH_GRACE_SECONDS", 0.05)

    with create_pipe_input() as pipe:

        def feed() -> None:
            time.sleep(0.4)  # let the app start and the grace window elapse
            pipe.send_text("1")

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        value, available = wg.select_guarded_workspace_action(
            binding=_binding(),
            candidates=_candidates("app"),
            allow_use_current_action=True,
            input=pipe,
            output=DummyOutput(),
        )
        feeder.join(timeout=3)

    assert available is True
    assert value == "use_current"  # digit quick-pick works once seen
    assert wg.drain_startup_input_stash() == ""


def test_kill_switch_restores_old_behavior(monkeypatch):
    monkeypatch.setenv("ALYSIS_STARTUP_INPUT_STASH", "0")
    monkeypatch.setenv("ALYSIS_STARTUP_INPUT_STASH_FORCE", "1")
    _gate_never_opens(monkeypatch)

    # Gate disabled: the buffered digit acts immediately, blank screen or not.
    value, available = _drive(
        wg.select_guarded_workspace_action,
        "2",
        binding=_binding(),
        candidates=_candidates("app"),
        allow_use_current_action=True,
    )

    assert available is True
    assert value == "choose_project"
    assert wg.drain_startup_input_stash() == ""


def test_drain_returns_and_clears():
    wg._PREFILL_STASH.extend(["h", "i", " ", "2"])
    assert wg.drain_startup_input_stash() == "hi 2"
    assert wg.drain_startup_input_stash() == ""


# --------------------------- chat-input prefill ---------------------------


def test_run_tui_initial_input_text_prefills(monkeypatch):
    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import app as app_module
    from alysis_code.cli_impl.tui.state import TuiState

    captured: dict = {}
    feeder_errors: list[str] = []

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)
        captured["application"] = application
        captured["input_buffer"] = application.layout.current_buffer
        return application

    monkeypatch.setattr(app_module, "Application", capture_application)

    with create_pipe_input() as pipe:

        def feed() -> None:
            # Returns as soon as the TUI exists; the bound only limits a failure.
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                buffer = captured.get("input_buffer")
                if buffer is not None:
                    captured["prefilled_text"] = buffer.text
                    captured["prefilled_cursor"] = buffer.cursor_position
                    buffer.text = ""
                    pipe.send_text("/exit\r")
                    return
                time.sleep(0.01)
            feeder_errors.append("input buffer never captured")
            # Closing the input ends the TUI even with the prefilled text in it (Ctrl-D does not).
            pipe.close()

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        try:
            app_module.run_tui(
                TuiState(model_name="test-model"),
                owl_color=False,
                input=pipe,
                output=DummyOutput(),
                initial_input_text="hello guard 2 test",
            )
        except EOFError:
            if feeder_errors:
                pytest.fail("; ".join(feeder_errors))
            raise
        feeder.join(timeout=3)

    assert feeder_errors == []
    assert captured["prefilled_text"] == "hello guard 2 test"
    assert captured["prefilled_cursor"] == len("hello guard 2 test")
