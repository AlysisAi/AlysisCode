"""What a Windows sandbox command may read of the person's home and installed programs.

``tests/test_windows_mxc.py`` reads and writes such folders in the real
sandbox when a helper is supplied.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from alysis_code import windows_home_access as access

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows profile layout and links")


def _make(base: Path, *names: str) -> None:
    for name in names:
        path = base / name
        if name.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")


@pytest.fixture
def home(tmp_path):
    profile = tmp_path / "Users" / "person"
    roaming = profile / "AppData" / "Roaming"
    roaming.mkdir(parents=True)
    (profile / "AppData" / "Local").mkdir()
    return profile.resolve(), roaming.resolve()


@pytest.fixture
def tools(home):
    """A PATH folder outside the home, for programs that are evidence by their names."""
    folder = home[0].parent / "tools"
    folder.mkdir()
    return folder


def _names(paths, base):
    return {path.relative_to(base).as_posix() for path in paths if path.is_relative_to(base)}


def _read(home, *, programs=(), program_roots=(), program_data=None, apart=()):
    result = access.readable_places(
        *home,
        programs=programs,
        program_roots=program_roots,
        program_data=program_data,
        apart=apart,
    )
    return _names(result.read, home[0]), _names(result.deny, home[0]), result


def test_a_tools_folder_in_the_home_is_readable_and_the_persons_folders_are_not(home):
    profile, _ = home
    _make(
        profile,
        ".rustup/settings.toml",
        ".cargo/bin/cargo.exe",
        ".m2/repository/",
        "Documents/letter.txt",
        "Desktop/todo.txt",
        "Downloads/",
        "OneDrive - Company/plan.txt",
        "Dropbox/notes.txt",
        "projects/other/main.py",
        "NTUSER.DAT",
    )
    read, _, _ = _read(home)
    assert read == {".rustup", ".cargo", ".m2"}


@pytest.mark.parametrize(
    "name",
    sorted(name for name in access._CREDENTIAL_FOLDERS if name.startswith(".")),
)
def test_a_credential_folder_in_the_home_is_never_readable(home, name):
    profile, _ = home
    _make(profile, f"{name}/content", ".tool/state")
    assert _read(home)[0] == {".tool"}


@pytest.mark.parametrize("name", sorted(access._PERSONAL_FOLDERS - {"appdata"}))
def test_the_persons_own_folder_is_not_a_tools_even_with_a_program_in_it(home, name):
    """A PATH folder inside "Documents" says nothing about the documents."""
    profile, _ = home
    _make(profile, f"{name}/bin/tool.exe")
    programs = [profile / name / "bin", profile / name]
    assert _read(home, programs=programs)[0] == set()


def test_a_folder_without_a_dot_is_a_tools_only_when_a_path_program_lives_in_it(home):
    profile, _ = home
    _make(
        profile,
        "go/bin/gopls.exe",
        "go/pkg/mod/",
        "scoop/shims/",
        "code/tools/bin/",
        "work/scripts/deploy.ps1",
        "Dropbox/bin/",
    )
    programs = [
        profile / "go" / "bin",
        profile / "scoop",
        profile / "code" / "tools" / "bin",
        profile / "work" / "scripts",
        profile / "Dropbox" / "bin",
    ]
    read, _, _ = _read(home, programs=programs)
    # "~/code" and "~/work" hold the person's projects as well: a program two
    # folders down, or in a folder that is not an installation's program
    # folder, says nothing about the rest. A synced folder never counts.
    assert read == {"go", "scoop"}


def test_files_in_the_home_are_readable_unless_they_hold_credentials_or_history(home):
    profile, _ = home
    _make(
        profile,
        ".gitconfig",
        ".condarc",
        ".npmrc",
        ".netrc",
        ".git-credentials",
        ".pgpass",
        ".erlang.cookie",
        ".bash_history",
        ".python_history",
        ".vault-token",
        ".env",
        ".gitconfig.lock",
        "notes.txt",
    )
    read, _, _ = _read(home)
    # A lock file is being written by some program right now: one that is
    # gone when the command starts would fail the command.
    assert read == {".gitconfig", ".condarc"}


def test_other_coding_agents_keep_their_homes(home, tools):
    profile, roaming = home
    _make(
        profile,
        ".claude/projects/chat.jsonl",
        ".claude.json",
        ".codex/sessions/",
        ".gemini/",
        ".aider.chat.history.md",
        ".cursor/",
        ".opencode/",
        ".continuum/data",
    )
    # In the roaming data the agent's own program on the PATH would otherwise
    # make its folder "a tool's".
    _make(tools, "claude.cmd", "codex.cmd", "gemini.cmd", "opencode.exe", "cursor.cmd")
    _make(
        roaming,
        "Claude-3p/claude_desktop_config.json",
        "claude-code/state",
        "codex/auth",
        "gemini-cli/state",
        "opencode/state",
        "Cursor/state",
    )
    read, _, _ = _read(home, programs=[tools])
    assert read == {".continuum"}


def test_a_credential_inside_a_tools_folder_is_refused_by_name(home):
    profile, _ = home
    _make(
        profile,
        ".cargo/credentials.toml",
        ".cargo/config.toml",
        ".cargo/registry/",
        ".tool/auth.json",
        ".tool/api_token",
        ".tool/tokenizer.json",
        ".tool/secrets/",
        ".tool/server.pem",
    )
    read, deny, _ = _read(home)
    assert read == {".cargo", ".tool"}
    assert deny == {
        ".cargo/credentials.toml",
        ".tool/auth.json",
        ".tool/api_token",
        ".tool/secrets",
        ".tool/server.pem",
    }


def test_further_down_only_a_credentials_exact_name_is_refused(home):
    """A package cache holds thousands of folders and files with names like "js-tokens"."""
    profile, _ = home
    _make(
        profile,
        ".cache/huggingface/token",
        ".cache/pip/http/",
        ".local/share/agent/auth.json",
        ".local/share/agent/settings.json",
        ".pkgs/cache/js-tokens/index.js",
        ".pkgs/cache/password-strength/lib.js",
        ".pkgs/one/two/three/four/auth.json",
    )
    read, deny, _ = _read(home)
    assert read == {".cache", ".local", ".pkgs"}
    # Four folders down nothing is looked at any more.
    assert deny == {".cache/huggingface/token", ".local/share/agent/auth.json"}


def test_a_program_in_a_path_folder_is_not_refused_for_its_name(home):
    """ "secretlint.cmd" in the folder of global npm packages ran before this."""
    profile, roaming = home
    _make(roaming, "npm/secretlint.cmd", "npm/git-credential-x.cmd", "npm/npm.cmd", "npm/auth.json")
    _, deny, _ = _read(home, programs=[roaming / "npm"])
    assert deny == {"AppData/Roaming/npm/auth.json"}


def test_a_roaming_folder_is_readable_on_evidence_that_it_is_a_tools(home, tools):
    profile, roaming = home
    _make(
        tools,
        "uv.exe",
        "python.exe",
        "poetry.cmd",
        "go.exe",
        "node.exe",
        "npm.cmd",
        "file.exe",
        "view.exe",
        "sync.exe",
        "code.cmd",
    )
    _make(
        roaming,
        "uv/python/cpython-3.13/python.exe",
        "Python/Python314/site-packages/",
        "npm/node_modules/",
        "npm-cache/",
        "pypoetry/config.toml",
        "NuGet/NuGet.Config",
        "create-next-app-nodejs/config.json",
        "some-cli-nodejs/Config/config.json",
        "Telegram Desktop/tdata/key_datas",
        "AnyDesk/user.conf",
        "Google/Chrome/",
        "FileZilla/sitemanager.xml",
        "TeamViewer/",
        "Resilio Sync/",
        "Codeium/",
        "SomeGame/save.dat",
    )
    _make(profile, ".nuget/packages/")
    read, _, _ = _read(home, programs=[tools, roaming / "npm"])
    assert {name for name in read if name.startswith("AppData")} == {
        "AppData/Roaming/uv",
        "AppData/Roaming/Python",
        "AppData/Roaming/npm",
        "AppData/Roaming/npm-cache",
        "AppData/Roaming/pypoetry",
        # Named like the ".nuget" folder in the home.
        "AppData/Roaming/NuGet",
    }


@pytest.mark.parametrize("name", ["Microsoft", "gnupg", "gh", "GitHub CLI", "gcloud"])
def test_a_credential_folder_in_the_roaming_data_is_refused_whatever_the_evidence(
    home, tools, name
):
    profile, roaming = home
    # A program of that very name on the PATH, and a PATH folder inside it.
    _make(tools, f"{name}.exe", "uv.exe")
    _make(roaming, f"{name}/bin/tool.exe", "uv/python/")
    read, _, _ = _read(home, programs=[tools, roaming / name / "bin"])
    assert read == {"AppData/Roaming/uv"}


def test_a_browser_or_an_app_built_on_one_keeps_its_profile(home, tools):
    profile, roaming = home
    # Each of them has its program on the PATH, so only what the folder holds
    # can keep it unreadable.
    _make(tools, "code.cmd", "slack.exe", "mozilla.exe", "thunderbird.exe", "scoop.cmd")
    _make(
        roaming,
        "Code/Local State",
        "Code/User/settings.json",
        "Slack/Cookies",
        "Thunderbird/profiles.ini",
        "Mozilla/Firefox/profiles.ini",
        "Mozilla/Extensions/",
    )
    _make(
        profile,
        ".cache/chrome-devtools-mcp/chrome-profile/Local State",
        ".cache/pip/http/",
        "scoop/shims/",
        "scoop/persist/firefox/profile/logins.json",
        "scoop/persist/git/config",
    )
    read, deny, _ = _read(home, programs=[tools, profile / "scoop" / "shims"])
    # The app's own folder is not readable at all; a profile further down is
    # refused where it lies and the rest of the tool's folder is read.
    assert read == {".cache", "scoop", "AppData/Roaming/Mozilla"}
    assert deny == {
        ".cache/chrome-devtools-mcp/chrome-profile",
        "scoop/persist/firefox/profile",
        "AppData/Roaming/Mozilla/Firefox",
    }


def test_the_keys_of_a_running_local_service_are_refused(home, tools):
    """With them and a local connection a command could have a running kernel run code."""
    profile, roaming = home
    _make(tools, "jupyter.exe")
    _make(
        roaming,
        "jupyter/runtime/kernel-1.json",
        "jupyter/kernels/python3/kernel.json",
    )
    _make(profile, ".ipython/profile_default/security/kernel-1.json", ".ipython/profile_default/db")
    read, deny, _ = _read(home, programs=[tools])
    assert read == {".ipython", "AppData/Roaming/jupyter"}
    assert deny == {"AppData/Roaming/jupyter/runtime", ".ipython/profile_default/security"}


def test_nothing_readable_lies_in_what_is_apart(home):
    profile, _ = home
    _make(profile, ".private/inner/state", ".other/x")
    read, _, _ = _read(home, apart=[profile / ".private"])
    assert read == {".other"}
    read, _, _ = _read(home, apart=[profile])
    assert read == set()


def test_a_tools_folder_that_holds_something_apart_is_readable_beside_it(home, tools):
    """A project in "~/go/src", a cache the command may write, the engine among uv's tools."""
    profile, roaming = home
    _make(
        profile,
        ".cargo/bin/cargo.exe",
        ".cargo/registry/cache/",
        ".cargo/git/",
        ".cargo/config.toml",
        ".cargo/.package-cache",
        ".cargo/credentials.toml",
        "go/bin/gopls.exe",
        "go/pkg/mod/",
        "go/src/mine/project/main.go",
        "go/src/theirs/other/main.go",
    )
    _make(roaming, "uv/python/cpython/", "uv/tools/engine/Scripts/", "uv/tools/other/")
    _make(tools, "uv.exe")
    read, deny, _ = _read(
        home,
        programs=[tools, profile / "go" / "bin"],
        apart=[
            profile / ".cargo" / "registry",
            profile / "go" / "src" / "mine" / "project",
            roaming / "uv" / "tools" / "engine",
        ],
    )
    assert read == {
        ".cargo/bin",
        ".cargo/git",
        ".cargo/config.toml",
        ".cargo/.package-cache",
        "go/bin",
        "go/pkg",
        "AppData/Roaming/uv/python",
    }
    # The folder that holds the project is not opened up: other projects in
    # "~/go/src" stay unreadable, and so do the other tools beside the engine.
    assert deny == set()


