"""Adversarial synthetic I/O under production run_turn and the real shell guard."""

import json
import os
import shlex
import sys

import pytest
from test_file_skills import (
    POSIX_FILES_CONTRACT,
    Surface,
    cli,
    isolated_runtime,  # noqa: F401 -- registers the autouse isolation fixture
    results,
    session_for,
    shell_json,
)

pytestmark = POSIX_FILES_CONTRACT


@pytest.mark.parametrize(
    "case", ["parent_swap", "after_open", "growth", "destination_race", "permission"]
)
def test_staging_descriptor_races_and_denial_in_real_admitted_child(tmp_path, case):
    root = tmp_path / "workspace"
    root.mkdir()
    parent = tmp_path / "external"
    parent.mkdir()
    source = parent / "source.txt"
    source.write_text("original")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "source.txt").write_text("other")
    harness = root / "competing_actor.py"
    setup = (
        "from pathlib import Path\nimport os\nfrom alysis_code import file_staging as m\n"
        f"source=Path({str(source)!r}); parent=source.parent; replacement=Path({str(replacement)!r})\n"
        "changed=False\n"
    )
    if case == "permission":
        code = (
            "original=m.os.open\ndef open_(path, *args, **kwargs):\n"
            " if str(path) == source.name: raise PermissionError('synthetic folder privacy denial')\n"
            " return original(path, *args, **kwargs)\nm.os.open=open_\n"
        )
        expected = "permission_denied"
    elif case == "destination_race":
        # Direct staging creates the output before reading. Schedule the
        # competing entry immediately before its actual exclusive creation.
        code = (
            "original=m.os.open\ndef open_(path, flags, *args, **kwargs):\n global changed\n"
            " if str(path)=='copy.txt' and flags & os.O_CREAT and not changed:\n"
            "  changed=True\n  Path('copy.txt').write_text('preserve competing destination')\n"
            " return original(path, flags, *args, **kwargs)\nm.os.open=open_\n"
        )
        expected = "destination_conflict"
    elif case == "after_open":
        code = (
            "original=m.os.open\ndef open_(path, *args, **kwargs):\n global changed\n"
            " fd=original(path, *args, **kwargs)\n if str(path)==source.name and not changed:\n"
            "  changed=True\n  os.replace(replacement/'source.txt', source)\n return fd\nm.os.open=open_\n"
        )
        expected = "stale_source"
    else:
        change = {
            "parent_swap": "parent.rename(parent.with_name('old-external')); parent.symlink_to(replacement, target_is_directory=True)",
            "growth": "source.write_bytes(b'x' * 100)",
        }[case]
        setup += "m.MAX_BYTES=32\n" if case == "growth" else ""
        code = (
            "original=m.os.read\ndef read(fd, size):\n global changed\n if not changed:\n"
            "  changed=True\n  " + change + "\n return original(fd, size)\nm.os.read=read\n"
        )
        expected = {"parent_swap": "stale_source", "growth": "size_limit"}[case]
    harness.write_text(setup + code + "raise SystemExit(m.main())\n")
    command = shlex.join(
        [sys.executable, str(harness), "--source", str(source), "--output", "copy.txt"]
    )
    surface = Surface()
    session, client, host = session_for(
        root,
        [("shell_run", {"cmd": command})],
        mode="review",
        surface=surface,
        final="The named file could not be staged; I stopped.",
    )
    try:
        assert session.run_turn(f"Read the saved file {source} only.") == 0
        response = results(client)[0]
        assert (
            response["exit_code"] == 1 and json.loads(response["stdout"])["error_code"] == expected
        )
        assert len(surface.approvals) == 1 and len(client.requests) == 2 and host == []
        if case == "destination_race":
            assert (root / "copy.txt").read_text() == "preserve competing destination"
        elif case in {"parent_swap", "growth"}:
            assert (root / "copy.txt").read_text() == ("original" if case == "parent_swap" else "")
        else:
            assert not (root / "copy.txt").exists()
        assert not list(root.glob(".alysis-stage-*"))
        if case in {"parent_swap", "growth"}:
            cleanup = json.loads(response["stdout"])["cleanup"]
            assert cleanup["status"] == "retained_unverified"
            assert cleanup["entries"] == [{"path": "copy.txt", "observed": "present"}]
        else:
            assert "cleanup" not in json.loads(response["stdout"])
    finally:
        session.close()


@pytest.mark.parametrize(
    "case", ["directory", "fifo", "destination_symlink", "outside_destination"]
)
def test_staging_refuses_nonregular_sources_and_destination_escape(tmp_path, case):
    source = tmp_path / "source"
    if case == "directory":
        source.mkdir()
    elif case == "fifo":
        os.mkfifo(source)
    else:
        source.write_text("original")
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "untouched.txt"
    target.write_text("preserve")
    output = "copy.txt"
    if case == "destination_symlink":
        (root / output).symlink_to(target)
    if case == "outside_destination":
        output = "../untouched.txt"
    session, client, host = session_for(
        root, [("shell_run", {"cmd": cli("file_staging", "--source", source, "--output", output)})]
    )
    try:
        assert session.run_turn(f"Read the saved file {source}.") == 0
        response = results(client)[0]
        assert response["exit_code"] == 1
        assert json.loads(response["stdout"])["error_code"] == (
            "invalid_source"
            if case in {"directory", "fifo"}
            else "destination_conflict"
            if case == "destination_symlink"
            else "invalid_destination"
        )
        assert target.read_text() == "preserve" and host == []
    finally:
        session.close()


def test_staging_quoted_tilde_is_exact_current_home_file(tmp_path, monkeypatch):
    home = tmp_path / "synthetic-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = home / "named file.txt"
    source.write_text("saved content")
    session, client, host = session_for(
        tmp_path / "workspace",
        [
            ("capability_status", {}),
            (
                "shell_run",
                {
                    "cmd": cli(
                        "file_staging", "--source", "~/named file.txt", "--output", "copy.txt"
                    )
                },
            ),
        ],
    )
    try:
        assert session.run_turn("Read ~/named file.txt.") == 0
        assert results(client)[0]["file_staging"]["max_bytes"] == 50 * 1024 * 1024
        assert shell_json(client)["status"] == "staged"
        assert (tmp_path / "workspace/copy.txt").read_text() == source.read_text() and host == []
    finally:
        session.close()
