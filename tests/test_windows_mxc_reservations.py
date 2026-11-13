"""Taking back the names a background command reserved in a project.

These tests make and remove real entries on NTFS and need no sandbox helper.
``tests/test_windows_mxc.py`` runs the same thing through real background
commands when a helper is supplied.
"""

from __future__ import annotations

import ctypes
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="NTFS handles and Windows file sharing")

if os.name == "nt":
    from ctypes import wintypes

    from alysis_code import windows_mxc_reservations as registry
    from alysis_code._windows_git_reservations import GitReservations, remove_reservations
    from alysis_code._windows_managed_session import _Api

READ = 0x1  # read a file, list a folder: the access that takes part in sharing
ANYONE = 0x7  # shares reading, writing and deleting
NOT_DELETE = 0x3


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    workspace = (tmp_path / "project").resolve()
    workspace.mkdir()
    (workspace / "README.md").write_text("the person's file\n")
    return workspace


def _protected(workspace: Path) -> list[str]:
    """What the runner asks to have reserved: folders end with a separator."""
    return [
        str(workspace / ".git") + os.sep,
        str(workspace / ".claude") + os.sep,
        str(workspace / ".github" / "skills") + os.sep,
        str(workspace / "AGENTS.md"),
        str(workspace / "CLAUDE.md"),
    ]


def _reserve(workspace: Path):
    """Make the missing entries the way a background command does, and stop holding them."""
    reservations = GitReservations()
    reservations.reserve(workspace, _protected(workspace))
    return reservations.detach()


def _names(workspace: Path) -> set[str]:
    return {path.relative_to(workspace).as_posix() for path in workspace.rglob("*")}


class _OtherProgram:
    """A handle some other program has on an entry."""

    def __init__(self, path: Path, *, share: int) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateFileW(str(path), READ, share, None, 3, 0x02000000, None)
        assert self.handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.kernel.CloseHandle(self.handle)


def _identity(path: Path) -> tuple[int, bytes]:
    """The identity the engine records for ``path``."""
    api = _Api()
    handle = api.kernel.CreateFileW(str(path), 0x80, ANYONE, None, 3, 0x02200000, None)
    try:
        return api.info(handle).identity()
    finally:
        api.close(handle)


RESERVED = {".git", ".claude", ".github", ".github/skills", "AGENTS.md", "CLAUDE.md"}


def test_detached_entries_stay_and_are_reported_with_what_they_are(project):
    made = _reserve(project)
    assert _names(project) == RESERVED | {"README.md"}
    assert {path.relative_to(project).as_posix() for path, _, _ in made} == RESERVED
    assert {path.name for path, _, directory in made if not directory} == {
        "AGENTS.md",
        "CLAUDE.md",
    }
    removed, busy = remove_reservations(made)
    assert busy == [] and len(removed) == len(made)
    assert _names(project) == {"README.md"}


def test_entries_stay_while_the_command_that_made_them_runs(project):
    record = registry.BackgroundRecord(project, _reserve(project))
    assert registry.remove_unheld_reservations(project) == []
    assert _names(project) == RESERVED | {"README.md"}
    assert record.path.exists()
    record.let_go()
    removed = registry.remove_unheld_reservations(project)
    assert len(removed) == len(RESERVED)
    assert _names(project) == {"README.md"}
    assert not registry.records_folder(project).exists()


def test_the_last_command_to_stop_takes_back_what_any_of_them_made(project):
    first = registry.BackgroundRecord(project, _reserve(project))
    # The second command finds everything in place and makes nothing itself.
    assert _reserve(project) == []
    second = registry.BackgroundRecord(project, [])
    first.let_go()
    assert registry.remove_unheld_reservations(project) == []
    assert _names(project) == RESERVED | {"README.md"}
    second.let_go()
    assert len(registry.remove_unheld_reservations(project)) == len(RESERVED)
    assert _names(project) == {"README.md"}
    assert not registry.records_folder(project).exists()


def test_what_the_person_already_had_is_never_recorded_or_removed(project):
    (project / "AGENTS.md").write_text("")
    (project / ".claude").mkdir()
    made = _reserve(project)
    assert {path.relative_to(project).as_posix() for path, _, _ in made} == RESERVED - {
        "AGENTS.md",
        ".claude",
    }
    registry.BackgroundRecord(project, made).let_go()
    registry.remove_unheld_reservations(project)
    assert _names(project) == {"README.md", "AGENTS.md", ".claude"}


