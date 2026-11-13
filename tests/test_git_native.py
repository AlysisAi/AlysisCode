import os
from pathlib import Path

import pytest

from alysis_code.config import ConfigError
from alysis_code.git_native import native_argv, repository_metadata, run, safe_global_config
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.mark.parametrize(
    "command",
    [
        "git status; git reset --hard",
        "git -c alias.x=evil x",
        "git --git-dir=elsewhere commit -m x",
        "git rebase main | powershell",
        'git commit -m "$secret"',
    ],
)
def test_shell_expressions_and_global_overrides_are_not_native_requests(command):
    assert native_argv(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "git switch main",
        "git rebase main",
        "git merge --no-ff feature",
        "git stash push -u",
        "git worktree add child main",
        "git submodule update --init",
        "git commit -S -m signed",
    ],
)
def test_literal_native_operations(command):
    assert native_argv(command)


def test_explicit_ssh_command_is_literal_and_other_transport_overrides_are_rejected():
    command = (
        'git -c \'core.sshCommand="C:/Windows/System32/OpenSSH/ssh.exe" -F "C:/keys/config"\' '
    )
    assert native_argv(command + "clone -- ssh://example/repo child")
    assert native_argv(command + "ls-remote ssh://example/repo", inspection=True)
    assert native_argv(command + "fetch origin; evil") is None
    for config in ("protocol.allow=always", "core.hooksPath=elsewhere", "core.sshCommand="):
        assert native_argv("git -c '" + config + "' fetch origin") is None


@pytest.mark.parametrize("reference", ["stash@{0}", "'stash@{0}'", '"stash@{0}"'])
def test_reflog_selectors_stay_literal_git_arguments(reference):
    assert native_argv("git stash pop " + reference) == ["stash", "pop", "stash@{0}"]
    assert native_argv("git stash pop " + reference + "; evil") is None
    assert native_argv("git stash pop " + reference + "$(evil)") is None


def test_quoted_metacharacters_remain_data_and_expansion_stays_rejected():
    assert native_argv('git commit -m "Fix #42; keep literal"') == [
        "commit",
        "-m",
        "Fix #42; keep literal",
    ]
    assert native_argv("git commit -m 'Cost $5; $(not-code)'") == [
        "commit",
        "-m",
        "Cost $5; $(not-code)",
    ]
    assert native_argv("git commit -m 'It''s literal'") == ["commit", "-m", "It's literal"]
    assert native_argv('git commit -m "Expand $secret"') is None
    assert native_argv("git commit -m 'safe'; evil") is None


def test_native_read_family_never_selects_mutations_or_shell_expressions():
    assert native_argv("git log --graph --oneline --decorate -8", inspection=True)
    assert native_argv("git show --format=raw HEAD", inspection=True)
    assert native_argv("git reset --hard", inspection=True) is None
    assert native_argv("git log; git reset --hard", inspection=True) is None
    assert native_argv("git branch --list --verbose --no-abbrev", inspection=True)
    assert native_argv("git branch -avv", inspection=True)
    assert native_argv("git branch -D topic", inspection=True) is None
    assert native_argv("git branch new-branch", inspection=True) is None
    assert native_argv("git diff --cached --stat", inspection=True)
    assert native_argv("git diff --stat", inspection=True) is None
    assert native_argv("git check-attr --all -- base.txt", inspection=True) is None
    assert native_argv("git check-ignore -v base.txt", inspection=True) is None
    assert native_argv("git describe --dirty", inspection=True) is None


def test_local_config_queries_are_strictly_readonly():
    assert native_argv("git config --local --get core.hooksPath", inspection=True)
    for command in [
        "git config --local core.hooksPath evil",
        "git config --local --unset user.name",
        "git config --file elsewhere --get user.name",
        "git config --local --get --help",
        "git config --local --get core.hooksPath; evil",
        "git config --local --get invalid",
    ]:
        assert native_argv(command, inspection=True) is None


@pytest.mark.parametrize(
    "command",
    [
        "git config user.name Someone",
        "git config --local --unset user.name",
        "git remote add origin https://example.invalid/r.git",
        "git -C sub remote remove origin",
    ],
)
def test_configuration_and_remotes_are_native_requests_only_with_the_metadata(command):
    assert native_argv(command) is None
    assert native_argv(command, metadata_access=True)
    assert native_argv(command + "; evil", metadata_access=True) is None
    # An inspection never becomes a write because the metadata was approved.
    assert native_argv(command, inspection=True, metadata_access=True) is None


