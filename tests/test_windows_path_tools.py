"""Which folders of the person's PATH a Windows sandbox command may read and use.

``tests/test_windows_mxc.py`` runs a program from such a folder in the real
sandbox when a helper is supplied.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from alysis_code import windows_mxc as mxc
from alysis_code import windows_path_tools as tools
from alysis_code import windows_sandbox as native
from alysis_code.config import ConfigError
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A profile, a Program Files folder and a project, all under ``tmp_path``."""
    home = tmp_path / "Users" / "person"
    for folder in (home / "AppData" / "Roaming", home / "AppData" / "Local"):
        folder.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))

    def local_path(path, **_):
        if not Path(path).is_dir():
            raise ConfigError(f"Sandbox directory does not exist: {path}")
        return Path(path).resolve()

    monkeypatch.setattr(native, "_local_path", local_path)
    if os.name != "nt":
        # The helper takes drive-letter paths only. Elsewhere every other rule is still tested.
        monkeypatch.setattr(tools, "_plain", lambda path: True)
    programs = tmp_path / "Program Files"
    project = tmp_path / "project"
    programs.mkdir()
    project.mkdir()
    return tmp_path, home, programs, project


def _tool(folder: Path, name: str = "tool.exe") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"MZ")
    return folder


def _folders(path, *, readable=(), apart=(), programs=()):
    return tools.host_path_folders(
        os.pathsep.join(map(str, path)), readable=readable, apart=apart, program_files=programs
    )


def test_a_folder_on_the_path_is_readable_and_keeps_its_place(machine):
    _, home, _, project = machine
    first = _tool(home / ".local" / "bin")
    second = _tool(home / "AppData" / "Local" / "Programs" / "Python" / "Scripts")
    grants, entries = _folders([second, first], apart=[project])
    assert grants == [second, first]
    assert entries == [second, first]


def test_a_program_files_folder_brings_its_whole_installation(machine):
    _, _, programs, project = machine
    cmd = _tool(programs / "Git" / "cmd", "git.exe")
    mingw = _tool(programs / "Git" / "mingw64" / "bin", "git.exe")
    cli = _tool(programs / "GitHub CLI", "gh.exe")
    grants, entries = _folders([cmd, mingw, cli], apart=[project], programs=[programs])
    assert grants == [programs / "Git", programs / "GitHub CLI"]
    assert entries == [cmd, mingw, cli]
    # Program Files itself is never one installation.
    assert _folders([programs], apart=[project], programs=[programs]) == ([programs], [programs])


def test_a_folder_the_command_can_read_already_needs_no_grant(machine):
    _, _, programs, _ = machine
    node = _tool(programs / "nodejs", "node.exe")
    inside = _tool(programs / "nodejs" / "extra")
    grants, entries = _folders([node, inside], readable=[node], programs=[programs])
    assert grants == [] and entries == [node, inside]


def test_one_folder_spelled_twice_is_taken_once(machine):
    _, home, _, _ = machine
    folder = _tool(home / "bin")
    spellings = [str(folder), str(folder) + os.sep, f'"{folder}"']
    grants, entries = _folders(spellings)
    assert grants == [folder] and entries == [folder]


def test_a_grant_inside_another_is_not_repeated(machine):
    _, home, _, _ = machine
    install = _tool(home / "AppData" / "Local" / "Programs" / "Python314", "python.exe")
    (install / "Lib").mkdir()
    (install / "Lib" / "os.py").write_text("")
    scripts = _tool(install / "Scripts", "pip.exe")
    assert _folders([scripts, install]) == ([install], [scripts, install])
    assert _folders([install, scripts]) == ([install], [install, scripts])


def test_what_is_not_a_folder_is_left_out(machine):
    tmp_path, home, _, _ = machine
    real = _tool(home / "bin")
    a_file = home / "bin" / "tool.exe"
    grants, entries = _folders(
        ["", "relative\\bin", "%SystemRoot%\\x", home / "missing", a_file, real]
    )
    assert grants == [real] and entries == [real]


def test_a_folder_the_sandbox_refuses_is_left_out_without_failing_the_command(machine, monkeypatch):
    _, home, _, _ = machine
    linked, plain = _tool(home / "linked"), _tool(home / "plain")
    resolve = native._local_path

    def local_path(path, **options):
        if Path(path) == linked:
            raise ConfigError(
                "Sandbox roots and working directories cannot traverse reparse points"
            )
        return resolve(path, **options)

    monkeypatch.setattr(native, "_local_path", local_path)
    assert _folders([linked, plain]) == ([plain], [plain])