def test_program_folders_are_readable_whole_unless_something_apart_lies_inside(home, tmp_path):
    programs = tmp_path / "Program Files"
    _make(programs, "Git/cmd/git.exe", "Java/jdk/bin/java.exe", "The App/runtime/engine.py")
    programs = programs.resolve()
    _, _, whole = _read(home, program_roots=[programs])
    assert whole.read == [programs]
    _, _, split = _read(home, program_roots=[programs], apart=[programs / "The App" / "runtime"])
    assert set(split.read) == {programs / "Git", programs / "Java"}
    _, _, inside = _read(home, program_roots=[programs], apart=[tmp_path.resolve()])
    assert inside.read == []


def test_a_programdata_folder_needs_the_same_evidence_as_a_roaming_one(home, tools, tmp_path):
    """A remote-desktop service keeps its settings there as much as a package manager."""
    data = tmp_path / "ProgramData"
    _make(
        data,
        "chocolatey/bin/choco.exe",
        "chocolatey/lib/tool/tools/tool.exe",
        "uv/python/",
        "AnyDesk/service.conf",
        "Claude/managed-settings.json",
        "ssh/ssh_host_ed25519_key",
        "Microsoft/VisualStudio/Packages/_Instances/1/state.json",
        "Microsoft/Network/Connections/Pbk/rasphone.pbk",
    )
    data = data.resolve()
    _make(tools, "uv.exe", "ssh.exe", "anydesktop.exe", "claude.cmd")
    _, _, result = _read(home, programs=[tools, data / "chocolatey" / "bin"], program_data=data)
    assert set(result.read) == {
        data / "chocolatey",
        data / "uv",
        # Where the Visual Studio installer records what is installed.
        data / "Microsoft" / "VisualStudio",
    }
    assert result.deny == []