@pytest.mark.parametrize(
    ("argv", "plain"),
    [
        (["init"], True),
        (["init", "-q"], True),
        (["init", "-b", "main"], True),
        (["init", "--initial-branch=main", "folder"], True),
        (["-C", "sub", "init", "--quiet", "folder"], True),
        (["init", "one", "two"], False),
        (["init", "--template=elsewhere"], False),
        (["init", "--separate-git-dir", "elsewhere"], False),
        (["init", "--bare"], False),
        (["init", "--shared"], False),
        (["-c", "core.sshCommand=x", "init"], False),
        (["commit", "-m", "init"], False),
    ],
)
def test_only_a_plain_init_creates_a_repository_git_chose_every_byte_of(argv, plain):
    from alysis_code.git_native import creates_plain_repository

    assert creates_plain_repository(argv) is plain


def _git_directory(folder, *existing):
    folder.mkdir(parents=True)
    for name in existing:
        if name.endswith("/"):
            (folder / name[:-1]).mkdir(parents=True)
        else:
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text("x")
    return folder


def test_program_entries_of_an_ordinary_repository(tmp_path):
    from alysis_code.git_native import program_entries

    git = _git_directory(tmp_path / ".git", "config", "hooks/", "HEAD", "objects/", "refs/")
    entries = program_entries([git])
    assert set(entries.existing) == {git / "config", git / "hooks"}
    # Empty entries Git does not mind stand in for these, write-protected.
    assert dict(entries.empty) == {
        git / "config.worktree": False,
        git / "remotes": True,
        git / "branches": True,
        git / "modules": True,
        git / "worktrees": True,
    }
    # An empty one of these would stop Git or look like a rebase.
    assert dict(entries.taken) == {git / "commondir": False, git / "rebase-merge": True}
    assert entries.read_only == () and entries.own == ()
    # Nothing Git writes for a commit, a branch or a stash is among them.
    kept = {path.name for path in entries.existing} | {path.name for path, _ in entries.empty}
    kept |= {path.name for path, _ in entries.taken}
    assert not kept & {"HEAD", "index", "objects", "refs", "logs", "packed-refs", "ORIG_HEAD"}


def test_program_entries_protect_submodules_and_worktrees_as_a_whole(tmp_path):
    from alysis_code.git_native import program_entries

    git = _git_directory(
        tmp_path / ".git",
        "config",
        "hooks/",
        "modules/library/config",
        "worktrees/other/HEAD",
        "rebase-merge/git-rebase-todo",
    )
    entries = program_entries([git])
    assert {git / "modules", git / "worktrees", git / "rebase-merge"} <= set(entries.existing)
    assert git / "rebase-merge" not in dict(entries.taken)
    assert entries.read_only == () and entries.own == ()


def test_program_entries_in_a_linked_worktree_leave_the_others_to_their_git(tmp_path):
    from alysis_code.git_native import program_entries

    git = _git_directory(
        tmp_path / ".git",
        "config",
        "hooks/",
        "worktrees/mine/commondir",
        "worktrees/mine/HEAD",
        "worktrees/other/commondir",
        "worktrees/third/commondir",
    )
    mine = git / "worktrees" / "mine"
    entries = program_entries([mine, git])
    # The folder of worktrees is write-protected by a rule, with whatever is
    # in it now or later, and only this worktree's Git directory is writable.
    assert entries.read_only == (git / "worktrees",) and entries.own == (mine,)
    inside = [path for path in entries.existing if path.is_relative_to(git / "worktrees")]
    assert inside and all(path.is_relative_to(mine) for path in inside)
    assert mine / "commondir" in entries.existing
    assert {git / "config", git / "hooks"} <= set(entries.existing)
    assert (mine / "rebase-merge", True) in entries.taken
    assert (git / "rebase-merge", True) in entries.taken
    # From the main worktree nothing in there is this command's: denied whole.
    main = program_entries([git])
    assert git / "worktrees" in main.existing and main.read_only == () and main.own == ()
    # The same folder twice is the same folder.
    assert program_entries([git, git]) == main


