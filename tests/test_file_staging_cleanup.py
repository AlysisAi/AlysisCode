"""F4: replacements after the cleanup observation survive real guarded CLI runs."""

import json
import shlex
import sys
from textwrap import dedent

import pytest
from test_file_skills import (
    POSIX_FILES_CONTRACT,
    Surface,
    isolated_runtime,  # noqa: F401 -- registers the autouse isolation fixture
    results,
    session_for,
)

pytestmark = POSIX_FILES_CONTRACT


@pytest.mark.parametrize("mode", ["review", "fullaccess"])
@pytest.mark.parametrize("cleanup_path", ["output", "unrelated_temporary"])
def test_cleanup_never_unlinks_a_replacement_after_its_observation(tmp_path, mode, cleanup_path):
    root = tmp_path / "workspace"
    root.mkdir()
    source = tmp_path / "source.txt"
    source.write_text("saved source data")
    marker = tmp_path / "actor.json"
    harness = root / "actor.py"
    harness.write_text(
        dedent(f"""
        import json, os, sys
        from pathlib import Path
        from alysis_code import file_staging as m
        root = Path({str(root)!r})
        marker = Path({str(marker)!r})
        cleanup_path = {cleanup_path!r}
        original_stat, original_unlink, original_fsync = m.os.stat, m.os.unlink, m.os.fsync
        changed = False
        helper_unlinks = []
        # There is no helper temporary file or temporary cleanup anymore. Keep
        # the former success-path guarantee with a competing hidden entry: even
        # a replacement at fsync must never be read, copied or deleted.
        unrelated = root / '.alysis-stage-competitor'
        if cleanup_path == 'unrelated_temporary':
            unrelated.write_text('original unrelated file')
            retained = unrelated.open('rb')
        def replace(path, old, dir_fd=None):
            original_unlink(path, dir_fd=dir_fd)
            destination = root / str(path)
            destination.write_text('preserve cleanup competitor')
            replacement = original_stat(path, dir_fd=dir_fd, follow_symlinks=False)
            marker.write_text(json.dumps({{'name': destination.name, 'owned_inode': old.st_ino,
                'competing_inode': replacement.st_ino, 'bytes': destination.read_text()}}))
        def observed(path, *args, **kwargs):
            global changed
            result = original_stat(path, *args, **kwargs)
            caller = sys._getframe(1).f_code.co_name
            cleanup = caller in ('_remove_owned_entry', '_retain_cleanup_entry')
            target = cleanup_path == 'output' and str(path) == 'copy.txt'
            if cleanup and target and not changed:
                changed = True
                # Return the actual old stat result AFTER a real unlink/create.
                # On the base, the next helper unlink destroys this competitor.
                replace(path, result, kwargs['dir_fd'])
            return result
        m.os.stat = observed
        def unlinked(path, *args, **kwargs):
            helper_unlinks.append(str(path))
            return original_unlink(path, *args, **kwargs)
        m.os.unlink = unlinked
        def synced(fd):
            global changed
            result = original_fsync(fd)
            if cleanup_path == 'output' and (root / 'copy.txt').exists():
                (root / 'copy.txt').write_text('forged saved data')
            elif cleanup_path == 'unrelated_temporary' and not changed:
                changed = True
                replace(unrelated, original_stat(unrelated, follow_symlinks=False))
            return result
        m.os.fsync = synced
        code = m.main()
        audit = json.loads(marker.read_text())
        audit['helper_unlinks'] = helper_unlinks
        marker.write_text(json.dumps(audit))
        raise SystemExit(code)
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
        final="I will not use or delete unverified cleanup entries.",
    )
    try:
        assert session.run_turn(f"Read only the saved file {source}.") == 0
        response = results(client)[0]
        result = json.loads(response["stdout"])
        audit = json.loads(marker.read_text())
        assert audit["owned_inode"] != audit["competing_inode"]
        assert audit["bytes"] == "preserve cleanup competitor"
        competitor = root / audit["name"]
        assert competitor.exists(), "a replacement arriving after the observation must survive"
        assert competitor.read_text() == "preserve cleanup competitor"
        assert competitor.stat().st_ino == audit["competing_inode"]
        assert audit["helper_unlinks"] == [], "cleanup must not delete mutable names"
        if cleanup_path == "output":
            assert response["exit_code"] == 1 and result["error_code"] == "stale_destination"
            assert "sha256" not in result
            assert result["cleanup"]["status"] == "retained_unverified"
            assert result["cleanup"]["entries"] == [{"path": "copy.txt", "observed": "present"}]
            assert "unverified" in result["cleanup"]["message"].lower()
            assert not list(root.glob(".alysis-stage-*"))
        else:
            assert response["exit_code"] == 0 and result["status"] == "staged"
            assert (root / "copy.txt").read_text() == "saved source data"
            assert "cleanup" not in result
            assert list(root.glob(".alysis-stage-*")) == [competitor]
        assert source.read_text() == "saved source data"
        assert len(surface.approvals) == 1
        assert len(client.requests) == 2 and host == []
    finally:
        session.close()
