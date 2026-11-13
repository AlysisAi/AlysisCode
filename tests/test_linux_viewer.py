from __future__ import annotations

import socket
import subprocess
from types import SimpleNamespace

import pytest

from alysis_code.ide import linux_viewer
from alysis_code.ide.linux_viewer import LinuxBrowserHandoff, LinuxViewerError
from alysis_code.ide.managed_browser import BrowserCancelledError, BrowserControlError


class Browser:
    def __init__(self):
        self.paused = False
        self.generation = 0
        self.resumes = 0
        self.capture_error = False
        self.drain_error = False
        self.lease = None

    def claim_viewer_control(self, owner, session):
        assert (owner, session) == ("owner", "session")
        if self.lease:
            raise BrowserControlError("already claimed")
        self.lease = "test-claim"
        return ":123", self.lease

    def release_viewer_control(self, _owner, _session, lease):
        assert self.lease == lease
        self.lease = None

    def status(self, *_args):
        return SimpleNamespace(commands_paused=self.paused, command_generation=self.generation)

    def pause_commands(self, *_args, **_kwargs):
        self.paused = True
        self.generation += 1
        if self.drain_error:
            raise BrowserControlError("not drained")
        return self.status()

    def resume_commands(self, *_args, expected_generation, cancel, **_kwargs):
        if expected_generation != self.generation or self.capture_error or self.lease:
            raise BrowserControlError("no fresh capture")
        if cancel and cancel():
            raise BrowserCancelledError("cancelled")
        self.resumes += 1
        self.generation += 1
        self.paused = False
        return {"command_generation": self.generation, "data": "fresh snapshot"}


class Process:
    returncode = None
    ignore_term = False
    ignore_kill = False
    killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        if not self.ignore_term:
            self.returncode = -15

    def kill(self):
        self.killed = True
        if not self.ignore_kill:
            self.returncode = -9

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("viewer", timeout)
        return self.returncode


@pytest.fixture
def rig(monkeypatch):
    browser, process, launches, cleanups = Browser(), Process(), [], []

    def launch(args, **kwargs):
        assert browser.paused
        launches.append((args, kwargs))
        return process

    def release(*args, **kwargs):
        assert process.poll() is not None
        assert browser.paused
        cleanups.append((args, kwargs))
        return SimpleNamespace(stdout=b'{"remaining_keys":0,"remaining_buttons":0}')

    monkeypatch.setattr(linux_viewer.subprocess, "Popen", launch)
    monkeypatch.setattr(linux_viewer.subprocess, "run", release)
    handoff = LinuxBrowserHandoff(browser, "owner", "session")
    return browser, process, handoff, launches, cleanups


def begin(handoff):
    client, server = socket.socketpair()
    try:
        result = handoff.begin(server)
        assert server.fileno() == -1
        return result
    finally:
        client.close()
        server.close()


def test_one_owned_socket_minimal_environment_and_fresh_resume(rig, monkeypatch):
    browser, process, handoff, launches, cleanups = rig
    monkeypatch.setenv("SYNTHETIC_PROVIDER_SECRET", "do not inherit")
    assert begin(handoff)["state"] == "viewer"
    args, kwargs = launches[0]
    assert "-inetd" in args and "-noremote" in args and "-norc" in args
    assert "-listen" not in args and "-rfbport" not in args
    assert kwargs["stdin"] is kwargs["stdout"]
    assert "SYNTHETIC_PROVIDER_SECRET" not in kwargs["env"]
    assert handoff.end() == {"command_generation": 2, "data": "fresh snapshot"}
    assert cleanups and process.returncode == -15 and browser.resumes == 1
    assert handoff.status()["state"] == "agent"


def test_second_grant_is_rejected_without_disturbing_first(rig):
    browser, process, handoff, launches, _ = rig
    begin(handoff)
    with pytest.raises(LinuxViewerError):
        begin(handoff)
    assert len(launches) == 1 and process.poll() is None and browser.paused


def test_second_handoff_object_cannot_claim_the_same_browser(rig):
    browser, _, handoff, launches, _ = rig
    begin(handoff)
    other = LinuxBrowserHandoff(browser, "owner", "session")
    with pytest.raises(BrowserControlError):
        begin(other)
    assert len(launches) == 1
    handoff.end()


