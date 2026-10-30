"""W4a-F1–F3: actual admitted CLI, deterministic competing filesystem actors."""

import hashlib
import json
import shlex
import stat
import sys
from textwrap import dedent

import pytest
from test_file_skills import (
    POSIX_FILES_CONTRACT,
    Surface,
    cli,
    isolated_runtime,  # noqa: F401 -- registers the autouse isolation fixture
    results,
    session_for,
)

pytestmark = POSIX_FILES_CONTRACT


@pytest.mark.parametrize("mode", ["review", "fullaccess"])
@pytest.mark.parametrize("override", ["outside", "child", "symlink"])
def test_cli_rejects_workspace_rebinding_before_creating_any_file(tmp_path, mode, override):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    child = root / "child"
    child.mkdir()
    alias = root / "outside-alias"
    alias.symlink_to(outside, target_is_directory=True)
    requested = {"outside": outside, "child": child, "symlink": alias}[override]
    source = tmp_path / "named-source.txt"
    source.write_text("exact named source")
    audit = tmp_path / "created.json"
    harness = root / "actor.py"
    harness.write_text(
        dedent(f"""
        import json, os
        from pathlib import Path
        from alysis_code import file_staging as m
        original = m.os.open
        created = []
        def opened(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                created.append(str(path))
            return original(path, flags, *args, **kwargs)
        m.os.open = opened
        result = m.main()
        Path({str(audit)!r}).write_text(json.dumps(created))
        raise SystemExit(result)
    """)
    )
    command = shlex.join(
        [
            sys.executable,
            str(harness),
            "--workspace",
            str(requested),
            "--source",
            str(source),
            "--output",
            "copy.txt",
        ]
    )
    surface = Surface()
    session, client, host = session_for(
        root,
        [("shell_run", {"cmd": command})],
        mode=mode,
        surface=surface,
        final="The destination cannot be rebound; no file was staged.",
    )
    try:
        assert session.run_turn(f"Read only {source}; stage inside this chat workspace.") == 0
        response = results(client)[0]
        assert response["exit_code"] == 1
        assert json.loads(response["stdout"])["error_code"] == "invalid_destination"
        assert json.loads(audit.read_text()) == [], (
            "rebinding must fail before any temporary or output creation"
        )
        assert not list(tmp_path.rglob("copy.txt")) and not list(tmp_path.rglob(".alysis-stage-*"))
        assert source.read_text() == "exact named source"
        assert len(surface.approvals) == 1 and host == []
    finally:
        session.close()


