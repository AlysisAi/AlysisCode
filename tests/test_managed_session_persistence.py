from __future__ import annotations

import json
import os
import signal
import stat
from pathlib import Path
from typing import Any

import pytest
from _fs_link_helpers import directory_link
from typer.testing import CliRunner

from alysis_code.cli import app as alysis_app
from alysis_code.managed_session import (
    ManagedSessionError,
    ManagedSessionPersistence,
    normalize_managed_session_id,
    prepare_managed_session_persistence,
)
from alysis_code.session_store import SessionStore


def _attempt_directory_swap(directory: Path, detached: Path, foreign: Path) -> bool:
    try:
        directory.rename(detached)
    except PermissionError as exc:
        # Windows can reject renaming a directory with a live child handle,
        # even when that child permits delete sharing. Refusal is a safe outcome;
        # callers still verify replay/writes stay on the admitted original file.
        if os.name != "nt" or exc.winerror not in {5, 32}:
            raise
        assert directory.is_dir() and not detached.exists()
        return False
    directory_link(directory, foreign)
    return True


def _desktop_env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "config"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }


def _invoke_managed_run(
    *,
    tmp_path: Path,
    session_id: str,
    sessions_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, dict[str, bool]]:
    reached = {"subscription": False, "run": False}

    def subscription(**_kwargs: object) -> None:
        reached["subscription"] = True

    def run_impl(*_args: object, **_kwargs: object) -> None:
        reached["run"] = True

    monkeypatch.setattr(
        "alysis_code.cli_impl.commands.root._require_active_subscription_ready",
        subscription,
    )
    monkeypatch.setattr("alysis_code.cli_impl.chat.run_impl", run_impl)
    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--session",
            session_id,
            "--session-dir",
            os.fspath(sessions_dir),
            "continue",
        ],
        env=_desktop_env(tmp_path),
    )
    return result, reached


@pytest.mark.parametrize(
    "session_id",
    [
        ".leading-dot",
        "_leading-underscore",
        "Ωmega",
        "safe trailing-space",
        "a" * 129,
        "CON",
        "con.notes",
        "PRN.backup",
        "AUX",
        "NUL.jsonl",
        "COM1.chat",
        "LPT9",
    ],
)
def test_managed_session_ids_are_exact_ascii_and_portable(session_id: str) -> None:
    with pytest.raises(ManagedSessionError):
        normalize_managed_session_id(session_id)


@pytest.mark.parametrize(
    "session_id",
    ["a", "App.Session-01", "ses_abcdefgh", "COM0", "LPT10", "x" * 128],
)
def test_managed_session_ids_accept_the_app_contract(session_id: str) -> None:
    assert normalize_managed_session_id(session_id) == session_id