def test_an_entry_that_is_no_longer_the_empty_one_that_was_made_stays(project):
    record = registry.BackgroundRecord(project, _reserve(project))
    (project / "AGENTS.md").write_text("# Written by the person meanwhile\n")
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    os.rmdir(project / ".claude")
    (project / ".claude").mkdir()  # the person's own folder under the same name
    os.remove(project / "CLAUDE.md")
    record.let_go()
    removed = registry.remove_unheld_reservations(project)
    assert {path.relative_to(project).as_posix() for path in removed} == {
        ".github",
        ".github/skills",
    }
    assert _names(project) == {"README.md", "AGENTS.md", ".git", ".git/HEAD", ".claude"}
    # Nothing here is worth another try: the records are gone.
    assert not registry.records_folder(project).exists()


def test_a_reserved_folder_the_person_put_something_into_stays_and_is_not_tried_again(project):
    registry.BackgroundRecord(project, _reserve(project)).let_go()
    (project / ".github" / "workflows").mkdir()
    (project / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    removed = registry.remove_unheld_reservations(project)
    assert ".github" not in {path.name for path in removed}
    assert _names(project) == {
        "README.md",
        ".github",
        ".github/workflows",
        ".github/workflows/ci.yml",
    }
    assert not registry.records_folder(project).exists()


def test_a_reserved_file_that_was_made_read_only_stays_and_is_not_tried_again(project):
    registry.BackgroundRecord(project, _reserve(project)).let_go()
    os.chmod(project / "AGENTS.md", stat.S_IREAD)
    try:
        registry.remove_unheld_reservations(project)
        assert _names(project) == {"README.md", "AGENTS.md"}
        assert not registry.records_folder(project).exists()
    finally:
        os.chmod(project / "AGENTS.md", stat.S_IWRITE)


def test_a_name_is_free_at_once_even_while_another_program_has_the_entry_open(project):
    """Otherwise the next command, which makes the entry anew, fails on the name."""
    registry.BackgroundRecord(project, _reserve(project)).let_go()
    with (
        _OtherProgram(project / "AGENTS.md", share=ANYONE),
        _OtherProgram(project / ".claude", share=ANYONE),
    ):
        assert len(registry.remove_unheld_reservations(project)) == len(RESERVED)
        assert _names(project) == {"README.md"}
        assert not registry.records_folder(project).exists()
        # What the next command does: make them again, under the same names.
        again = _reserve(project)
        assert {path.relative_to(project).as_posix() for path, _, _ in again} == RESERVED
        remove_reservations(again)
    assert _names(project) == {"README.md"}


def test_an_entry_something_keeps_from_being_deleted_is_tried_again_later(project):
    record = registry.BackgroundRecord(project, _reserve(project))
    record.let_go()
    with (project / "AGENTS.md").open("rb"):
        removed = registry.remove_unheld_reservations(project)
        assert (project / "AGENTS.md").exists()
        assert len(removed) == len(RESERVED) - 1
        (kept,) = registry.records_folder(project).iterdir()
        assert [entry[0] for entry in json.loads(kept.read_text())["entries"]] == [
            str(project / "AGENTS.md")
        ]
    assert [path.name for path in registry.remove_unheld_reservations(project)] == ["AGENTS.md"]
    assert _names(project) == {"README.md"}
    assert not registry.records_folder(project).exists()


def test_a_folder_is_tried_again_together_with_the_entry_inside_it_that_is_kept(project):
    registry.BackgroundRecord(project, _reserve(project)).let_go()
    with _OtherProgram(project / ".github" / "skills", share=NOT_DELETE):
        registry.remove_unheld_reservations(project)
        assert _names(project) == {"README.md", ".github", ".github/skills"}
        (kept,) = registry.records_folder(project).iterdir()
        again = {Path(entry[0]).name for entry in json.loads(kept.read_text())["entries"]}
        assert again == {".github", "skills"}
    registry.remove_unheld_reservations(project)
    assert _names(project) == {"README.md"}
    assert not registry.records_folder(project).exists()


def test_a_record_left_by_an_engine_process_that_is_gone_is_acted_on(project):
    made = _reserve(project)
    script = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "from alysis_code import windows_mxc_reservations as registry\n"
        "entries = [(Path(p), (v, bytes.fromhex(i)), d) for p, v, i, d in json.loads(sys.argv[2])]\n"
        "registry.BackgroundRecord(Path(sys.argv[1]), entries)\n"
        "os._exit(0)\n"
    )
    encoded = json.dumps(
        [
            [str(path), identity[0], identity[1].hex(), directory]
            for path, identity, directory in made
        ]
    )
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    subprocess.run(
        [sys.executable, "-c", script, str(project), encoded], check=True, env=environment
    )
    (left,) = registry.records_folder(project).iterdir()
    assert left.suffix == ".json"  # nobody let it go; its holder just ended
    assert len(registry.remove_unheld_reservations(project)) == len(RESERVED)
    assert _names(project) == {"README.md"}