def test_failed_drain_never_launches_or_resumes(rig):
    browser, _, handoff, launches, _ = rig
    browser.drain_error = True
    with pytest.raises(BrowserControlError):
        begin(handoff)
    with pytest.raises(BrowserControlError):
        handoff.end()
    assert launches == [] and browser.paused and browser.resumes == 0


def test_failed_spawn_remains_paused_until_explicit_resume(rig, monkeypatch):
    browser, _, handoff, _, cleanups = rig

    def fail(*_args, **_kwargs):
        raise OSError("synthetic private diagnostic")

    monkeypatch.setattr(linux_viewer.subprocess, "Popen", fail)
    with pytest.raises(LinuxViewerError, match="startup failed") as error:
        begin(handoff)
    assert "private" not in str(error.value)
    assert browser.paused and cleanups == []
    handoff.end()
    assert browser.resumes == 1


def test_ignored_terminate_is_killed_and_reaped_before_cleanup(rig):
    _, process, handoff, _, cleanups = rig
    process.ignore_term = True
    begin(handoff)
    handoff.end()
    assert process.killed and cleanups


def test_unconfirmed_process_exit_retains_handle_and_blocks_resume(rig):
    browser, process, handoff, _, cleanups = rig
    process.ignore_term = process.ignore_kill = True
    begin(handoff)
    with pytest.raises(LinuxViewerError, match="revocation is unconfirmed"):
        handoff.end()
    assert browser.paused and browser.resumes == 0 and cleanups == []
    assert handoff.status()["viewer_process_alive"]
    process.ignore_kill = False
    handoff.end()
    assert browser.resumes == 1


@pytest.mark.parametrize(
    "result",
    [b"{}", b'{"remaining_keys":1,"remaining_buttons":0}', b"[]", b"not json"],
)
def test_unverified_input_release_keeps_agent_paused(rig, monkeypatch, result):
    browser, _, handoff, _, _ = rig
    begin(handoff)
    monkeypatch.setattr(
        linux_viewer.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=result)
    )
    with pytest.raises(LinuxViewerError, match="cleanup is unconfirmed"):
        handoff.end()
    assert browser.paused and browser.resumes == 0
    assert handoff.status()["input_cleanup_pending"]


def test_cleanup_timeout_blocks_resume(rig, monkeypatch):
    browser, _, handoff, _, _ = rig
    begin(handoff)

    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("release", 3)

    monkeypatch.setattr(linux_viewer.subprocess, "run", fail)
    with pytest.raises(LinuxViewerError, match="helper timeout"):
        handoff.end()
    assert browser.paused and browser.resumes == 0


def test_helper_failure_has_safe_typed_diagnostics(rig, monkeypatch):
    browser, _, handoff, _, _ = rig
    begin(handoff)

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "release", stderr=b"private diagnostic")

    monkeypatch.setattr(linux_viewer.subprocess, "run", fail)
    with pytest.raises(LinuxViewerError, match="helper exit 1") as error:
        handoff.end()
    assert "private" not in str(error.value)
    assert browser.paused and browser.resumes == 0


def test_viewer_disconnect_requires_cleanup_and_explicit_resume(rig):
    browser, process, handoff, _, cleanups = rig
    begin(handoff)
    process.returncode = 0
    assert handoff.status()["state"] == "disconnected"
    assert browser.paused and cleanups == []
    handoff.end()
    assert cleanups and browser.resumes == 1


@pytest.mark.parametrize("cause", ["new_pause", "capture_failure", "cancelled"])
def test_failed_fresh_resume_does_not_restore_viewer_or_agent_input(rig, cause):
    browser, process, handoff, _, cleanups = rig
    begin(handoff)
    if cause == "new_pause":
        browser.generation += 1
    if cause == "capture_failure":
        browser.capture_error = True
    with pytest.raises((BrowserControlError, BrowserCancelledError)):
        handoff.end(cancel=lambda: cause == "cancelled")
    assert process.poll() is not None and cleanups
    assert browser.paused and browser.resumes == 0


def test_close_revokes_without_automatic_resume_and_disallows_restart(rig):
    browser, process, handoff, _, cleanups = rig
    begin(handoff)
    handoff.close()
    assert handoff.status()["state"] == "closed"
    assert process.poll() is not None and cleanups and browser.paused
    with pytest.raises(LinuxViewerError):
        begin(handoff)


def test_completed_close_is_idempotent_after_browser_disposal(rig, monkeypatch):
    browser, _, handoff, _, cleanups = rig
    begin(handoff)
    handoff.close()

    def disposed(*_args):
        raise BrowserControlError("Browser was already disposed.")

    monkeypatch.setattr(browser, "status", disposed)
    handoff.close()
    assert len(cleanups) == 1 and browser.resumes == 0