def test_a_link_is_never_offered(home, tmp_path):
    import _winapi

    profile, roaming = home
    outside = tmp_path / "elsewhere"
    _make(outside, "secret.txt")
    _make(profile, ".real/x")
    _winapi.CreateJunction(str(outside), str(profile / ".linked"))
    _winapi.CreateJunction(str(outside), str(profile / ".real" / "token-link"))
    read, deny, _ = _read(home)
    assert read == {".real"} and deny == set()


def test_a_name_the_helper_cannot_be_sent_is_never_offered(home):
    """Windows allows a name that is not valid Unicode; the request is UTF-8."""
    profile, _ = home
    _make(profile, ".tool/state")
    try:
        (profile / ".odd\udc80").mkdir()
    except OSError:
        pytest.skip("the file system refuses the name")
    assert _read(home)[0] == {".tool"}


def test_what_is_offered_follows_the_folders_as_they_change(home):
    """What a folder held is remembered; a command must never be offered what is gone."""
    profile, _ = home
    _make(profile, ".tool/state", ".gone/state")
    assert _read(home)[0] == {".tool", ".gone"}
    (profile / ".gone" / "state").unlink()
    (profile / ".gone").rmdir()
    _make(profile, ".tool/credentials.json", ".new/state")
    read, deny, _ = _read(home)
    assert read == {".tool", ".new"} and deny == {".tool/credentials.json"}
    (profile / ".tool" / "credentials.json").unlink()
    assert _read(home)[1] == set()