def test_a_folder_that_contains_or_lies_inside_an_approved_or_private_folder_is_left_out(machine):
    tmp_path, home, _, project = machine
    private = home / "AppData" / "Roaming" / "Alysis Code"
    inside_project = _tool(project / "node_modules" / ".bin")
    inside_private = _tool(private / "runtime" / "Scripts")
    above_project = tmp_path
    ordinary = _tool(home / "tools")
    grants, entries = _folders(
        [inside_project, inside_private, above_project, ordinary], apart=[project, private]
    )
    assert grants == [ordinary] and entries == [ordinary]


def test_a_folder_holding_the_persons_home_or_application_data_is_left_out(machine):
    tmp_path, home, _, _ = machine
    _tool(home)
    _tool(home / "AppData" / "Roaming")
    _tool(home / "AppData" / "Local")
    below = _tool(home / "AppData" / "Roaming" / "npm", "tsc.cmd")
    grants, entries = _folders(
        [
            home.parent,
            home,
            home / "AppData",
            home / "AppData" / "Roaming",
            home / "AppData" / "Local",
            below,
        ]
    )
    assert grants == [below] and entries == [below]


def test_a_drive_root_is_left_out(machine):
    _, home, _, _ = machine
    assert _folders([Path(home.anchor)]) == ([], [])


@pytest.mark.parametrize("name", ["python.exe", "python.bat", "python3.cmd", "python"])
def test_a_folder_that_offers_python_without_being_an_installation_is_left_out(machine, name):
    """A virtual environment's script folder, a version manager's shims."""
    _, home, _, _ = machine
    scripts = _tool(home / "project" / ".venv" / "Scripts", name)
    assert _folders([scripts]) == ([], [])


def test_a_python_installation_and_its_script_folder_are_kept(machine):
    _, home, _, _ = machine
    install = _tool(home / "Python314", "python.exe")
    (install / "Lib").mkdir()
    (install / "Lib" / "os.py").write_text("")
    scripts = _tool(install / "Scripts", "pytest.exe")
    versioned = _tool(home / ".local" / "bin", "python3.13.exe")
    grants, entries = _folders([install, scripts, versioned])
    assert grants == [install, versioned] and entries == [install, scripts, versioned]


@pytest.mark.skipif(os.name != "nt", reason="creates a Windows junction")
def test_a_folder_that_holds_links_instead_of_programs_is_left_out(machine):
    """The Store's app aliases are links the sandbox cannot start."""
    _, home, _, _ = machine
    target = _tool(home / "elsewhere")
    aliases = _tool(home / "WindowsApps")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(aliases / "python-alias"), str(target)],
        capture_output=True,
    )
    assert made.returncode == 0, made.stderr
    try:
        assert _folders([aliases, target]) == ([target], [target])
    finally:
        os.rmdir(aliases / "python-alias")


@pytest.mark.parametrize(
    "text",
    ["C:\\tools\\bin", "D:/tools"],
)
def test_paths_the_helper_accepts(text):
    assert tools._plain(Path(text))


@pytest.mark.parametrize(
    "text",
    [
        "\\\\server\\share\\bin",
        "C:tools",
        "C:\\tools\\..\\bin",
        "C:\\tools \\bin",
        "C:\\tools.\\bin",
        "C:\\a:b\\bin",
    ],
)
def test_paths_the_helper_would_refuse_are_never_offered(text):
    # Read as Windows text on every system: elsewhere a backslash is part of a name.
    assert not tools._plain(PureWindowsPath(text))