@pytest.mark.parametrize("mode", ["review", "fullaccess"])
@pytest.mark.parametrize(
    "case",
    [
        "output_fsync_rewrite",
        "output_before_verify_rewrite",
        "output_rewrite_during_verify",
        "root_before_create",
        "source_swap_before_copy",
        "competing_output",
        "destination_appears",
        "root_after_fsync",
        "output_swap_before_verify",
        "source_rewrite_after_copy",
    ],
)
def test_cli_publication_races_refuse_and_report_unverified_entries(tmp_path, mode, case):
    root = tmp_path / "workspace"
    root.mkdir()
    source = tmp_path / "source.txt"
    source.write_text("saved source data")
    marker = tmp_path / "actor.json"
    harness = root / "actor.py"
    harness.write_text(
        dedent(f"""
        import json, os
        from pathlib import Path
        from alysis_code import file_staging as m
        root = Path({str(root)!r})
        source = Path({str(source)!r})
        case = {case!r}
        changed = False
        def trigger():
            global changed
            if changed:
                return
            changed = True
            if case in ('root_before_create', 'root_after_fsync'):
                root.rename(root.with_name('moved-workspace'))
                root.mkdir()
            elif case == 'source_swap_before_copy':
                source.rename(source.with_name('original-source.txt'))
                source.write_text('unrelated replacement')
            elif case == 'source_rewrite_after_copy':
                source.write_text('forged saved data')
            elif case in ('output_fsync_rewrite', 'output_before_verify_rewrite', 'output_rewrite_during_verify'):
                (root / 'copy.txt').write_text('forged saved data')
            elif case in ('competing_output', 'output_swap_before_verify'):
                (root / 'copy.txt').unlink()
                (root / 'copy.txt').write_text('preserve competing occupant')
            else:
                (root / 'copy.txt').write_text('preserve competing occupant')
            Path({str(marker)!r}).write_text(json.dumps({{'triggered': case}}))
        # Former temporary-file rewrites now target the sole output at fsync,
        # just before descriptor verification, or during its verification read.
        # The swapped-input case still proves no unrelated pathname is reopened.
        original_open = m.os.open
        def opened(path, flags, *args, **kwargs):
            publishing = str(path) == 'copy.txt' and flags & os.O_CREAT
            if publishing and case in ('root_before_create', 'destination_appears'):
                trigger()
            fd = original_open(path, flags, *args, **kwargs)
            if publishing and case in ('competing_output', 'source_swap_before_copy'):
                trigger()
            return fd
        m.os.open = opened
        original_fsync = m.os.fsync
        def synced(fd):
            result = original_fsync(fd)
            if case in ('output_fsync_rewrite', 'root_after_fsync', 'output_swap_before_verify', 'source_rewrite_after_copy'):
                trigger()
            return result
        m.os.fsync = synced
        original_seek = m.os.lseek
        verifying_fd = None
        def seek(fd, *args):
            global verifying_fd
            verifying_fd = fd
            if case == 'output_before_verify_rewrite':
                trigger()
            return original_seek(fd, *args)
        m.os.lseek = seek
        original_read = m.os.read
        def read(fd, size):
            result = original_read(fd, size)
            if fd == verifying_fd and case == 'output_rewrite_during_verify':
                trigger()
            return result
        m.os.read = read
        raise SystemExit(m.main())
    """)
    )
    command = shlex.join(
        [sys.executable, str(harness), "--source", str(source), "--output", "copy.txt"]
    )
    surface = Surface()
    session, client, host = session_for(
        root,
        [("shell_run", {"cmd": command})],
        mode=mode,
        surface=surface,
        final="The staging check failed; no verified copy was produced.",
    )
    try:
        assert session.run_turn(f"Read only the saved file {source}.") == 0
        response = results(client)[0]
        assert json.loads(marker.read_text()) == {"triggered": case}, (
            "competing actor must actually execute"
        )
        assert response["exit_code"] == 1, response
        result = json.loads(response["stdout"])
        assert result["status"] == "error" and result["retryable"] is False
        expected_error = (
            "destination_conflict"
            if case == "destination_appears"
            else "stale_source"
            if case in {"source_swap_before_copy", "source_rewrite_after_copy"}
            else "stale_destination"
        )
        assert result["error_code"] == expected_error
        assert "sha256" not in result, "a failed publication has no success digest"
        copies = list(tmp_path.rglob("copy.txt"))
        if case in ("competing_output", "destination_appears", "output_swap_before_verify"):
            assert copies == [root / "copy.txt"]
            assert copies[0].read_text() == "preserve competing occupant"
        else:
            expected_root = (
                root.with_name("moved-workspace")
                if case in {"root_before_create", "root_after_fsync"}
                else root
            )
            assert copies == [expected_root / "copy.txt"]
            expected_bytes = {
                "root_before_create": "",
                "root_after_fsync": "saved source data",
                "source_swap_before_copy": "saved source data",
                "source_rewrite_after_copy": "saved source data",
                "output_fsync_rewrite": "forged saved data",
                "output_before_verify_rewrite": "forged saved data",
                "output_rewrite_during_verify": "forged saved data",
            }[case]
            assert copies[0].read_text() == expected_bytes
        assert not list(tmp_path.rglob(".alysis-stage-*"))
        if case == "destination_appears":
            assert "cleanup" not in result, (
                "a failed exclusive create never establishes a cleanup candidate"
            )
        else:
            assert result["cleanup"]["status"] == "retained_unverified"
            assert result["cleanup"]["entries"] == [{"path": "copy.txt", "observed": "present"}]
        if case == "source_swap_before_copy":
            assert source.read_text() == "unrelated replacement"
            assert source.with_name("original-source.txt").read_text() == "saved source data"
            assert copies[0].read_text() != source.read_text(), (
                "unrelated replacement bytes are never copied"
            )
        else:
            assert source.read_text() == (
                "forged saved data" if case == "source_rewrite_after_copy" else "saved source data"
            )
        assert len(surface.approvals) == 1 and host == []
        assert len(client.requests) == 2, "no retry or alternate access route"
    finally:
        session.close()


@pytest.mark.parametrize("mode", ["review", "fullaccess"])
@pytest.mark.parametrize(
    "data",
    [b"", b"saved source data\x00\xff", bytes(range(256)) * 513],
    ids=["empty", "binary", "multiple_chunks"],
)
def test_cli_same_execution_workspace_succeeds_with_only_output_and_actual_digest(
    tmp_path, mode, data
):
    root = tmp_path / "workspace"
    root.mkdir()
    source = tmp_path / "source.txt"
    source.write_bytes(data)
    command = cli("file_staging", "--workspace", root, "--source", source, "--output", "copy.txt")
    surface = Surface()
    session, client, host = session_for(
        root, [("shell_run", {"cmd": command})], mode=mode, surface=surface
    )
    try:
        before = set(root.iterdir())
        assert session.run_turn(f"Read the saved file {source}.") == 0
        response = results(client)[0]
        assert response["exit_code"] == 0
        result = json.loads(response["stdout"])
        actual = (root / "copy.txt").read_bytes()
        assert actual == source.read_bytes()
        assert result["sha256"] == hashlib.sha256(actual).hexdigest() and result["bytes"] == len(
            actual
        )
        assert set(root.iterdir()) - before == {root / "copy.txt"}, (
            "successful staging creates exactly one output and no temporary duplicate"
        )
        assert not list(root.glob(".alysis-stage-*"))
        assert stat.S_IMODE((root / "copy.txt").stat().st_mode) == 0o600
        assert "cleanup" not in result, "a success has no unverified cleanup artifacts"
        assert len(surface.approvals) == 1 and host == []
    finally:
        session.close()