def test_what_appears_further_down_is_seen_when_the_folder_is_looked_into_again(home, monkeypatch):
    """A change several folders down does not show in the folder's own time."""
    profile, _ = home
    _make(profile, ".app/Default/state")
    assert _read(home)[1] == set()
    _make(profile, ".app/Default/Cookies")
    # Remembered for a while: looking several folders down for every command
    # would cost more than the command.
    assert _read(home)[1] == set()
    monkeypatch.setattr(access, "_INSPECTION_AGE_S", 0.0)
    assert _read(home)[1] == {".app/Default"}


def test_a_folder_that_cannot_be_looked_through_is_not_readable(home, monkeypatch):
    profile, _ = home
    _make(profile, *(f".big/file{n}" for n in range(8)), ".big/zz-credentials.json", ".small/x")
    monkeypatch.setattr(access, "_SCAN_LIMIT", 5)
    assert _read(home)[0] == {".small"}


def test_a_folder_with_a_credential_that_cannot_be_refused_is_not_readable(home, monkeypatch):
    """The helper takes no path with a trailing dot, so such a file cannot be named to it."""
    profile, _ = home
    _make(profile, ".tool/credentials.toml", ".other/x")
    acceptable = access._acceptable
    monkeypatch.setattr(
        access, "_acceptable", lambda path: path.name != "credentials.toml" and acceptable(path)
    )
    read, deny, _ = _read(home)
    assert read == {".other"} and deny == set()