def test_the_engines_and_the_applications_own_folders_are_named(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DESKTOP_USER_DATA", str(tmp_path / "userData"))
    monkeypatch.setenv(
        "ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(tmp_path / "control"), "", 5])
    )
    folders = tools.private_and_runtime_folders()
    assert Path(tools.__file__).parent.resolve() in folders
    assert Path(sys.prefix).resolve() in folders
    assert (tmp_path / "userData").resolve() in folders
    assert (tmp_path / "control").resolve() in folders
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", "not json")
    assert (tmp_path / "userData").resolve() in tools.private_and_runtime_folders()


class _Reservations:
    def reserve(self, workspace, deny_write):
        pass


@pytest.fixture
def prepared(machine, monkeypatch):
    """``WindowsMxcRunner._prepare`` with the helper and the host lookups replaced."""
    tmp_path, home, programs, project = machine
    helper = tmp_path / "helper.exe"
    helper.write_bytes(b"helper")
    windows = tmp_path / "Windows"
    (windows / "System32").mkdir(parents=True)
    fallback = _tool(tmp_path / "engine-python", "python.exe")
    monkeypatch.setenv("SystemRoot", str(windows))
    monkeypatch.setattr(mxc, "resolve_helper", lambda: helper)
    monkeypatch.setattr(mxc, "_python_tool_root", lambda: fallback)
    monkeypatch.setattr(mxc, "_local_path", lambda path, **_: Path(path))
    monkeypatch.setattr(mxc, "_directory_discovery_roots", lambda: [])
    monkeypatch.setattr(mxc, "_parent_identity", lambda: {"parent_pid": 99, "parent_created": 1})
    monkeypatch.setattr(
        "alysis_code.windows_toolchain.known_folder",
        lambda csidl: {0x26: programs, 0x23: tmp_path / "ProgramData"}.get(csidl),
    )
    monkeypatch.setattr(
        "alysis_code.windows_private_state.protect_private_state", lambda request: None
    )
    monkeypatch.setattr(tools, "private_and_runtime_folders", lambda: [tmp_path / "engine"])

    def build_request(settings, *, root, cwd, cmd, **_):
        return {
            "protocol": 3,
            "cwd": str(cwd),
            "argv": ["powershell.exe", "-Command", cmd],
            "read": [str(root), *settings.windows_read_roots],
            "traverse": [],
            "write": [str(root)],
            "deny_read": [],
            "deny_write": [],
            "env": [("PATH", str(windows / "System32")), ("SYSTEMROOT", str(windows))],
            "network": "off",
        }

    monkeypatch.setattr(mxc, "build_request", build_request)
    token = mxc._foreground_reservations.set(_Reservations())
    yield project, windows, fallback
    mxc._foreground_reservations.reset(token)


def _request(project, *, person):
    token = mxc._person_command.set(person)
    try:
        _, payload = mxc.WindowsMxcRunner(ShellSandboxSettings())._prepare(
            root=project, cwd=project, cmd="pytest -q"
        )
    finally:
        mxc._person_command.reset(token)
    request = json.loads(payload)
    return request, dict(request["env"])["PATH"].split(os.pathsep)


def test_a_command_gets_the_persons_programs_between_the_engines_and_the_fallback_python(
    prepared, machine, monkeypatch
):
    project, windows, fallback = prepared
    _, home, programs, _ = machine
    python = _tool(home / "Python314", "python.exe")
    (python / "Lib").mkdir()
    (python / "Lib" / "os.py").write_text("")
    cli = _tool(programs / "GitHub CLI", "gh.exe")
    engine_scripts = _tool(machine[0] / "engine" / "Scripts", "alysis.exe")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(map(str, [windows / "System32", python, cli, engine_scripts, project])),
    )
    request, path = _request(project, person=True)
    assert path == [str(windows / "System32"), str(python), str(cli), str(project), str(fallback)]
    assert str(python) in request["read"] and str(cli) in request["read"]
    # Readable is all they become: nothing is added to what the command may write.
    assert request["write"] == [str(project)]
    assert not any(
        "engine" in Path(entry).parts for entry in request["read"] if entry != str(fallback)
    )


def test_the_engines_fixed_workers_do_not_get_the_persons_programs(prepared, machine, monkeypatch):
    project, windows, fallback = prepared
    _, home, _, _ = machine
    extra = _tool(home / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join(map(str, [windows / "System32", extra])))
    request, path = _request(project, person=False)
    assert path == [str(windows / "System32"), str(fallback)]
    assert str(extra) not in request["read"]


def test_two_runs_with_the_same_path_compare_as_the_same_execution(prepared, machine, monkeypatch):
    project, windows, _ = prepared
    monkeypatch.setenv(
        "PATH", os.pathsep.join(map(str, [windows / "System32", _tool(machine[1] / "bin")]))
    )
    runner = mxc.WindowsMxcRunner(ShellSandboxSettings())
    token = mxc._person_command.set(True)
    try:
        contexts = []
        for _ in range(2):
            argv, payload = runner._prepare(root=project, cwd=project, cmd="pytest -q")
            contexts.append(runner._execution_context([str(mxc.resolve_helper())], payload))
    finally:
        mxc._person_command.reset(token)
    assert contexts[0] == contexts[1]