def test_a_record_cannot_name_anything_outside_its_project(project, tmp_path):
    outside = tmp_path / "outside.txt"
    inside = project / "inside.txt"
    outside.write_text("")
    inside.write_text("")
    folder = registry.records_folder(project)
    folder.mkdir(parents=True)

    def record(path: Path, workspace: Path) -> str:
        identity = _identity(path)
        return json.dumps(
            {
                "workspace": str(workspace),
                "entries": [[str(path), identity[0], identity[1].hex(), False]],
            }
        )

    (folder / "forged.ended").write_text(record(outside, project))
    (folder / "garbage.ended").write_text("{not json")
    (folder / "elsewhere.ended").write_text(record(outside, tmp_path))
    # The same record for a file inside the project is acted on, so what
    # saves the file outside is where it is, not how the record is written.
    (folder / "inside.ended").write_text(record(inside, project))
    assert registry.remove_unheld_reservations(project) == [inside]
    assert outside.exists() and not inside.exists()
    assert not folder.exists()


def test_projects_do_not_share_records(project, tmp_path):
    other = (tmp_path / "other").resolve()
    other.mkdir()
    record = registry.BackgroundRecord(project, _reserve(project))
    registry.BackgroundRecord(other, _reserve(other)).let_go()
    assert registry.records_folder(project) != registry.records_folder(other)
    assert len(registry.remove_unheld_reservations(other)) == len(RESERVED)
    assert _names(project) == RESERVED | {"README.md"}
    record.let_go()
    registry.remove_unheld_reservations(project)


def test_an_entry_is_recorded_under_the_innermost_project_that_holds_it(project):
    inner = project / "packages" / "app"
    inner.mkdir(parents=True)
    outer_entry = (project / "AGENTS.md", (1, b"a"), False)
    inner_entry = (inner / "AGENTS.md", (1, b"b"), False)
    stray = (project.parent / "AGENTS.md", (1, b"c"), False)
    assert registry.by_project([project, inner], [outer_entry, inner_entry, stray]) == {
        project: [outer_entry],
        inner: [inner_entry],
    }


def test_a_record_that_could_not_be_written_is_not_left_to_be_acted_on(project, monkeypatch):
    made = _reserve(project)

    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(registry.os, "fsync", fail)
    with pytest.raises(OSError, match="disk full"):
        registry.BackgroundRecord(project, made)
    assert list(registry.records_folder(project).iterdir()) == []
    remove_reservations(made)


def test_reserved_files_on_record_are_listed_while_they_are_still_empty(project):
    record = registry.BackgroundRecord(project, _reserve(project))
    assert set(registry.recorded_files(project)) == {project / "AGENTS.md", project / "CLAUDE.md"}
    (project / "AGENTS.md").write_text("# The person's now\n")
    assert registry.recorded_files(project) == [project / "CLAUDE.md"]
    record.let_go()
    registry.remove_unheld_reservations(project)
    assert registry.recorded_files(project) == []


def _watched(project, operation):
    from alysis_code.agent.turn.snapshot import _run_with_command_mutation_detection

    return _run_with_command_mutation_detection(root=project, enabled=True, operation=operation)