def test_the_command_is_told_where_the_home_really_is(home, monkeypatch):
    profile, roaming = home
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setattr(access, "_own_profile", lambda: profile)
    assert access.person_home() == home
    variables = access.home_environment(*home)
    assert variables["USERPROFILE"] == str(profile) and variables["APPDATA"] == str(roaming)
    assert variables["HOMEDRIVE"] + variables["HOMEPATH"] == str(profile)
    # The local application data and the temp folder are not among them:
    # those stay the command's own.
    assert not {"LOCALAPPDATA", "TEMP", "TMP"} & set(variables)


@pytest.mark.parametrize("value", ["", "relative\\home", "Z:\\no\\such\\profile"])
def test_a_home_that_cannot_be_used_leaves_the_previous_arrangement(value, monkeypatch):
    monkeypatch.setenv("USERPROFILE", value)
    assert access.person_home() is None


def test_a_home_windows_does_not_have_for_the_person_is_not_used(home, monkeypatch):
    """The Windows folder API answers from USERPROFILE inside the sandbox.

    Below a profile Windows did not make, the folder for the command's local
    data does not exist and the command cannot make it: a program that asks
    the API would get nothing and write into the project.
    """
    profile, roaming = home
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("APPDATA", str(roaming))
    assert access._own_profile() != profile
    assert access.person_home() is None
    monkeypatch.setattr(access, "_own_profile", lambda: None)
    assert access.person_home() is None


def test_roaming_data_that_cannot_be_used_is_left_out_alone(home, monkeypatch):
    profile, _ = home
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setattr(access, "_own_profile", lambda: profile)
    for unusable in (profile / "no-such-folder", profile, profile.parent, Path(profile.anchor)):
        monkeypatch.setenv("APPDATA", str(unusable))
        # A value that names the profile or a folder above it would have the
        # profile's own folders judged as application data.
        assert access.person_home() == (profile, None), unusable
    assert "APPDATA" not in access.home_environment(profile, None)


@pytest.mark.parametrize(
    ("folder", "program", "expected"),
    [
        ("uv", "uv", True),
        ("Python", "python", True),
        ("npm-cache", "npm", True),
        ("pypoetry", "poetry", True),
        (".anaconda", "conda", True),
        ("Google", "go", False),
        ("Uvicorn", "uv", False),
        ("opencode", "code", False),
        ("Codeium", "code", False),
        ("some-cli-nodejs", "node", False),
        ("TP-Link", "link", False),
        ("Visual Studio Setup", "setup", False),
        ("FileZilla", "file", False),
        ("TeamViewer", "view", False),
        ("Resilio Sync", "sync", False),
    ],
)
def test_named_like_a_program(folder, program, expected):
    assert access._named_like(folder, frozenset({program})) is expected