def test_failed_close_retains_ownership_for_explicit_retry_and_cannot_resume(rig, monkeypatch):
    browser, process, handoff, launches, _ = rig
    begin(handoff)
    release = linux_viewer.subprocess.run
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise subprocess.TimeoutExpired("release", 3)

    monkeypatch.setattr(linux_viewer.subprocess, "run", fail)
    with pytest.raises(LinuxViewerError, match="helper timeout"):
        handoff.close()
    assert process.poll() is not None and browser.lease is not None
    assert browser.paused and browser.resumes == 0
    assert handoff.status()["input_cleanup_pending"]
    with pytest.raises(LinuxViewerError):
        begin(handoff)
    with pytest.raises(BrowserControlError, match="shutdown"):
        handoff.end()
    assert attempts == [1] and len(launches) == 1
    monkeypatch.setattr(linux_viewer.subprocess, "run", release)
    handoff.close()
    assert browser.lease is None and browser.resumes == 0
    assert handoff.status()["state"] == "closed"


def test_failed_close_retries_the_retained_live_process(rig):
    browser, process, handoff, _, cleanups = rig
    process.ignore_term = process.ignore_kill = True
    begin(handoff)
    with pytest.raises(LinuxViewerError, match="revocation is unconfirmed"):
        handoff.close()
    assert handoff.status()["viewer_process_alive"] and browser.lease is not None
    assert cleanups == [] and browser.resumes == 0
    process.ignore_kill = False
    handoff.close()
    assert cleanups and browser.lease is None and process.poll() is not None
    assert browser.resumes == 0


def test_failed_claim_release_retries_without_repeating_verified_input_cleanup(rig, monkeypatch):
    browser, _, handoff, _, cleanups = rig
    begin(handoff)
    release = browser.release_viewer_control

    def fail(*_args):
        raise BrowserControlError("Synthetic claim release failure.")

    monkeypatch.setattr(browser, "release_viewer_control", fail)
    with pytest.raises(BrowserControlError):
        handoff.close()
    assert browser.lease is not None and browser.paused and browser.resumes == 0
    assert len(cleanups) == 1 and not handoff.status()["input_cleanup_pending"]
    assert handoff.status()["state"] == "closing"
    monkeypatch.setattr(browser, "release_viewer_control", release)
    handoff.close()
    assert len(cleanups) == 1 and browser.lease is None and browser.resumes == 0


def phases(*names):
    return b"".join(linux_viewer._CLEANUP_PHASE_PREFIX + name.encode() + b"\n" for name in names)


@pytest.mark.parametrize("failure", ["timeout", "exit", "invalid_result"])
def test_verified_clear_diagnostic_never_authorizes_failed_cleanup(rig, monkeypatch, failure):
    browser, process, handoff, _, cleanups = rig
    begin(handoff)
    diagnostic = phases("helper-entry", "state-verify", "verified-clear", "display-close")
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        assert process.poll() is not None
        assert kwargs["timeout"] == 3 and kwargs["check"] is True
        if failure == "timeout":
            raise subprocess.TimeoutExpired("release", 3, stderr=diagnostic)
        if failure == "exit":
            raise subprocess.CalledProcessError(1, "release", stderr=diagnostic)
        return SimpleNamespace(stdout=b"not json", stderr=diagnostic)

    monkeypatch.setattr(linux_viewer.subprocess, "run", fail)
    with pytest.raises(LinuxViewerError, match="Input cleanup is unconfirmed") as error:
        handoff.end()
    assert "Helper phases: helper-entry, state-verify, verified-clear, display-close." in str(
        error.value
    )
    assert len(calls) == 1 and not cleanups  # No automatic retry.
    assert browser.paused and browser.resumes == 0 and browser.lease is not None
    assert handoff.status()["input_cleanup_pending"]
    with pytest.raises(LinuxViewerError, match="previous viewer"):
        begin(handoff)