def test_taking_back_a_reservation_is_not_a_change_the_command_made(project):
    registry.BackgroundRecord(project, _reserve(project)).let_go()
    result, touched = _watched(project, lambda: registry.remove_unheld_reservations(project))
    assert len(result) == len(RESERVED) and touched == []


def test_a_reservation_appearing_is_not_a_change_the_command_made(project):
    """A background command starts in another thread while a command is watched."""
    made, touched = _watched(project, lambda: _reserve(project))
    assert touched == [] and _names(project) == RESERVED | {"README.md"}
    remove_reservations(made)


def test_a_reservation_taken_back_and_made_anew_is_not_a_change_either(project):
    registry.BackgroundRecord(project, _reserve(project)).let_go()

    def replaced():
        registry.remove_unheld_reservations(project)
        return _reserve(project)

    made, touched = _watched(project, replaced)
    assert touched == []
    remove_reservations(made)


def test_a_command_that_deletes_or_writes_the_persons_file_is_still_noticed(project):
    (project / "AGENTS.md").write_text("")
    (project / "notes.txt").write_text("")
    registry.BackgroundRecord(project, _reserve(project)).let_go()

    def command():
        registry.remove_unheld_reservations(project)
        os.remove(project / "AGENTS.md")
        os.remove(project / "notes.txt")

    assert _watched(project, command)[1] == ["AGENTS.md", "notes.txt"]


def test_a_command_that_writes_into_a_reserved_file_is_still_noticed(project):
    made = _reserve(project)
    _, touched = _watched(project, lambda: (project / "AGENTS.md").write_text("planted\n"))
    assert touched == ["AGENTS.md"]
    os.remove(project / "AGENTS.md")
    remove_reservations(made)


def test_a_reservation_the_engine_made_and_something_else_deleted_is_noticed(project):
    """Only the engine's own removal is excused, not the file's disappearing as such."""
    made = _reserve(project)
    _, touched = _watched(project, lambda: os.remove(project / "AGENTS.md"))
    assert touched == ["AGENTS.md"]
    remove_reservations(made)


def test_a_reserved_file_the_person_added_to_git_is_theirs_and_stays(project):
    """Their Git works while a command runs, and "git add ." takes the empty files along."""
    from dulwich import porcelain

    porcelain.init(str(project))
    record = registry.BackgroundRecord(project, _reserve(project))
    porcelain.add(str(project), [str(project / "AGENTS.md")])
    record.let_go()
    removed = {path.name for path in registry.remove_unheld_reservations(project)}
    assert "AGENTS.md" not in removed and "CLAUDE.md" in removed
    assert (project / "AGENTS.md").is_file() and not (project / "CLAUDE.md").exists()
    assert not registry.records_folder(project).exists()


def test_a_tracked_file_whose_name_a_reservation_took_is_not_mistaken_for_a_staged_one(project):
    """The person deleted a tracked AGENTS.md; the empty file now under that name is ours."""
    from dulwich import porcelain

    porcelain.init(str(project))
    (project / "AGENTS.md").write_text("# The project's instructions\n")
    porcelain.add(str(project), [str(project / "AGENTS.md")])
    os.remove(project / "AGENTS.md")
    registry.BackgroundRecord(project, _reserve(project)).let_go()
    assert (project / "AGENTS.md").stat().st_size == 0
    removed = {path.name for path in registry.remove_unheld_reservations(project)}
    assert "AGENTS.md" in removed and not (project / "AGENTS.md").exists()


def test_a_reserved_file_staged_in_a_repository_above_the_project_stays(project):
    """A repository of several projects: "git add ." at its top takes the file along."""
    from dulwich import porcelain

    inner = project / "packages" / "app"
    inner.mkdir(parents=True)
    porcelain.init(str(project))
    reservations = GitReservations()
    reservations.reserve(inner, [str(inner / "AGENTS.md"), str(inner / "CLAUDE.md")])
    record = registry.BackgroundRecord(inner, reservations.detach())
    porcelain.add(str(project), [str(inner / "AGENTS.md")])
    record.let_go()
    removed = {path.name for path in registry.remove_unheld_reservations(inner)}
    assert removed == {"CLAUDE.md"} and (inner / "AGENTS.md").is_file()