@pytest.mark.skipif(os.name != "nt", reason="Windows keeps a name taken while it is being deleted")
def test_held_names_cannot_be_created_and_are_gone_afterwards(tmp_path):
    import subprocess

    from alysis_code._windows_git_reservations import HeldNames

    names = HeldNames()
    file, folder = tmp_path / "commondir", tmp_path / "rebase-merge"
    try:
        names.hold(file, directory=False)
        names.hold(folder, directory=True)
        for attempt in (
            f'echo planted> "{file}"',
            f'mkdir "{file}"',
            f'mkdir "{folder}"',
            f'echo planted> "{folder}\\git-rebase-todo"',
        ):
            done = subprocess.run(["cmd", "/c", attempt], capture_output=True, text=True)
            assert done.returncode != 0, attempt
        with pytest.raises(OSError):
            file.read_text()
        with pytest.raises(OSError):
            names.hold(file, directory=False)
    finally:
        names.close()
    assert list(tmp_path.iterdir()) == []
    # The name is free again for whoever needs it next.
    folder.mkdir()
    file.write_text("../elsewhere")


@pytest.mark.skipif(os.name != "nt", reason="Windows keeps a name taken while it is being deleted")
def test_holding_a_name_never_touches_what_is_already_there(tmp_path):
    from alysis_code._windows_git_reservations import HeldNames

    file, folder = tmp_path / "commondir", tmp_path / "rebase-merge"
    file.write_text("../elsewhere")
    folder.mkdir()
    (folder / "git-rebase-todo").write_text("pick 1234567 theirs")
    names = HeldNames()
    try:
        for path, directory in ((file, False), (folder, True)):
            with pytest.raises(FileExistsError):
                names.hold(path, directory=directory)
    finally:
        names.close()
    assert file.read_text() == "../elsewhere"
    assert (folder / "git-rebase-todo").read_text() == "pick 1234567 theirs"


@pytest.mark.skipif(os.name != "nt", reason="Windows keeps a name taken while it is being deleted")
def test_git_reader_opens_a_repository_while_its_commondir_name_is_held(tmp_path):
    """Git takes a name that cannot be opened as absent; the read library takes it as an error."""
    from dulwich.repo import Repo

    from alysis_code._windows_git_reservations import HeldNames
    from alysis_code.tools.git_readonly import _open, git_status

    Repo.init(str(tmp_path)).close()
    (tmp_path / "new.txt").write_text("new")
    names = HeldNames()
    try:
        names.hold(tmp_path / ".git" / "commondir", directory=False)
        with pytest.raises(PermissionError):
            (tmp_path / ".git" / "commondir").read_bytes()
        with _open(tmp_path) as repo:
            assert Path(repo.commondir()) == tmp_path / ".git"
        # Also where the library opens the repository again by itself.
        assert "new.txt" in git_status(root=tmp_path)["status"]
    finally:
        names.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows keeps a name taken while it is being deleted")
def test_git_reader_still_needs_the_commondir_of_a_linked_worktree(tmp_path):
    from dulwich.repo import Repo

    from alysis_code._windows_git_reservations import HeldNames
    from alysis_code.tools import git_readonly  # noqa: F401 - the reader's own lookup applies

    Repo.init(str(tmp_path)).close()
    # A Git directory without objects of its own is a linked worktree's.
    linked = tmp_path / ".git" / "worktrees" / "linked"
    linked.mkdir(parents=True)
    work = tmp_path / "linked"
    work.mkdir()
    (work / ".git").write_text("gitdir: " + linked.as_posix())
    names = HeldNames()
    try:
        names.hold(linked / "commondir", directory=False)
        with pytest.raises(PermissionError):
            Repo(str(work))
    finally:
        names.close()