@pytest.mark.parametrize(
    "diagnostic,expected",
    [
        (None, "Helper entry unconfirmed."),
        (b"private display :987 and typed contents", "Helper entry unconfirmed."),
        (b"ALYSIS_INPUT_CLEANUP_PHASE=state-re", "Helper entry unconfirmed."),
        (b"ALYSIS_INPUT_CLEANUP_PHASE=state-read", "Helper entry unconfirmed."),
        (b"ALYSIS_INPUT_CLEANUP_PHASE=unknown-secret\n", "Helper entry unconfirmed."),
        (b"prefix ALYSIS_INPUT_CLEANUP_PHASE=verified-clear\n", "Helper entry unconfirmed."),
        (b"ALYSIS_INPUT_CLEANUP_PHASE=verified-clear\r\n", "Helper entry unconfirmed."),
        (
            phases("helper-entry") + b"\xffprivate\n" + phases("state-read") + b"truncated",
            "Helper phases: helper-entry, state-read.",
        ),
        (phases("state-read", "state-read"), "Helper phases: state-read."),
        (phases("helper-entry").decode(), "Helper phases: helper-entry."),
        (b"x" * 4096 + b"\n" + phases("verified-clear"), "Helper entry unconfirmed."),
        (
            b"x\n" * 2040 + phases("verified-clear"),
            "Helper entry unconfirmed.",
        ),
    ],
)
def test_cleanup_failure_exposes_only_complete_bounded_markers(
    rig, monkeypatch, diagnostic, expected
):
    browser, _, handoff, _, _ = rig
    begin(handoff)

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "release", stderr=diagnostic)

    monkeypatch.setattr(linux_viewer.subprocess, "run", fail)
    with pytest.raises(LinuxViewerError) as error:
        handoff.end()
    assert str(error.value) == (
        "Input cleanup is unconfirmed (helper exit 1); commands remain paused. " + expected
    )
    assert browser.paused and browser.resumes == 0


@pytest.fixture
def x11_cleanup(monkeypatch):
    """Exercise the actual helper sequence without requiring a host X server."""
    events = []
    samples = iter([{50}, set()])
    button_samples = iter([[1, 8], []])

    class Function:
        def __init__(self, name, operation=None):
            self.name, self.operation = name, operation

        def __call__(self, *args):
            events.append(self.name)
            return self.operation(*args) if self.operation else 1

    def query(_display, output):
        for key in next(samples):
            output[key // 8] |= 1 << (key % 8)
        return 1

    x11 = SimpleNamespace(
        XOpenDisplay=Function("open"),
        XDefaultRootWindow=Function("root"),
        XQueryKeymap=Function("query", query),
        XSync=Function("sync"),
        XCloseDisplay=Function("close"),
    )
    xtst = SimpleNamespace(
        XTestFakeKeyEvent=Function("release-key"),
        XTestFakeButtonEvent=Function("release-button"),
    )
    monkeypatch.setattr(
        linux_viewer.ctypes, "CDLL", lambda name: x11 if name == "libX11.so.6" else xtst
    )
    monkeypatch.setattr(linux_viewer, "_pressed_x11_buttons", lambda *_args: next(button_samples))
    return x11, events


def test_helper_markers_cover_real_sequence_without_display_or_input_values(x11_cleanup, capfd):
    _, events = x11_cleanup
    outcome = linux_viewer._release_x11_input(":987-private-display")
    captured = capfd.readouterr()
    assert outcome == {
        "released_keys": 1,
        "released_buttons": 2,
        "remaining_keys": 0,
        "remaining_buttons": 0,
    }
    assert captured.out == ""
    assert captured.err == phases(*linux_viewer._CLEANUP_PHASES).decode()
    assert events == [
        "open",
        "query",
        "release-key",
        "release-button",
        "release-button",
        "sync",
        "query",
        "close",
    ]


def test_failed_display_close_keeps_verified_clear_and_close_phase(x11_cleanup, capfd):
    x11, _ = x11_cleanup

    def fail(*_args):
        raise OSError("private display close error")

    x11.XCloseDisplay.operation = fail
    with pytest.raises(OSError):
        linux_viewer._release_x11_input(":987-private-display")
    captured = capfd.readouterr()
    assert captured.err == phases(*linux_viewer._CLEANUP_PHASES[:-1]).decode()


def test_diagnostic_write_failure_does_not_skip_input_release_or_display_close(
    x11_cleanup, monkeypatch
):
    _, events = x11_cleanup

    def fail(*_args):
        raise OSError("closed diagnostic pipe")

    monkeypatch.setattr(linux_viewer.os, "write", fail)
    outcome = linux_viewer._release_x11_input(":987-private-display")
    assert outcome["remaining_keys"] == outcome["remaining_buttons"] == 0
    assert "release-key" in events and events[-1] == "close"