def test_symlinked_session_directory_fails_before_subscription_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_dir = tmp_path / "real-sessions"
    real_dir.mkdir()
    alias = tmp_path / "session-alias"
    directory_link(alias, real_dir)

    result, reached = _invoke_managed_run(
        tmp_path=tmp_path,
        session_id="safe-session",
        sessions_dir=alias,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 2
    assert "must not be a symlink" in result.output
    assert reached == {"subscription": False, "run": False}


def test_session_file_symlink_fails_before_subscription_and_does_not_touch_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_symlinks: None,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    foreign = tmp_path / "foreign.jsonl"
    original = '{"foreign":true}\n'
    foreign.write_text(original, encoding="utf-8")
    (sessions_dir / "safe-session.jsonl").symlink_to(foreign)

    result, reached = _invoke_managed_run(
        tmp_path=tmp_path,
        session_id="safe-session",
        sessions_dir=sessions_dir,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 2
    assert "could not be opened safely" in result.output
    assert reached == {"subscription": False, "run": False}
    assert foreign.read_text(encoding="utf-8") == original


def test_non_directory_destination_fails_before_subscription_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "not-a-directory"
    destination.write_text("file", encoding="utf-8")

    result, reached = _invoke_managed_run(
        tmp_path=tmp_path,
        session_id="safe-session",
        sessions_dir=destination,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 2
    assert "must be a directory" in result.output
    assert reached == {"subscription": False, "run": False}


def test_non_regular_session_leaf_fails_before_subscription_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "safe-session.jsonl").mkdir()

    result, reached = _invoke_managed_run(
        tmp_path=tmp_path,
        session_id="safe-session",
        sessions_dir=sessions_dir,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 2
    assert reached == {"subscription": False, "run": False}


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO admission regression")
def test_read_only_fifo_session_leaf_fails_promptly_before_subscription_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    os.mkfifo(sessions_dir / "safe-session.jsonl", mode=0o600)

    def blocked_open_timeout(_signum: int, _frame: object) -> None:
        raise TimeoutError("managed FIFO admission blocked")

    previous_handler = signal.signal(signal.SIGALRM, blocked_open_timeout)
    signal.setitimer(signal.ITIMER_REAL, 2.0)
    try:
        reached = {"subscription": False, "run": False}

        def subscription(**_kwargs: object) -> None:
            reached["subscription"] = True

        def run_impl(*_args: object, **_kwargs: object) -> None:
            reached["run"] = True

        monkeypatch.setattr(
            "alysis_code.cli_impl.commands.root._require_active_subscription_ready",
            subscription,
        )
        monkeypatch.setattr("alysis_code.cli_impl.chat.run_impl", run_impl)
        result = CliRunner().invoke(
            alysis_app,
            [
                "run",
                "--no-log",
                "--session",
                "safe-session",
                "--session-dir",
                os.fspath(sessions_dir),
                "continue",
            ],
            env=_desktop_env(tmp_path),
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)

    assert result.exit_code == 2
    assert "must be a regular file" in result.output
    assert reached == {"subscription": False, "run": False}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission-mode regression")
def test_unwritable_session_directory_fails_before_subscription_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(mode=0o500)
    try:
        result, reached = _invoke_managed_run(
            tmp_path=tmp_path,
            session_id="safe-session",
            sessions_dir=sessions_dir,
            monkeypatch=monkeypatch,
        )
    finally:
        sessions_dir.chmod(0o700)

    assert result.exit_code == 2
    assert "not writable" in result.output
    assert reached == {"subscription": False, "run": False}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission-mode regression")
def test_unwritable_existing_session_log_fails_before_subscription_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = sessions_dir / "safe-session.jsonl"
    session_path.write_text('{"type":"prior"}\n', encoding="utf-8")
    session_path.chmod(0o400)
    try:
        result, reached = _invoke_managed_run(
            tmp_path=tmp_path,
            session_id="safe-session",
            sessions_dir=sessions_dir,
            monkeypatch=monkeypatch,
        )
    finally:
        session_path.chmod(0o600)

    assert result.exit_code == 2
    assert "could not be opened safely" in result.output
    assert reached == {"subscription": False, "run": False}


def test_invalid_session_id_fails_before_subscription_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, reached = _invoke_managed_run(
        tmp_path=tmp_path,
        session_id="AUX.txt",
        sessions_dir=tmp_path / "sessions",
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 2
    assert "Windows reserved device name" in result.output
    assert reached == {"subscription": False, "run": False}


def test_session_dir_without_session_fails_before_subscription_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_subscription = False

    def subscription(**_kwargs: object) -> None:
        nonlocal reached_subscription
        reached_subscription = True

    monkeypatch.setattr(
        "alysis_code.cli_impl.commands.root._require_active_subscription_ready",
        subscription,
    )
    result = CliRunner().invoke(
        alysis_app,
        ["run", "--session-dir", os.fspath(tmp_path / "sessions"), "continue"],
        env=_desktop_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "--session-dir requires --session" in result.output
    assert reached_subscription is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode regression")
def test_new_managed_log_is_created_owner_only_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    result, reached = _invoke_managed_run(
        tmp_path=tmp_path,
        session_id="safe-session",
        sessions_dir=sessions_dir,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 0, result.output
    assert reached == {"subscription": True, "run": True}
    metadata = (sessions_dir / "safe-session.jsonl").stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode regression")
def test_existing_managed_log_is_restricted_owner_only_before_provider_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = sessions_dir / "safe-session.jsonl"
    session_path.write_text('{"type":"prior"}\n', encoding="utf-8")
    session_path.chmod(0o644)

    result, reached = _invoke_managed_run(
        tmp_path=tmp_path,
        session_id="safe-session",
        sessions_dir=sessions_dir,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 0, result.output
    assert reached == {"subscription": True, "run": True}
    assert stat.S_IMODE(session_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode regression")
def test_replay_only_managed_log_is_restricted_before_provider_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = sessions_dir / "safe-session.jsonl"
    prior = {"type": "prior", "payload": {"content": "prior"}}
    session_path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    session_path.chmod(0o644)
    observed: dict[str, object] = {}

    def subscription(**_kwargs: object) -> None:
        observed["mode_at_subscription"] = stat.S_IMODE(session_path.stat().st_mode)

    def run_impl(*_args: object, **kwargs: object) -> None:
        persistence = kwargs["managed_session_persistence"]
        observed["events"] = persistence.events_snapshot()

    monkeypatch.setattr(
        "alysis_code.cli_impl.commands.root._require_active_subscription_ready",
        subscription,
    )
    monkeypatch.setattr("alysis_code.cli_impl.chat.run_impl", run_impl)
    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--no-log",
            "--session",
            "safe-session",
            "--session-dir",
            os.fspath(sessions_dir),
            "continue",
        ],
        env=_desktop_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert observed == {"mode_at_subscription": 0o600, "events": [prior]}
    assert stat.S_IMODE(session_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode regression")
def test_existing_log_permission_restriction_failure_stops_before_provider_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = sessions_dir / "safe-session.jsonl"
    original = '{"type":"prior"}\n'
    session_path.write_text(original, encoding="utf-8")
    session_path.chmod(0o644)

    def reject_fchmod(_fd: int, _mode: int) -> None:
        raise PermissionError("simulated permission failure")

    monkeypatch.setattr("alysis_code.managed_session.os.fchmod", reject_fchmod)
    result, reached = _invoke_managed_run(
        tmp_path=tmp_path,
        session_id="safe-session",
        sessions_dir=sessions_dir,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 2
    assert "could not be restricted to its owner" in result.output
    assert reached == {"subscription": False, "run": False}
    assert session_path.read_text(encoding="utf-8") == original


def test_final_writer_seal_rejects_leaf_swap_before_provider_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_symlinks: None,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = sessions_dir / "safe-session.jsonl"
    session_path.write_text('{"type":"prior"}\n', encoding="utf-8")
    detached = sessions_dir / "detached.jsonl"
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text("foreign\n", encoding="utf-8")
    original_validate = ManagedSessionPersistence.validate_bound_path
    swapped = False

    def swap_before_final_validation(self: ManagedSessionPersistence) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            session_path.rename(detached)
            session_path.symlink_to(foreign)
        original_validate(self)

    monkeypatch.setattr(
        ManagedSessionPersistence,
        "validate_bound_path",
        swap_before_final_validation,
    )
    result, reached = _invoke_managed_run(
        tmp_path=tmp_path,
        session_id="safe-session",
        sessions_dir=sessions_dir,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 2
    assert "changed" in result.output
    assert reached == {"subscription": False, "run": False}
    assert foreign.read_text(encoding="utf-8") == "foreign\n"
    assert detached.read_text(encoding="utf-8") == '{"type":"prior"}\n'


def test_final_writer_creation_failure_stops_before_provider_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"

    def reject_fdopen(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr("alysis_code.managed_session.os.fdopen", reject_fdopen)
    result, reached = _invoke_managed_run(
        tmp_path=tmp_path,
        session_id="safe-session",
        sessions_dir=sessions_dir,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 2
    assert "writer could not be prepared safely" in result.output
    assert reached == {"subscription": False, "run": False}


def test_path_swap_after_preprovider_seal_uses_only_the_retained_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_symlinks: None,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = sessions_dir / "safe-session.jsonl"
    prior = {"type": "prior", "payload": {"content": "prior"}}
    session_path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    detached = sessions_dir / "detached.jsonl"
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text("foreign\n", encoding="utf-8")
    reached = {"subscription": False, "run": False}

    def subscription(**_kwargs: object) -> None:
        reached["subscription"] = True
        session_path.rename(detached)
        session_path.symlink_to(foreign)

    def run_impl(*_args: object, **kwargs: object) -> None:
        reached["run"] = True
        persistence = kwargs["managed_session_persistence"]
        persistence.require_sealed()
        prepared_writer = persistence._claimed_writer  # noqa: SLF001 - exact fd handoff proof
        assert prepared_writer is not None
        prepared_fd = prepared_writer.fileno()
        prepared_identity = os.fstat(prepared_fd)
        store = SessionStore(
            enabled=True,
            sessions_dir=sessions_dir,
            session_id="safe-session",
            cwd=os.fspath(tmp_path),
            repo_root=None,
            managed_session_persistence=persistence,
        )
        assert store._fh is prepared_writer  # noqa: SLF001 - exact fd handoff proof
        assert store._fh.fileno() == prepared_fd  # noqa: SLF001 - exact fd handoff proof
        claimed_identity = os.fstat(store._fh.fileno())  # noqa: SLF001 - exact fd handoff proof
        assert (claimed_identity.st_dev, claimed_identity.st_ino) == (
            prepared_identity.st_dev,
            prepared_identity.st_ino,
        )
        assert store.events_snapshot() == [prior]
        store.append("final", {"content": "new"})
        store.close()

    monkeypatch.setattr(
        "alysis_code.cli_impl.commands.root._require_active_subscription_ready",
        subscription,
    )
    monkeypatch.setattr("alysis_code.cli_impl.chat.run_impl", run_impl)
    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--session",
            "safe-session",
            "--session-dir",
            os.fspath(sessions_dir),
            "continue",
        ],
        env=_desktop_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert reached == {"subscription": True, "run": True}
    assert foreign.read_text(encoding="utf-8") == "foreign\n"
    detached_events = [
        json.loads(line) for line in detached.read_text(encoding="utf-8").splitlines()
    ]
    assert detached_events[0] == prior
    assert detached_events[1]["type"] == "final"
    assert detached_events[1]["payload"] == {"content": "new"}


def test_directory_swap_after_preprovider_seal_keeps_the_retained_log_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = sessions_dir / "safe-session.jsonl"
    prior = {"type": "prior", "payload": {"content": "prior"}}
    session_path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    detached_dir = tmp_path / "detached-sessions"
    foreign_dir = tmp_path / "foreign-sessions"
    foreign_dir.mkdir()
    foreign = foreign_dir / "safe-session.jsonl"
    foreign.write_text("foreign\n", encoding="utf-8")
    reached = {"subscription": False, "run": False}
    swapped = False

    def subscription(**_kwargs: object) -> None:
        nonlocal swapped
        reached["subscription"] = True
        swapped = _attempt_directory_swap(sessions_dir, detached_dir, foreign_dir)

    def run_impl(*_args: object, **kwargs: object) -> None:
        reached["run"] = True
        persistence = kwargs["managed_session_persistence"]
        persistence.require_sealed()
        store = SessionStore(
            enabled=True,
            sessions_dir=persistence.sessions_dir,
            session_id="safe-session",
            cwd=os.fspath(tmp_path),
            repo_root=None,
            managed_session_persistence=persistence,
        )
        assert store.events_snapshot() == [prior]
        store.append("final", {"content": "new"})
        store.close()

    monkeypatch.setattr(
        "alysis_code.cli_impl.commands.root._require_active_subscription_ready",
        subscription,
    )
    monkeypatch.setattr("alysis_code.cli_impl.chat.run_impl", run_impl)
    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--session",
            "safe-session",
            "--session-dir",
            os.fspath(sessions_dir),
            "continue",
        ],
        env=_desktop_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert reached == {"subscription": True, "run": True}
    assert foreign.read_text(encoding="utf-8") == "foreign\n"
    detached_events = [
        json.loads(line)
        for line in ((detached_dir if swapped else sessions_dir) / "safe-session.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert detached_events[0] == prior
    assert detached_events[1]["type"] == "final"


def test_read_and_append_share_the_same_admitted_file_descriptor(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = sessions_dir / "safe-session.jsonl"
    prior = {"type": "user_message", "payload": {"content": "prior"}}
    session_path.write_text(json.dumps(prior) + "\n", encoding="utf-8")

    persistence = prepare_managed_session_persistence(
        sessions_dir=sessions_dir,
        session_id="safe-session",
    )
    try:
        assert persistence.events_snapshot() == [prior]
        writer = persistence.claim_text_writer()
        writer.write(json.dumps({"type": "final", "payload": {"content": "new"}}) + "\n")
        writer.flush()
        writer.close()
    finally:
        persistence.close()

    assert [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines()] == [
        prior,
        {"type": "final", "payload": {"content": "new"}},
    ]


def test_session_leaf_swap_after_validation_fails_closed(
    tmp_path: Path, require_symlinks: None
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = sessions_dir / "safe-session.jsonl"
    session_path.write_text('{"type":"prior"}\n', encoding="utf-8")
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text("foreign\n", encoding="utf-8")

    persistence = prepare_managed_session_persistence(
        sessions_dir=sessions_dir,
        session_id="safe-session",
    )
    detached = sessions_dir / "detached.jsonl"
    session_path.rename(detached)
    session_path.symlink_to(foreign)
    try:
        with pytest.raises(ManagedSessionError, match="changed"):
            persistence.claim_text_writer()
    finally:
        persistence.close()

    assert foreign.read_text(encoding="utf-8") == "foreign\n"
    assert detached.read_text(encoding="utf-8") == '{"type":"prior"}\n'


def test_session_directory_swap_after_admission_fails_closed(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = sessions_dir / "safe-session.jsonl"
    session_path.write_text('{"type":"prior"}\n', encoding="utf-8")
    foreign_dir = tmp_path / "foreign-sessions"
    foreign_dir.mkdir()
    foreign = foreign_dir / "safe-session.jsonl"
    foreign.write_text("foreign\n", encoding="utf-8")

    persistence = prepare_managed_session_persistence(
        sessions_dir=sessions_dir,
        session_id="safe-session",
    )
    detached_dir = tmp_path / "detached-sessions"
    try:
        swapped = _attempt_directory_swap(sessions_dir, detached_dir, foreign_dir)
        if swapped:
            with pytest.raises(ManagedSessionError, match="directory changed"):
                persistence.claim_text_writer()
        else:
            persistence.validate_bound_path()
            assert persistence.events_snapshot() == [{"type": "prior"}]
            persistence.claim_text_writer()
    finally:
        persistence.close()

    assert foreign.read_text(encoding="utf-8") == "foreign\n"
    assert ((detached_dir if swapped else sessions_dir) / "safe-session.jsonl").read_text(
        encoding="utf-8"
    ) == ('{"type":"prior"}\n')