def _rename_like_posix(source: Path, target: Path) -> None:
    """Rename as Git, Node and the MSYS tools do: replace the target, POSIX semantics."""
    import ctypes
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateFileW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        w.LPVOID,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ]
    kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    name = str(target)

    class RenameInformation(ctypes.Structure):
        _fields_ = [
            ("Flags", w.DWORD),
            ("RootDirectory", w.HANDLE),
            ("FileNameLength", w.DWORD),
            ("FileName", w.WCHAR * (len(name) + 1)),
        ]

    # DELETE access on the source, opened as itself whether file or folder.
    handle = kernel.CreateFileW(str(source), 0x00010000, 0x7, None, 3, 0x02200000, None)
    assert handle != w.HANDLE(-1).value
    try:
        information = RenameInformation(0x1 | 0x2, None, len(name) * 2, name)
        # FileRenameInfoEx: replace if it exists, with POSIX semantics.
        if not kernel.SetFileInformationByHandle(
            handle, 22, ctypes.byref(information), ctypes.sizeof(information)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows keeps a name taken while it is being deleted")
@pytest.mark.parametrize("rename", ["classic", "posix"])
def test_nothing_can_be_renamed_onto_a_held_name(tmp_path, rename):
    """A prepared entry moved into place in one step, replacing whatever is there."""
    from alysis_code._windows_git_reservations import HeldNames

    move = os.replace if rename == "classic" else _rename_like_posix
    # The call does replace a file that exists, where nothing is held.
    (tmp_path / "source").write_text("new")
    (tmp_path / "existing").write_text("old")
    move(tmp_path / "source", tmp_path / "existing")
    assert (tmp_path / "existing").read_text() == "new"
    names = HeldNames()
    file, folder = tmp_path / "commondir", tmp_path / "rebase-merge"
    prepared_file, prepared_folder = tmp_path / "prepared", tmp_path / "prepared-folder"
    prepared_file.write_text("../elsewhere")
    prepared_folder.mkdir()
    (prepared_folder / "git-rebase-todo").write_text("exec planted")
    try:
        names.hold(file, directory=False)
        names.hold(folder, directory=True)
        with pytest.raises(OSError):
            move(prepared_file, file)
        # (The classic call never replaces a folder; the other one does.)
        with pytest.raises(OSError):
            move(prepared_folder, folder)
        assert prepared_file.read_text() == "../elsewhere"
        assert (prepared_folder / "git-rebase-todo").is_file()
    finally:
        names.close()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "existing",
        "prepared",
        "prepared-folder",
    ]
    if rename == "posix":
        # With the names free again the same call puts the folder in place.
        folder.mkdir()
        move(prepared_folder, folder)
        assert (folder / "git-rebase-todo").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows keeps a name taken while it is being deleted")
def test_a_second_git_command_finds_the_repository_while_a_name_is_held(tmp_path):
    """It read ".git/commondir", which the running command holds, and failed with access denied."""
    from alysis_code._windows_git_reservations import HeldNames

    git = _git_directory(tmp_path / ".git", "config", "HEAD", "objects/", "refs/")
    names = HeldNames()
    try:
        names.hold(git / "commondir", directory=False)
        assert repository_metadata(tmp_path, [tmp_path]) == [git]
    finally:
        names.close()
    assert repository_metadata(tmp_path, [tmp_path]) == [git]
    # A linked worktree's Git directory has no objects and cannot do without the file.
    linked = _git_directory(git / "worktrees" / "side", "HEAD")
    work = tmp_path / "side"
    work.mkdir()
    (work / ".git").write_text("gitdir: " + linked.as_posix())
    names = HeldNames()
    try:
        names.hold(linked / "commondir", directory=False)
        with pytest.raises(PermissionError):
            repository_metadata(work, [tmp_path])
    finally:
        names.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows keeps a name taken while it is being deleted")
def test_a_separate_git_directory_is_still_reported_while_its_name_is_held(tmp_path):
    """Reported as nothing, the directory would not be write-protected for a command starting then."""
    from alysis_code._windows_git_reservations import HeldNames
    from alysis_code.git_safe import separate_git_directories

    project = tmp_path / "project"
    project.mkdir()
    real = _git_directory(tmp_path / "real-git", "config", "HEAD", "objects/")
    (project / ".git").write_text("gitdir: " + real.as_posix())
    assert separate_git_directories(project) == (real,)
    names = HeldNames()
    try:
        names.hold(real / "commondir", directory=False)
        assert separate_git_directories(project) == (real,)
    finally:
        names.close()


def test_a_plain_init_is_new_only_where_no_repository_is(tmp_path):
    from alysis_code.git_native import _init_target

    roots = [tmp_path]
    assert _init_target(["init"], tmp_path, roots) == (tmp_path, True)
    assert _init_target(["init", "-b", "main", "new"], tmp_path, roots) == (tmp_path / "new", True)
    assert _init_target(["init", "--initial-branch", "new", "-q"], tmp_path, roots) == (
        tmp_path,
        True,
    )
    # The empty entry that reserves the name is not a repository.
    (tmp_path / ".git").mkdir()
    assert _init_target(["init"], tmp_path, roots) == (tmp_path, True)
    # Anything in it is: a hook somebody put there runs when Git creates HEAD.
    prepared = tmp_path / "prepared"
    (prepared / ".git" / "hooks").mkdir(parents=True)
    assert _init_target(["init", "prepared"], tmp_path, roots) == (prepared, False)
    (tmp_path / "pointer").mkdir()
    (tmp_path / "pointer" / ".git").write_text("gitdir: ../prepared/.git")
    assert _init_target(["init", "pointer"], tmp_path, roots) == (tmp_path / "pointer", False)
    for outside in (["init", ".."], ["init", str(tmp_path.parent / "elsewhere")]):
        with pytest.raises(ConfigError, match="inside the approved folders"):
            _init_target(outside, tmp_path, roots)
    if os.name == "nt":
        import _winapi

        # A link named .git is never a new repository, wherever it leads.
        (tmp_path / "empty").mkdir()
        (tmp_path / "linked").mkdir()
        _winapi.CreateJunction(str(tmp_path / "empty"), str(tmp_path / "linked" / ".git"))
        assert _init_target(["init", "linked"], tmp_path, roots) == (tmp_path / "linked", False)


def test_readonly_and_disabled_git_policy_refuse_before_any_preparation(tmp_path):
    for settings in (
        ShellSandboxSettings(filesystem_access="read-only"),
        ShellSandboxSettings(safe_git_writes=False),
    ):
        with pytest.raises(ConfigError, match="disabled"):
            run(
                settings=settings,
                root=tmp_path,
                cwd=tmp_path,
                argv=["reset", "--hard"],
                timeout_s=30,
                cancel_check=None,
            )
    assert list(tmp_path.iterdir()) == []


def test_linked_metadata_requires_explicit_folder_grant(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / ".git").write_text("gitdir: " + str(outside))
    with pytest.raises(ConfigError, match="outside the approved"):
        repository_metadata(work, [work])
    assert repository_metadata(work, [work, outside]) == [outside]


def test_global_preferences_do_not_forward_secrets_or_programs(tmp_path, monkeypatch):
    from alysis_code import windows_toolchain

    monkeypatch.setattr(windows_toolchain, "installed_tools", lambda: {})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".gitconfig").write_text(
        "[user]\nname = User\nemail = user@example.invalid\n[credential]\nhelper = !secret\n[http]\nextraHeader = AUTHORIZATION secret\n[core]\nhooksPath = outside\nautocrlf = true\n"
    )
    path = tmp_path / "safe"
    safe_global_config(path)
    value = path.read_text()
    assert "user@example.invalid" in value and "autocrlf = true" in value
    assert "secret" not in value and "hooksPath" not in value


@pytest.mark.skipif(os.name != "nt", reason="Windows handle identity")
def test_reservations_remove_only_exclusive_unchanged_creations(tmp_path):
    from alysis_code._windows_git_reservations import GitReservations

    existing = tmp_path / "AGENTS.md"
    existing.write_text("user instructions")
    missing = tmp_path / "CLAUDE.md"
    reservations = GitReservations()
    try:
        reservations.reserve(tmp_path, [str(existing), str(missing)])
        assert missing.exists()
    finally:
        reservations.close()
    assert existing.read_text() == "user instructions"
    assert not missing.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle identity")
def test_reservation_cleanup_preserves_a_modified_object(tmp_path):
    from alysis_code._windows_git_reservations import GitReservations

    path = tmp_path / "AGENTS.md"
    reservations = GitReservations()
    reservations.reserve(tmp_path, [str(path)])
    for pin in reservations.pins:
        pin.close()
    path.write_text("legitimate concurrent content")
    reservations.close()
    assert path.read_text() == "legitimate concurrent content"


def test_untrusted_tool_manifest_is_refused(tmp_path, monkeypatch):
    from alysis_code import windows_toolchain

    monkeypatch.setattr(windows_toolchain, "program_files", lambda: tmp_path)
    monkeypatch.setattr(windows_toolchain, "__file__", str(tmp_path / "windows_toolchain.py"))
    folder = tmp_path / "assets/windows-git"
    folder.mkdir(parents=True)
    (folder / "bundle-manifest.json").write_text('{"files": {}}')
    with pytest.raises(ValueError, match="manifest integrity"):
        windows_toolchain.installed_tools()
