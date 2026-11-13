from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from alysis_code import windows_mxc as mxc
from alysis_code import windows_sandbox as native
from alysis_code.background_runner import build_background_shell_runner_from_settings
from alysis_code.config import AppConfig, ConfigError
from alysis_code.sandbox_doctor import diagnose_sandbox
from alysis_code.sandbox_runner import (
    build_shell_runner_from_settings,
    select_shell_sandbox_backend,
)
from alysis_code.sandbox_settings import ShellSandboxSettings, resolve_shell_sandbox_settings
from alysis_code.skills.discovery import project_skill_root_relative_paths

FACTORIES = (build_shell_runner_from_settings, build_background_shell_runner_from_settings)


@pytest.mark.parametrize("mode", ["strict", "warn"])
@pytest.mark.parametrize("fallback", ["refuse", "ask"])
def test_explicit_mxc_never_falls_back(tmp_path, monkeypatch, mode, fallback):
    def missing():
        raise ConfigError("pinned helper unavailable")

    monkeypatch.setattr(mxc, "resolve_helper", missing)
    settings = ShellSandboxSettings(backend="windows-mxc", mode=mode, unavailable_fallback=fallback)
    assert select_shell_sandbox_backend(settings).backend == "windows-mxc"
    for factory in FACTORIES:
        with pytest.raises(ConfigError, match="pinned helper unavailable"):
            factory(settings, tmp_path)


def test_explicit_mxc_in_wsl_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(mxc.platform, "system", lambda: "Linux")
    for factory in FACTORIES:
        with pytest.raises(ConfigError, match="WSL"):
            factory(
                ShellSandboxSettings(backend="windows-mxc", unavailable_fallback="ask"), tmp_path
            )


def test_mxc_is_automatically_selected_on_windows(monkeypatch):
    result = select_shell_sandbox_backend(
        ShellSandboxSettings(), system="Windows", which=lambda _: None, path_exists=lambda _: False
    )
    assert result.backend == "windows-mxc"


def test_config_and_doctor_do_not_confuse_mxc_with_installed_backend(monkeypatch):
    config = AppConfig(model="test")
    config.extra_fields = {"shell_sandbox": {"backend": "windows-mxc"}}
    assert resolve_shell_sandbox_settings(config).backend == "windows-mxc"
    monkeypatch.setattr(mxc, "resolve_helper", lambda: Path("qualified-probe-only.exe"))
    monkeypatch.setattr(
        native, "helper_status", lambda: pytest.fail("old account backend consulted")
    )
    result = diagnose_sandbox(config, include_smoke=False)
    assert result.selected_backend == "windows-mxc"
    assert result.ready and not result.can_pull
    assert any(
        check.name == "native capability" and check.status == "ok" for check in result.checks
    )


def test_durable_service_cannot_escape_mxc_to_host(tmp_path):
    from alysis_code.durable_service_manager import DurableServiceManager

    manager = DurableServiceManager(
        root=tmp_path,
        state_dir=tmp_path / "state",
        settings=ShellSandboxSettings(backend="windows-mxc", unavailable_fallback="ask"),
    )
    with pytest.raises(RuntimeError, match="Host fallback is disabled"):
        manager._build_launch(cmd="never run", cwd=tmp_path, service_id="probe")


@pytest.fixture
def contract(tmp_path, monkeypatch):
    helper = tmp_path / "helper.exe"
    helper.write_bytes(b"fixture-pinned-helper")
    monkeypatch.setattr(mxc.platform, "system", lambda: "Windows")
    monkeypatch.setattr(mxc, "_local_path", lambda p, **_: p)
    monkeypatch.setenv("ALYSIS_WINDOWS_MXC_HELPER", str(helper))
    monkeypatch.setenv("ALYSIS_WINDOWS_MXC_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    metadata = dict(
        protocol=4,
        security_revision=11,
        directory_discovery=True,
        scoped_local_preview=True,
        implementation="alysis-windows-sandbox",
        containment="mxc-psec",
        upstream_commit=mxc.UPSTREAM_COMMIT,
        network_modes=["off", "on"],
        host_acl_mutation=False,
        automatic_fallback=False,
        ancestor_grants=False,
        qualification="prototype-unqualified",
        metadata_reservations="persistent-protected-directories",
    )
    monkeypatch.setattr(
        mxc, "_helper_json", lambda _, op: metadata if op == "mxc-protocol" else {"available": True}
    )
    return helper, metadata


@pytest.mark.parametrize(
    "change",
    [
        {"automatic_fallback": True},
        {"protocol": 3},
        {"security_revision": 7},
        {"security_revision": 9},
        {"security_revision": 10},
        {"directory_discovery": False},
        {"security_revision": True},
        {"host_acl_mutation": True},
        {"upstream_commit": "unreviewed"},
    ],
)
def test_contract_mismatch_refuses(contract, change):
    helper, metadata = contract
    metadata.update(change)
    with pytest.raises(ConfigError, match="policy contract"):
        mxc.resolve_helper()


def test_tampered_helper_is_rejected_before_execution(contract, monkeypatch):
    helper, _ = contract
    helper.write_bytes(b"modified")
    monkeypatch.setattr(mxc, "_helper_json", lambda *a: pytest.fail("unverified helper executed"))
    with pytest.raises(ConfigError, match="integrity"):
        mxc.resolve_helper()


def _use_home(monkeypatch, profile, roaming):
    """Have the engine take ``profile`` for the person's home."""
    from alysis_code import windows_home_access

    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("APPDATA", str(roaming))
    # The engine uses a home only when it is the profile Windows has for the
    # person; for a profile made by a test it is told so.
    monkeypatch.setattr(windows_home_access, "_own_profile", lambda: Path(profile))


def _machine_home(monkeypatch):
    """The profile of the machine itself, where its tools are, instead of the fixture's."""
    from alysis_code.windows_toolchain import known_folder

    profile, roaming = known_folder(0x28), known_folder(0x1A)
    _use_home(monkeypatch, profile, roaming)
    return profile


@pytest.fixture
def installed(tmp_path, monkeypatch):
    selected = os.environ.get("ALYSIS_TEST_WINDOWS_MXC_HELPER")
    if os.name != "nt" or not selected:
        pytest.skip("explicit development helper needed for native MXC qualification")
    source = Path(selected).resolve(strict=True)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    helper = bundle / "alysis-sandbox.exe"
    shutil.copyfile(source, helper)
    monkeypatch.setenv("ALYSIS_WINDOWS_MXC_HELPER", str(helper))
    monkeypatch.setenv("ALYSIS_WINDOWS_MXC_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    # A throwaway profile: a command is told where the person's home is and
    # may read it, and no test should read the home of the machine it runs on.
    home = tmp_path / "persons-home"
    (home / "AppData" / "Roaming").mkdir(parents=True)
    _use_home(monkeypatch, home, home / "AppData" / "Roaming")
    root = tmp_path / "workspace"
    root.mkdir()
    for name in native._PROTECTED_DIRS:
        (root / name).mkdir()
    for parts in project_skill_root_relative_paths():
        root.joinpath(*parts).mkdir(parents=True, exist_ok=True)
    for name in native._PROTECTED_FILES:
        (root / name).write_text("protected-original")
    node = Path(shutil.which("node") or "").resolve(strict=True)
    settings = ShellSandboxSettings(backend="windows-mxc", windows_read_roots=(str(node.parent),))
    return root, settings, node


def test_native_ssh_startup_and_exact_file_boundaries(installed, monkeypatch):
    from alysis_code import windows_toolchain

    root, settings, _ = installed
    fake_home = root.parent / "private-home"
    credentials = fake_home / ".ssh"
    credentials.mkdir(parents=True)
    key, sibling, control = (credentials / name for name in ("key", "sibling", "app-state"))
    for path in (key, sibling, control):
        path.write_text("synthetic")
    program_data = root.parent / "program-data"
    (program_data / "ssh").mkdir(parents=True)
    canary = program_data / "ssh" / "canary"
    canary.write_text("synthetic")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    original_folder = windows_toolchain.known_folder
    monkeypatch.setattr(
        windows_toolchain,
        "known_folder",
        lambda n: program_data if n == 0x23 else original_folder(n),
    )
    monkeypatch.setenv("ProgramData", str(root / "untrusted-environment-value"))
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(control)]))
    # Private app state is refused at admission even alongside an approved key.
    runner = mxc.WindowsMxcRunner(replace(settings, additional_read_files=(str(key), str(control))))
    from alysis_code.host_file_policy import HostFilePolicyError

    with pytest.raises(HostFilePolicyError):
        runner._prepare(root=root, cwd=root, cmd="")
    runner = mxc.WindowsMxcRunner(replace(settings, additional_read_files=(str(key),)))
    _, payload = runner._prepare(root=root, cwd=root, cmd="")
    request = json.loads(payload)
    assert dict(request["env"])["PROGRAMDATA"] == str(program_data)
    assert str(program_data) not in request["read"]
    assert str(credentials) in request["discovery_deny"]
    assert str(control) in request["deny_read"]

    def quote(p):
        return "'" + str(p).replace("'", "''") + "'"

    command = "ssh -V; if ($LASTEXITCODE -ne 0) { exit 92 }; "
    command += f"if ([IO.File]::ReadAllText({quote(key)}) -ne 'synthetic') {{ exit 93 }}; "
    for path in (sibling, control, canary):
        command += (
            f"try {{ [IO.File]::ReadAllText({quote(path)}) | Out-Null; exit 94 }} catch {{}}; "
        )
    command += f"try {{ [IO.File]::WriteAllText({quote(key)},'bad'); exit 95 }} catch {{}}; Write-Output 'SSH_BOUNDARIES_OK'"
    result = runner.run(root=root, cwd=root, cmd=command, timeout_s=25)
    assert result.returncode == 0 and "SSH_BOUNDARIES_OK" in result.stdout, result
    assert "OpenSSH_for_Windows" in result.stderr
    assert key.read_text() == "synthetic"


def test_native_engine_foreground_uses_real_policy_and_node_children(installed, monkeypatch):
    root, settings, node = installed
    monkeypatch.setenv("ALYSIS_MXC_TEST_SECRET", "must-not-be-inherited")
    script = root / "probe.cjs"
    script.write_text(
        "const fs=require('node:fs'),cp=require('node:child_process');"
        "if(process.env.ALYSIS_MXC_TEST_SECRET)throw Error('environment leaked');"
        "fs.writeFileSync('output.txt','MXC_ENGINE_OK');"
        "for(const p of ['AGENTS.md','.git/unsafe','.github/skills/unsafe']){"
        "try{fs.writeFileSync(p,'bad');throw Error('write escaped')}"
        "catch(e){if(!['EACCES','EPERM'].includes(e.code))throw e}}"
        "const c=cp.spawnSync(process.execPath,['-e','console.log(42)'],{encoding:'utf8'});"
        "if(c.status!==0||c.stdout.trim()!=='42')throw Error('child failed');console.log('MXC_ENGINE_OK')"
    )
    runner = build_shell_runner_from_settings(settings, root)
    completed = runner.run(
        root=root, cwd=root, cmd=f"& '{node}' './probe.cjs'; exit $LASTEXITCODE", timeout_s=20
    )
    assert completed.returncode == 0, completed.stderr
    assert "MXC_ENGINE_OK" in completed.stdout
    assert (root / "output.txt").read_text() == "MXC_ENGINE_OK"
    assert (root / "AGENTS.md").read_text() == "protected-original"


@pytest.mark.parametrize("filesystem_access", ["workspace-write", "read-only"])
def test_native_directory_discovery_never_grants_file_contents(
    installed, monkeypatch, filesystem_access
):
    root, settings, node = installed
    outside = root.parent / "outside names é"
    nested = outside / "nested"
    private = root.parent / "private-discovery"
    nested.mkdir(parents=True)
    private.mkdir()
    canary = nested / "unapproved.txt"
    canary.write_text("outside-original", encoding="utf-8")
    (private / "hidden.txt").write_text("private-original", encoding="utf-8")
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(private)]))
    script = root / "discover.cjs"
    script.write_text(
        "const fs=require('node:fs'),a=require('node:assert/strict');\n"
        f"const outside={json.dumps(str(outside))}, nested={json.dumps(str(nested))},"
        f"canary={json.dumps(str(canary))}, privateRoot={json.dumps(str(private))};\n"
        "a.ok(fs.readdirSync(outside).includes('nested'));\n"
        "a.ok(fs.readdirSync(nested).includes('unapproved.txt'));\n"
        "a.ok(fs.statSync(canary).isFile());\n"
        "for(const action of [()=>fs.readFileSync(canary),"
        "()=>fs.writeFileSync(canary,'bad'),()=>fs.unlinkSync(canary),"
        "()=>fs.writeFileSync(outside+'/new.txt','bad'),"
        "()=>fs.readdirSync(privateRoot),()=>fs.readFileSync(privateRoot+'/hidden.txt')])"
        "a.throws(action,e=>['EACCES','EPERM'].includes(e.code));\n"
        "console.log('DIRECTORY_DISCOVERY_ONLY_OK');\n",
        encoding="utf-8",
    )
    runner = build_shell_runner_from_settings(
        replace(settings, filesystem_access=filesystem_access), root
    )
    completed = runner.run(
        root=root, cwd=root, cmd=f"& '{node}' './discover.cjs'; exit $LASTEXITCODE", timeout_s=25
    )
    assert completed.returncode == 0, completed.stderr
    assert "DIRECTORY_DISCOVERY_ONLY_OK" in completed.stdout
    assert canary.read_text(encoding="utf-8") == "outside-original"
    assert not (outside / "new.txt").exists()


@pytest.mark.parametrize("managed_workspace", [False, True])
def test_native_discovery_with_helper_inside_desktop_profile(
    installed, monkeypatch, managed_workspace
):
    root, settings, node = installed
    profile = root.parent / "desktop-profile"
    bundle = profile / "cli-runtime" / "helper"
    bundle.mkdir(parents=True)
    helper = bundle / "alysis-sandbox.exe"
    shutil.copyfile(mxc.resolve_helper(), helper)
    monkeypatch.setenv("ALYSIS_WINDOWS_MXC_HELPER", str(helper))
    monkeypatch.setenv("ALYSIS_DESKTOP_USER_DATA", str(profile))
    monkeypatch.setenv(
        "ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(profile / "future-settings.json")])
    )
    # Installed Desktop also grants bundled Git below its profile. Keep this
    # real read dependency present; removing the launcher grant alone is not enough.
    tools = profile / "cli-runtime" / "tools"
    tools.mkdir()
    (tools / "tool.txt").write_text("read-only-tool")
    settings = replace(settings, windows_read_roots=(*settings.windows_read_roots, str(tools)))
    if managed_workspace:
        root = profile / "work-tasks" / "task"
        root.mkdir(parents=True)
    outside = profile.parent / "outside"
    outside.mkdir()
    (outside / "canary.txt").write_text("not-readable")
    script = root / "desktop-discovery.cjs"
    script.write_text(
        "const fs=require('node:fs'),a=require('node:assert/strict');"
        f"const outside={json.dumps(str(outside))},profile={json.dumps(str(profile))};"
        "a.ok(fs.readdirSync(outside).includes('canary.txt'));"
        "a.equal(fs.readFileSync(profile+'/cli-runtime/tools/tool.txt','utf8'),'read-only-tool');"
        "fs.writeFileSync('allowed.txt','ordinary-work');"
        "a.throws(()=>fs.readFileSync(outside+'/canary.txt'),"
        "e=>['EPERM','EACCES'].includes(e.code));"
        "a.throws(()=>fs.readdirSync(profile),e=>['EPERM','EACCES'].includes(e.code));"
        "a.throws(()=>fs.writeFileSync(profile+'/cli-runtime/tools/tool.txt','bad'),"
        "e=>['EPERM','EACCES'].includes(e.code));"
        "fs.writeFileSync('discovery-ready','1');"
        "const timer=setInterval(()=>{if(!fs.existsSync('discovery-release'))return;"
        "clearInterval(timer);"
        "for(const op of [()=>fs.readFileSync(profile+'/future-settings.json'),"
        "()=>fs.statSync(profile+'/future-settings.json')])"
        "a.throws(op,e=>['EPERM','EACCES'].includes(e.code));"
        "console.log('DESKTOP_DISCOVERY_OK');},20);"
    )
    runner = build_shell_runner_from_settings(settings, root)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            runner.run,
            root=root,
            cwd=root,
            cmd=f"& '{node}' './desktop-discovery.cjs'; exit $LASTEXITCODE",
            timeout_s=25,
        )
        try:
            deadline = time.monotonic() + 15
            while not (root / "discovery-ready").exists():
                assert not pending.done(), pending.result() if pending.done() else None
                assert time.monotonic() < deadline, "discovery probe never became ready"
                time.sleep(0.02)
            (profile / "future-settings.json").write_text("created by host after launch")
        finally:
            (root / "discovery-release").write_text("1")
        completed = pending.result(timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert "DESKTOP_DISCOVERY_OK" in completed.stdout
    assert (root / "allowed.txt").read_text() == "ordinary-work"


def test_native_python_default_temporary_directories_and_child_cleanup(installed):
    root, settings, _ = installed
    # No dir= workaround: ordinary Python libraries must work unchanged. An
    # apostrophe in the workspace also exercises shell-literal quoting.
    workspace = root / "temp's workspace"
    workspace.mkdir()
    (workspace / "temp_probe.py").write_text(
        "from pathlib import Path\nimport tempfile, subprocess, sys\n"
        "root=Path.cwd().resolve()\n"
        "temp=Path(tempfile.gettempdir()).resolve()\n"
        # The temp folder is the command's own, not the person's project.
        "assert temp!=root and root not in temp.parents and temp not in root.parents\n"
        # The home is the person's. The temp folder is not in it.
        "assert Path.home().is_dir() and Path.home() not in temp.parents\n"
        "with tempfile.TemporaryDirectory() as folder:\n"
        " p=Path(folder)/'sample.txt';p.write_text('PRIVATE_TEMP');assert p.read_text()=='PRIVATE_TEMP'\n"
        "assert not Path(folder).exists()\n"
        "if len(sys.argv)==1:\n"
        " subprocess.run([sys.executable,__file__,'child'],check=True)\n"
        "print('TEMP_CHILD_OK' if len(sys.argv)>1 else 'TEMP_PARENT_OK')\n"
    )
    completed = build_shell_runner_from_settings(settings, workspace).run(
        root=workspace,
        cwd=workspace,
        cmd="python ./temp_probe.py; exit $LASTEXITCODE",
        timeout_s=25,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "TEMP_PARENT_OK" in completed.stdout and "TEMP_CHILD_OK" in completed.stdout
    assert {path.name for path in workspace.iterdir()} == {"temp_probe.py"}


def _scratch_report(completed):
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    return json.loads(completed.stdout[completed.stdout.index("{") :])


_SCRATCH_PROBE = (
    "$r=[ordered]@{home=$HOME;profile=$env:USERPROFILE;roaming=$env:APPDATA;"
    "local=$env:LOCALAPPDATA;temp=$env:TEMP;net_temp=[IO.Path]::GetTempPath();"
    "drive=$env:SystemDrive;programs=$env:ProgramFiles;user=$env:USERNAME;written=@()};"
    "foreach($d in $r.home,$r.roaming,$r.local,$r.temp){"
    "Set-Content -LiteralPath (Join-Path $d 'probe.txt') -Value x -ErrorAction Stop;"
    "$r.written+=(Test-Path -LiteralPath (Join-Path $d 'probe.txt'))};"
    "$r|ConvertTo-Json -Compress"
)


def test_native_command_has_a_local_data_and_a_temp_folder_of_its_own(installed, monkeypatch):
    from alysis_code.windows_mxc_scratch import scratch_base

    root, settings, _ = installed
    persons = _persons_home(root.parent, monkeypatch)
    before = {path.name for path in root.iterdir()}
    completed = build_shell_runner_from_settings(settings, root).run(
        root=root,
        cwd=root,
        cmd=(
            "$r=[ordered]@{home=$HOME;profile=$env:USERPROFILE;roaming=$env:APPDATA;"
            "local=$env:LOCALAPPDATA;temp=$env:TEMP;net_temp=[IO.Path]::GetTempPath();written=@()};"
            "foreach($d in $r.local,$r.temp){"
            "Set-Content -LiteralPath (Join-Path $d 'probe.txt') -Value x -ErrorAction Stop;"
            "$r.written+=(Test-Path -LiteralPath (Join-Path $d 'probe.txt'))};"
            "$r|ConvertTo-Json -Compress"
        ),
        timeout_s=25,
    )
    report = _scratch_report(completed)
    # The home is the person's; what programs write to is the command's own.
    assert report["home"] == report["profile"] == str(persons)
    assert Path(report["roaming"]) == persons / "AppData" / "Roaming"
    local = Path(report["local"])
    scratch = local.parent.parent.parent
    assert (
        scratch.parent == scratch_base().resolve()
        and local == scratch / "home" / "AppData" / "Local"
    )
    assert Path(report["temp"]) == local / "Temp" == Path(report["net_temp"])
    assert report["written"] == [True, True]
    assert not scratch.is_relative_to(root) and not scratch.is_relative_to(persons)
    assert not scratch.exists()
    assert {path.name for path in root.iterdir()} == before


def test_native_command_gets_a_scratch_home_when_the_persons_cannot_be_used(installed, monkeypatch):
    """The arrangement before the person's home was readable, and still the fallback."""
    from alysis_code.windows_mxc_scratch import scratch_base

    root, settings, _ = installed
    monkeypatch.setenv("USERPROFILE", str(root.parent / "no-such-home"))
    before = {path.name for path in root.iterdir()}
    completed = build_shell_runner_from_settings(settings, root).run(
        root=root, cwd=root, cmd=_SCRATCH_PROBE, timeout_s=25
    )
    report = _scratch_report(completed)
    home = Path(report["home"])
    assert report["profile"] == str(home) and home.parent.parent == scratch_base().resolve()
    assert Path(report["roaming"]) == home / "AppData" / "Roaming"
    assert Path(report["local"]) == home / "AppData" / "Local"
    assert Path(report["temp"]) == home / "AppData" / "Local" / "Temp"
    assert Path(report["net_temp"]) == Path(report["temp"])
    assert report["written"] == [True, True, True, True]
    assert not home.is_relative_to(root) and not root.is_relative_to(home.parent)
    assert not home.is_relative_to(Path.home() / "Documents")
    assert report["drive"] == os.environ["SystemDrive"]
    assert report["programs"] == os.environ["ProgramFiles"]
    assert report["user"] == os.environ["USERNAME"]
    # The folder goes with the command, and the project holds nothing new.
    assert not home.parent.exists()
    assert {path.name for path in root.iterdir()} == before


@pytest.mark.parametrize("home", ["the person's", "scratch"])
def test_native_folder_api_answers_with_a_folder_and_nothing_lands_in_the_project(
    installed, monkeypatch, home
):
    """PowerShell wrote its module cache into the project as ``Microsoft\\Windows``.

    The Windows folder API does not read LOCALAPPDATA. It answers from
    USERPROFILE, and its answer did not exist below the scratch home, so the
    answer was empty and the cache path became relative to the working
    directory. Below the person's own profile Windows has made the folder.
    The cache is written some seconds after modules were analysed, hence the
    wait.
    """
    root, settings, _ = installed
    if home == "scratch":
        monkeypatch.setenv("USERPROFILE", str(root.parent / "no-such-home"))
    else:
        _machine_home(monkeypatch)
    before = {path.name for path in root.iterdir()}
    completed = build_shell_runner_from_settings(settings, root).run(
        root=root,
        cwd=root,
        cmd=(
            "$r=[ordered]@{home=$HOME;"
            "local=[Environment]::GetFolderPath('LocalApplicationData');"
            "cache=[Environment]::GetFolderPath('InternetCache');"
            "child=(powershell -NoProfile -Command "
            "\"[Environment]::GetFolderPath('LocalApplicationData')\")};"
            "Get-Module -ListAvailable | Out-Null; Start-Sleep -Seconds 14;"
            "$r|ConvertTo-Json -Compress"
        ),
        timeout_s=60,
    )
    report = _scratch_report(completed)
    home = Path(report["home"])
    for name in ("local", "cache", "child"):
        assert report[name] and Path(report[name]).is_relative_to(home), (name, report)
    assert {path.name for path in root.iterdir()} == before


def test_native_scratch_folder_is_the_only_new_place_a_command_can_write(installed):
    from alysis_code.windows_mxc_scratch import scratch_base

    root, settings, _ = installed
    base = scratch_base()
    base.mkdir(parents=True, exist_ok=True)
    neighbour = base / ("neighbour-" + uuid.uuid4().hex[:8])
    neighbour.mkdir()
    (neighbour / "theirs.txt").write_text("another command's file")
    try:
        completed = build_shell_runner_from_settings(settings, root).run(
            root=root,
            cwd=root,
            cmd=(
                "$base=$env:LOCALAPPDATA;foreach($i in 1..4){$base=Split-Path $base};$r=[ordered]@{};"
                f"foreach($t in (Join-Path $base 'planted.txt'),'{neighbour}\\planted.txt',"
                "(Join-Path (Split-Path $base) 'planted.txt')){"
                "try{Set-Content -LiteralPath $t -Value x -ErrorAction Stop;$r[$t]='written'}"
                "catch{$r[$t]='refused'}};"
                f"try{{$r.read=(Get-Content -LiteralPath '{neighbour}\\theirs.txt' -ErrorAction Stop)}}"
                "catch{$r.read='refused'};$r|ConvertTo-Json -Compress"
            ),
            timeout_s=25,
        )
        report = _scratch_report(completed)
        assert report.pop("read") == "refused"
        assert len(report) == 3 and set(report.values()) == {"refused"}, report
        assert not (base / "planted.txt").exists() and not (neighbour / "planted.txt").exists()
        assert not (base.parent / "planted.txt").exists()
    finally:
        shutil.rmtree(neighbour)


def test_native_background_command_keeps_its_scratch_folder_until_cleanup(installed):
    root, settings, _ = installed
    started = build_background_shell_runner_from_settings(settings, root).start(
        root=root, cwd=root, cmd="[Console]::Out.WriteLine($env:TEMP)"
    )
    try:
        started.popen.wait(timeout=15)
        temp = Path(started.popen.stdout.read().decode().strip())
        assert started.popen.returncode == 0, started.popen.stderr.read().decode()
        assert temp.is_dir() and not temp.is_relative_to(root)
    finally:
        started.cleanup()
    assert not temp.exists()


def test_native_mistake_in_a_command_is_reported_without_the_engine_preparation(installed):
    root, settings, _ = installed
    mistake = "Write-Output one && Write-Output two"
    completed = build_shell_runner_from_settings(settings, root).run(
        root=root, cwd=root, cmd=mistake, timeout_s=25
    )
    assert completed.returncode != 0 and "one" not in completed.stdout
    assert "+ " + mistake in completed.stderr
    for preparation in ("New-PSDrive", "Set-Location", "$env:TEMP", "Alysis:"):
        assert preparation not in completed.stderr


def test_native_commands_location_is_a_real_path_every_program_takes(installed):
    """ "$PWD" was "Alysis:\\", which .NET refuses, and the folder above was the folder itself."""
    root, settings, _ = installed
    (root / "sub folder").mkdir()
    completed = build_shell_runner_from_settings(settings, root).run(
        root=root,
        cwd=root / "sub folder",
        cmd=(
            "$PWD.Path; [Environment]::CurrentDirectory; "
            "[IO.File]::WriteAllText((Join-Path $PWD 'made.txt'), 'x'); "
            "(Resolve-Path made.txt).Path; "
            "(Split-Path $PWD -Parent | Get-ChildItem -Name) -contains 'sub folder'; "
            "cmd /c cd"
        ),
        timeout_s=25,
    )
    here = str(root / "sub folder")
    assert completed.stdout.splitlines() == [
        here,
        here,
        here + "\\made.txt",
        "True",
        here,
    ], (completed.stdout, completed.stderr)
    assert (root / "sub folder" / "made.txt").read_text() == "x"


def _link(target, link):
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def test_native_project_with_hard_links_and_links_runs_and_a_link_out_of_it_opens_nothing(
    installed,
):
    """A uv environment or pnpm's node_modules made the sandbox refuse the whole project."""
    root, settings, _ = installed
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "theirs.txt").write_text("OUTSIDE_CONTENT")
    (outside / "cache.bin").write_text("CACHED_PACKAGE")
    (root / "package.bin").hardlink_to(outside / "cache.bin")
    (root / "real").mkdir()
    (root / "real" / "inner.txt").write_text("INNER_CONTENT")
    _link(root / "real", root / "inside-link")
    _link(outside, root / "outside-link")
    _link(root / ".git", root / "git-link")
    before = _git_metadata(root) if (root / ".git").is_dir() else {}
    completed = build_shell_runner_from_settings(settings, root).run(
        root=root,
        cwd=root,
        cmd=(
            "Get-Content package.bin; Get-Content inside-link\\inner.txt; "
            "try { Get-Content outside-link\\theirs.txt -ErrorAction Stop } catch { 'OUT_READ_REFUSED' }; "
            "try { Set-Content outside-link\\planted.txt -Value x -ErrorAction Stop; 'OUT_WRITTEN' } "
            "catch { 'OUT_WRITE_REFUSED' }; "
            "try { Set-Content git-link\\planted -Value x -ErrorAction Stop; 'GIT_WRITTEN' } "
            "catch { 'GIT_WRITE_REFUSED' }"
        ),
        timeout_s=25,
    )
    said = completed.stdout
    assert "CACHED_PACKAGE" in said and "INNER_CONTENT" in said, (said, completed.stderr)
    assert (
        "OUT_READ_REFUSED" in said and "OUT_WRITE_REFUSED" in said and "GIT_WRITE_REFUSED" in said
    )
    assert "OUTSIDE_CONTENT" not in said
    assert not (outside / "planted.txt").exists() and not (root / ".git" / "planted").exists()
    assert (_git_metadata(root) if (root / ".git").is_dir() else {}) == before


def test_native_folder_let_written_for_one_command_holds_nothing_but_the_persons_files(
    installed,
):
    """The reserved entries made a copy stop, a zip fail and robocopy leave eleven of them behind."""
    import zipfile

    root, settings, _ = installed
    reports, backup, attached = (root.parent / name for name in ("Reports", "Backup", "attached"))
    (reports / "2026").mkdir(parents=True)
    (reports / "q1.txt").write_text("Q1")
    (reports / "2026" / "q2.txt").write_text("Q2")
    backup.mkdir()
    attached.mkdir()
    granted = replace(
        settings,
        additional_workspace_roots=(str(reports), str(backup), str(attached)),
        windows_plain_write_roots=(str(reports), str(backup)),
    )
    completed = build_shell_runner_from_settings(granted, root).run(
        root=root,
        cwd=root,
        cmd=(
            f"'IN_REPORTS=' + ((Get-ChildItem -Force -Name -LiteralPath '{reports}') -join ','); "
            f"'IN_ATTACHED=' + ((Get-ChildItem -Force -Name -LiteralPath '{attached}') -join ','); "
            f"Copy-Item -Path '{reports}\\*' -Destination '{backup}' -Recurse -ErrorAction Stop; "
            f"Compress-Archive -Path '{reports}\\*' -DestinationPath '{backup}\\r.zip' -ErrorAction Stop; "
            f"robocopy '{reports}' '{backup}\\mirror' /E /NFL /NDL /NJH /NJS /NP | Out-Null; "
            "'DONE'"
        ),
        timeout_s=40,
    )
    said = completed.stdout
    assert "IN_REPORTS=2026,q1.txt" in said and "DONE" in said, (said, completed.stderr)
    # A folder that is attached to the session is a project: names are reserved there.
    assert "AGENTS.md" in said.split("IN_ATTACHED=")[1].splitlines()[0]
    assert completed.stderr.strip() == ""
    kept = sorted(path.relative_to(backup).as_posix() for path in backup.rglob("*"))
    assert kept == ["2026", "2026/q2.txt", "mirror", "mirror/2026", "mirror/2026/q2.txt"] + [
        "mirror/q1.txt",
        "q1.txt",
        "r.zip",
    ]
    assert sorted(zipfile.ZipFile(backup / "r.zip").namelist()) == ["2026/q2.txt", "q1.txt"]
    assert sorted(path.name for path in attached.iterdir()) == []


def test_native_folder_with_the_links_windows_puts_in_documents_can_be_granted(installed):
    """Every Documents folder holds "My Music", "My Pictures" and "My Videos" as links."""
    root, settings, _ = installed
    documents = root.parent / "Documents"
    (documents / "Reports").mkdir(parents=True)
    (documents / "Reports" / "q1.txt").write_text("REPORT_Q1")
    music = root.parent / "Music"
    music.mkdir()
    (music / "song.txt").write_text("SONG")
    _link(music, documents / "My Music")
    read = build_shell_runner_from_settings(
        replace(settings, windows_read_roots=(*settings.windows_read_roots, str(documents))), root
    ).run(
        root=root,
        cwd=root,
        cmd=(
            f"Get-Content -LiteralPath '{documents}\\Reports\\q1.txt'; "
            f"try {{ Get-Content -LiteralPath '{documents}\\My Music\\song.txt' -ErrorAction Stop }} "
            "catch { 'BEHIND_THE_LINK_REFUSED' }; "
            f"try {{ Set-Content -LiteralPath '{documents}\\new.txt' -Value x -ErrorAction Stop; 'WRITTEN' }} "
            "catch { 'READ_ONLY' }"
        ),
        timeout_s=25,
    )
    assert "REPORT_Q1" in read.stdout and "BEHIND_THE_LINK_REFUSED" in read.stdout, (
        read.stdout,
        read.stderr,
    )
    assert "READ_ONLY" in read.stdout and "SONG" not in read.stdout


_RESERVED_NAMES = {
    ".agents",
    ".alysis",
    ".alysis_skills",
    ".claude",
    ".codex",
    ".git",
    ".github",
    ".sylliptor",
    "AGENTS.md",
    "CLAUDE.md",
    "CONVENTIONS.md",
}
_TRY_TO_PLANT = (
    "try{Set-Content -LiteralPath .git\\planted -Value x -ErrorAction Stop;'PLANTED'}"
    "catch{'REFUSED'}"
)


def _project_without_reserved_names(installed, name):
    fixture_root, settings, _ = installed
    root = fixture_root.parent / name
    root.mkdir()
    (root / "README.md").write_text("the person's file\n")
    return root, settings


def _background(settings, root):
    started = build_background_shell_runner_from_settings(settings, root).start(
        root=root, cwd=root, cmd="[Console]::Out.WriteLine('STARTED'); Start-Sleep -Seconds 120"
    )
    assert started.popen.stdout.readline().strip() == b"STARTED"
    return started


def _entries(root):
    return {path.name for path in root.iterdir()}


def test_native_background_command_takes_its_reserved_names_back_when_it_stops(installed):
    """They used to stay in the person's project for good."""
    from alysis_code.windows_mxc_reservations import records_folder

    root, settings = _project_without_reserved_names(installed, "background-project")
    started = _background(settings, root)
    try:
        assert _entries(root) == _RESERVED_NAMES | {"README.md"}
        assert any(records_folder(root.resolve()).iterdir())
    finally:
        started.cleanup()
    assert _entries(root) == {"README.md"}
    assert not records_folder(root.resolve()).exists()


def test_native_reserved_names_stay_until_the_last_background_command_stops(installed):
    root, settings = _project_without_reserved_names(installed, "two-commands")
    first = _background(settings, root)
    second = _background(settings, root)
    try:
        first.cleanup()
        # The second command still relies on them, and so does a foreground
        # command that runs meanwhile; it must not take them along when it ends.
        assert _entries(root) == _RESERVED_NAMES | {"README.md"}
        completed = build_shell_runner_from_settings(settings, root).run(
            root=root, cwd=root, cmd=_TRY_TO_PLANT, timeout_s=25
        )
        assert completed.stdout.strip() == "REFUSED", (completed.stdout, completed.stderr)
        assert _entries(root) == _RESERVED_NAMES | {"README.md"}
    finally:
        first.cleanup()
        second.cleanup()
    assert _entries(root) == {"README.md"}


def test_native_names_the_person_already_had_survive_a_background_command(installed):
    root, settings = _project_without_reserved_names(installed, "own-names")
    (root / "AGENTS.md").write_text("")
    (root / ".claude").mkdir()
    started = _background(settings, root)
    started.cleanup()
    assert _entries(root) == {"README.md", "AGENTS.md", ".claude"}


def test_native_next_command_takes_back_what_a_vanished_engine_process_left(installed):
    """The engine process dies without stopping its background command."""
    import subprocess
    import sys

    root, settings = _project_without_reserved_names(installed, "vanished-engine")
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from alysis_code.background_runner import build_background_shell_runner_from_settings\n"
        "from alysis_code.sandbox_settings import ShellSandboxSettings\n"
        "root = Path(sys.argv[1])\n"
        "settings = ShellSandboxSettings(backend='windows-mxc', windows_read_roots=(sys.argv[2],))\n"
        "started = build_background_shell_runner_from_settings(settings, root).start(\n"
        "    root=root, cwd=root, cmd=\"[Console]::Out.WriteLine('STARTED'); Start-Sleep -Seconds 120\")\n"
        "assert started.popen.stdout.readline().strip() == b'STARTED'\n"
        "print(sorted(p.name for p in root.iterdir()), flush=True)\n"
        "os._exit(0)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script, str(root), settings.windows_read_roots[0]],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "AGENTS.md" in done.stdout and _entries(root) == _RESERVED_NAMES | {"README.md"}
    # The helper notices that its engine is gone and ends the command. Until
    # then it still has the entries open, so more than one command may pass.
    runner = build_shell_runner_from_settings(settings, root)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        completed = runner.run(root=root, cwd=root, cmd=_TRY_TO_PLANT, timeout_s=25)
        assert completed.stdout.strip() == "REFUSED", (completed.stdout, completed.stderr)
        if _entries(root) == {"README.md"}:
            break
        time.sleep(1)
    assert _entries(root) == {"README.md"}


def test_native_background_command_starts_in_a_project_another_command_keeps_busy(
    installed, monkeypatch
):
    """Without the project's lease there is no record; the names stay, as they used to."""
    import threading

    from alysis_code._windows_git_reservations import GitLease

    root, settings = _project_without_reserved_names(installed, "busy-project")
    monkeypatch.setattr(mxc, "_BACKGROUND_LEASE_WAIT_S", 0.5)
    taken, done = threading.Event(), threading.Event()

    def another_command():
        lease = GitLease(root.resolve(), 10, None)
        taken.set()
        done.wait(60)
        lease.close()

    thread = threading.Thread(target=another_command)
    thread.start()
    try:
        assert taken.wait(10)
        started = _background(settings, root)
    finally:
        done.set()
        thread.join()
    started.cleanup()
    assert _entries(root) == _RESERVED_NAMES | {"README.md"}


def _repository_project(installed, name):
    """A project with a repository and one commit, and a way to run the bundled Git on it."""
    import subprocess

    from alysis_code import windows_toolchain

    tools = windows_toolchain.installed_tools()
    if "git" not in tools:
        pytest.skip("the bundled Git is needed to make the repository")
    git = str(tools["git"][0])
    root, settings = _project_without_reserved_names(installed, name)
    root = root.resolve()
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}

    def host_git(*arguments):
        done = subprocess.run(
            [git, *arguments], cwd=root, capture_output=True, text=True, env=environment
        )
        return done.returncode, (done.stdout + done.stderr).strip()

    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "probe@example.invalid"),
        ("config", "user.name", "Probe"),
        ("add", "README.md"),
        ("commit", "-q", "-m", "initial"),
    ):
        assert host_git(*arguments)[0] == 0
    return root, settings, host_git


def _git_metadata(root):
    """Every file of the repository with its content, apart from what Git itself rewrites."""
    state = {}
    for path in sorted((root / ".git").rglob("*")):
        if path.is_file():
            state[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


_TRY_THE_REPOSITORY = (
    "$r=@();"
    "foreach($t in '.git\\hooks\\pre-commit','.git\\config','.git\\planted'){"
    "try{Set-Content -LiteralPath $t -Value 'planted' -ErrorAction Stop;$r+='WROTE '+$t}"
    "catch{$r+='REFUSED'}};"
    "try{Rename-Item -LiteralPath .git -NewName gone -ErrorAction Stop;$r+='RENAMED'}"
    "catch{$r+='REFUSED'};"
    "try{Remove-Item -LiteralPath .git\\HEAD -Force -ErrorAction Stop;$r+='DELETED'}"
    "catch{$r+='REFUSED'};"
    "[Console]::Out.WriteLine($r -join ',')"
)


def test_native_persons_git_works_while_commands_run_in_the_repository(installed):
    """It failed with "unable to write new index file" for as long as any command ran."""
    import threading

    root, settings, host_git = _repository_project(installed, "git-meanwhile")

    def stage_commit_branch(name):
        (root / f"{name}.txt").write_text(name + "\n")
        for arguments in (
            ("add", f"{name}.txt"),
            ("commit", "-q", "-m", name),
            ("checkout", "-q", "-b", "topic-" + name),
        ):
            code, said = host_git(*arguments)
            assert code == 0, (name, arguments, said)

    started = _background(settings, root)
    try:
        stage_commit_branch("background")
    finally:
        started.cleanup()
    results = []
    foreground = threading.Thread(
        target=lambda: results.append(
            build_shell_runner_from_settings(settings, root).run(
                root=root, cwd=root, cmd="Start-Sleep -Seconds 8; 'FOREGROUND_DONE'", timeout_s=60
            )
        )
    )
    foreground.start()
    try:
        time.sleep(3)
        assert foreground.is_alive()
        stage_commit_branch("foreground")
    finally:
        foreground.join()
    assert "FOREGROUND_DONE" in results[0].stdout, (results[0].stdout, results[0].stderr)
    assert host_git("log", "--oneline")[1].count("\n") == 2


def test_native_engine_git_job_commits_while_a_background_command_runs(installed):
    from alysis_code import git_native

    root, settings, host_git = _repository_project(installed, "git-job-meanwhile")
    started = _background(settings, root)
    try:
        (root / "work.txt").write_text("work\n")
        for argv in (["add", "work.txt"], ["commit", "-m", "work"]):
            result = git_native.run(
                settings=replace(settings, safe_git_writes=True),
                root=root,
                cwd=root,
                argv=argv,
                timeout_s=60,
                cancel_check=None,
            )
            assert result.returncode == 0, (argv, result.stdout, result.stderr)
    finally:
        started.cleanup()
    assert "work" in host_git("log", "-1", "--format=%s")[1]


def _git_job(settings, root, argv, *, cwd=None, approved=False, decided=False):
    """One literal Git command in the Git job.

    ``approved`` is a person's ``permissions.git_metadata``; ``decided`` is a
    person's yes to the command for any other reason.
    """
    from alysis_code import git_native

    return git_native.run(
        settings=replace(settings, safe_git_writes=True, git_metadata_access=approved),
        root=root,
        cwd=cwd or root,
        argv=argv,
        timeout_s=120,
        cancel_check=None,
        **({"whole_metadata": True} if decided else {}),
    )


def _requests(monkeypatch):
    """The sandbox requests the Git job builds from here on, each as it was sent."""
    from alysis_code import windows_private_state

    seen = []
    original = windows_private_state.protect_private_state

    def recorded(request):
        # The job changes this same object until it sends it.
        if request not in seen:
            seen.append(request)
        return original(request)

    monkeypatch.setattr(windows_private_state, "protect_private_state", recorded)
    return seen


# What a Git directory does not have until something decides what a later Git runs.
_NAMES_GIT_WOULD_RUN_FROM = (
    "commondir",
    "config.worktree",
    "rebase-merge",
    "remotes",
    "branches",
    "modules",
    "worktrees",
)

# A hook is the repository's own program: Git starts it during a commit, in
# the same sandbox. Each line tries one way of deciding what a later Git runs.
_HOOK_THAT_TRIES_GIT_ITSELF = """#!/bin/sh
out=""
attempt() {
  if sh -c "$1" >/dev/null 2>&1; then out="$out WROTE"; else out="$out REFUSED"; fi
}
attempt 'echo allowed > hook-allowed.txt && mkdir .git/prepared && echo x > .git/prepared-file'
attempt 'echo "[alias]" >> .git/config'
attempt 'echo "[alias]" >> .git/CONFIG'
attempt 'ln .git/config .git/config-alias'
attempt 'echo planted > .git/hooks/post-commit'
attempt 'echo planted > .git/hooks/pre-commit'
attempt 'echo planted > .git/HOOKS/post-merge'
attempt 'mv .git/config .git/config.old'
attempt 'mv .git/prepared-file .git/config'
attempt 'mv .git/hooks .git/hooks.old'
attempt 'mv .git .git.old'
attempt 'git config alias.planted "!planted"'
attempt 'echo ../elsewhere > .git/commondir'
attempt 'mv .git/prepared-file .git/commondir'
attempt 'echo planted > .git/config.worktree'
attempt 'mkdir .git/rebase-merge'
attempt 'mv .git/prepared .git/rebase-merge'
attempt 'echo planted > .git/remotes/origin'
attempt 'echo planted > .git/branches/origin'
attempt 'mkdir .git/modules/planted'
attempt 'mkdir .git/worktrees/planted'
echo "$out" > hook-report.txt
exit 0
"""


def _program_state(root):
    """Git's configuration and hooks as they are, and which of the other names exist."""
    folder = root / ".git"
    hooks = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((folder / "hooks").iterdir())
    }
    present = sorted(set(os.listdir(folder)) & set(_NAMES_GIT_WOULD_RUN_FROM))
    return (folder / "config").read_bytes(), hooks, present


def test_native_git_job_commit_leaves_what_a_later_git_runs_as_it_was(installed):
    """A commit follows the mode, so nothing it starts may leave a program for the person's Git."""
    root, settings, host_git = _repository_project(installed, "git-programs")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_bytes(_HOOK_THAT_TRIES_GIT_ITSELF.encode())
    before = _program_state(root)
    assert before[2] == []
    (root / "work.txt").write_text("work\n")
    assert _git_job(settings, root, ["add", "work.txt"]).returncode == 0
    committed = _git_job(settings, root, ["commit", "-m", "work"])
    assert committed.returncode == 0, (committed.stdout, committed.stderr)
    assert committed.sandbox_policy["git_metadata"].startswith("writable except configuration")
    report = (root / "hook-report.txt").read_text().split()
    # The first line is the control: the hook can write where a commit writes.
    assert report == ["WROTE"] + ["REFUSED"] * 20, report
    assert (root / "hook-allowed.txt").read_text().strip() == "allowed"
    # What it prepared to move into place is still where it made it.
    assert (root / ".git" / "prepared").is_dir()
    assert (root / ".git" / "prepared-file").read_text().strip() == "x"
    # Nothing changed, and nothing the engine made to keep it so is left.
    assert _program_state(root) == before
    assert not any((root / name).exists() for name in (".git.old", ".git/config.old"))
    assert not (root / ".git" / "hooks.old").exists()
    assert not (root / ".git" / "config-alias").exists()
    assert (root / ".git" / "config").stat().st_nlink == 1
    assert host_git("log", "-1", "--format=%s")[1] == "work"
    assert host_git("config", "alias.theirs", "status") == (0, "")


def test_native_git_job_everyday_commands_need_no_metadata(installed):
    root, settings, host_git = _repository_project(installed, "git-everyday")
    before = _program_state(root)
    (root / "one.txt").write_text("one\n")
    steps = [
        ["add", "-A"],
        ["commit", "-m", "one"],
        ["switch", "-c", "topic"],
        ["commit", "--allow-empty", "-m", "on topic"],
        ["switch", "main"],
        ["merge", "--no-ff", "-m", "merge topic", "topic"],
        ["tag", "v1"],
        ["cherry-pick", "--allow-empty", "topic"],
        ["revert", "--no-edit", "HEAD~2"],
        ["reset", "--soft", "HEAD~1"],
        ["restore", "--staged", "."],
        ["stash", "push", "--include-untracked"],
        ["stash", "pop"],
    ]
    for argv in steps:
        done = _git_job(settings, root, argv)
        assert done.returncode == 0, (argv, done.stdout, done.stderr)
        # Git is not told about the names that are kept in place for it.
        assert "denied" not in done.stderr and "warning" not in done.stderr, (argv, done.stderr)
    # A deleted branch's settings would leave the configuration, which stays
    # as it is: the branch goes and Git says what it could not update.
    deleted = _git_job(settings, root, ["branch", "-D", "topic"])
    assert deleted.returncode == 0 and "config" in deleted.stderr, deleted.stderr
    assert host_git("branch", "--list", "topic")[1] == ""
    assert _program_state(root) == before
    assert host_git("tag", "--list")[1] == "v1"


def test_native_git_job_fetches_and_pushes_without_the_metadata(installed):
    """Git looks a remote up under names the engine keeps in place while it runs."""
    import subprocess

    from alysis_code import windows_toolchain

    root, settings, host_git = _repository_project(installed, "git-remote")
    remote = root / "remote.git"
    subprocess.run(
        [str(windows_toolchain.installed_tools()["git"][0]), "init", "-q", "--bare", str(remote)],
        check=True,
        capture_output=True,
    )
    assert host_git("remote", "add", "origin", remote.as_posix())[0] == 0
    before = _program_state(root)
    for argv in (
        ["push", "origin", "main"],
        ["fetch", "origin"],
        ["switch", "-c", "from-remote", "--no-track", "origin/main"],
        ["pull", "--no-rebase", "origin", "main"],
    ):
        done = _git_job(settings, root, argv)
        assert done.returncode == 0, (argv, done.stdout, done.stderr)
        assert "denied" not in done.stderr and "warning" not in done.stderr, (argv, done.stderr)
    assert _program_state(root) == before
    # Following a remote branch is written to the configuration, which stays as it is.
    tracked = _git_job(settings, root, ["switch", "-c", "tracked", "--track", "origin/main"])
    assert "permissions.git_metadata=true" in tracked.sandbox_policy["git_metadata"]
    assert _program_state(root) == before
    assert host_git("config", "--get", "branch.tracked.remote")[0] == 1


def test_native_git_job_rebase_waits_for_the_metadata_a_person_approves(installed):
    """A rebase keeps a list of commands in the Git directory that a later Git runs."""
    root, settings, host_git = _repository_project(installed, "git-rebase")
    for branch, name in (("topic", "topic.txt"), ("main", "main.txt")):
        assert host_git("switch", "-q", *(["-c"] if branch == "topic" else []), branch)[0] == 0
        (root / name).write_text(branch + "\n")
        assert host_git("add", name)[0] == 0
        assert host_git("commit", "-q", "-m", branch)[0] == 0
    assert host_git("switch", "-q", "topic")[0] == 0
    tip = host_git("rev-parse", "topic")[1]
    refused = _git_job(settings, root, ["rebase", "main"])
    # Git says what it could not do and exits 0, so the result names what lifts it.
    assert "rebase-merge" in refused.stderr
    assert "permissions.git_metadata=true" in refused.sandbox_policy["git_metadata"]
    assert host_git("rev-parse", "topic")[1] == tip
    assert host_git("status", "--porcelain", "--branch")[1].splitlines()[0] == "## topic"
    assert _program_state(root)[2] == []
    done = _git_job(settings, root, ["rebase", "main"], approved=True)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert not hasattr(done, "sandbox_policy")
    assert host_git("merge-base", "--is-ancestor", "main", "topic")[0] == 0
    # A person's yes to the command itself gives it the same.
    assert host_git("reset", "-q", "--hard", tip)[0] == 0
    assert host_git("merge-base", "--is-ancestor", "main", "topic")[0] == 1
    done = _git_job(settings, root, ["rebase", "main"], decided=True)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert not hasattr(done, "sandbox_policy")
    assert host_git("merge-base", "--is-ancestor", "main", "topic")[0] == 0


def test_native_git_job_changes_configuration_only_with_the_metadata(installed):
    root, settings, host_git = _repository_project(installed, "git-configuration")
    for argv in (
        ["config", "user.name", "Changed"],
        ["remote", "add", "origin", "https://example.invalid/project.git"],
    ):
        with pytest.raises(ConfigError, match="Unsupported literal Git operation"):
            _git_job(settings, root, argv)
        done = _git_job(settings, root, argv, approved=True)
        assert done.returncode == 0, (argv, done.stdout, done.stderr)
    assert host_git("config", "user.name")[1] == "Changed"
    assert host_git("remote", "get-url", "origin")[1] == "https://example.invalid/project.git"
    with pytest.raises(ConfigError, match="theirs to change"):
        _git_job(settings, root, ["config", "--global", "user.name", "Changed"], approved=True)


def test_native_shell_tool_changes_git_configuration_only_for_an_approved_command(installed):
    """The whole way: the shell tool, the route it picks, the sandbox."""
    from alysis_code.tools.shell import shell_run

    root, settings, host_git = _repository_project(installed, "git-shell-tool")
    settings = replace(settings, safe_git_writes=True)
    before = _program_state(root)
    command = "git config user.name Changed"
    refused = shell_run(
        root=root, cmd=command, runner=build_shell_runner_from_settings(settings, root)
    )
    assert refused["exit_code"] != 0
    assert refused["sandbox_policy"]["git_metadata"] == "protected"
    assert "permissions.git_metadata=true" in refused["sandbox_policy"]["guidance"]
    assert _program_state(root) == before
    # Reading it needs nothing.
    read = shell_run(
        root=root,
        cmd="git config user.name",
        runner=build_shell_runner_from_settings(settings, root),
    )
    assert read["exit_code"] == 0 and read["stdout"].strip() == "Probe"
    approved = replace(settings, git_metadata_access=True)
    done = shell_run(
        root=root, cmd=command, runner=build_shell_runner_from_settings(approved, root)
    )
    assert done["exit_code"] == 0, done
    assert host_git("config", "user.name")[1] == "Changed"
    # A commit through the shell tool, which nobody is asked about.
    (root / "work.txt").write_text("work")
    for step in ("git add work.txt", "git commit -m work"):
        done = shell_run(
            root=root, cmd=step, runner=build_shell_runner_from_settings(settings, root)
        )
        assert done["exit_code"] == 0, done
        assert done["sandbox_policy"]["git_metadata"].startswith("writable except configuration")
    assert host_git("log", "-1", "--format=%s")[1] == "work"


# The hook of a commit in one worktree, trying the Git directory of another.
_HOOK_THAT_TRIES_ANOTHER_WORKTREE = """#!/bin/sh
out=""
attempt() {
  if sh -c "$1" >/dev/null 2>&1; then out="$out WROTE"; else out="$out REFUSED"; fi
}
attempt 'echo allowed > hook-allowed.txt && echo allowed > "OWN/hook-allowed"'
attempt 'echo ../../../elsewhere > "OWN/commondir"'
attempt 'echo planted > "OWN/config.worktree"'
attempt 'mkdir "OWN/rebase-merge"'
attempt 'echo planted > "OTHER/config.worktree"'
attempt 'echo "ref: refs/heads/main" > "OTHER/HEAD"'
attempt 'mkdir "OTHER/rebase-merge"'
attempt 'echo ../../../elsewhere > "OTHER/commondir"'
attempt 'mkdir "OTHER/../planted"'
echo "$out" > hook-report.txt
# A worktree somebody adds while this command runs is not this command's either.
tries=0
while [ ! -d "OTHER/../late" ] && [ $tries -lt 40 ]; do sleep 0.5; tries=$((tries + 1)); done
if [ -f "OTHER/../late/commondir" ]; then late="SEEN"; else late="MISSING"; fi
attempt_late() {
  if sh -c "$1" >/dev/null 2>&1; then late="$late WROTE"; else late="$late REFUSED"; fi
}
attempt_late 'echo ../../../elsewhere > "OTHER/../late/commondir"'
attempt_late 'echo planted > "OTHER/../late/config.worktree"'
echo "$late" > hook-late.txt
exit 0
"""


@pytest.mark.parametrize("where", ["main", "linked"])
def test_native_git_job_leaves_another_worktree_to_its_own_git(installed, where):
    """A commit runs in one worktree while the person's Git commits in another and adds a third."""
    import threading

    root, settings, host_git = _repository_project(installed, "git-worktrees-" + where)
    side, other = root / "side", root / "other"
    for folder in (side, other):
        assert host_git("worktree", "add", "-q", "-b", folder.name, str(folder))[0] == 0
    theirs = root / ".git" / "worktrees" / "other"
    here = root if where == "main" else side
    mine = root / ".git" if where == "main" else root / ".git" / "worktrees" / "side"
    (root / ".git" / "hooks" / "pre-commit").write_bytes(
        _HOOK_THAT_TRIES_ANOTHER_WORKTREE.replace("OTHER", theirs.as_posix())
        .replace("OWN", mine.as_posix())
        .encode()
    )
    own_before = {name: (mine / name).read_bytes() for name in ("commondir",) if where != "main"}
    before = (
        _program_state(root),
        (theirs / "HEAD").read_bytes(),
        sorted(os.listdir(theirs)),
        (theirs / "commondir").read_bytes(),
    )
    (here / "mine.txt").write_text("mine\n")
    assert _git_job(settings, root, ["add", "mine.txt"], cwd=here).returncode == 0
    results = []
    job = threading.Thread(
        target=lambda: results.append(_git_job(settings, root, ["commit", "-m", "mine"], cwd=here))
    )
    job.start()
    try:
        deadline = time.monotonic() + 60
        while not (here / "hook-report.txt").is_file() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert job.is_alive(), "the commit ended before the other worktree was used"
        # The other worktree's Git replaces its own index and HEAD meanwhile.
        (other / "theirs.txt").write_text("theirs\n")
        # (Without the hook: outside the sandbox it would do what it tries.)
        for arguments in (
            ("add", "theirs.txt"),
            ("commit", "-q", "--no-verify", "-m", "theirs"),
        ):
            code, said = host_git("-C", str(other), *arguments)
            assert code == 0, (arguments, said)
        # And somebody adds a third worktree to the repository.
        code, said = host_git("worktree", "add", "-q", "-b", "late", str(root / "late"))
        assert code == 0, said
        late = (root / ".git" / "worktrees" / "late" / "commondir").read_bytes()
    finally:
        job.join()
    assert results[0].returncode == 0, (results[0].stdout, results[0].stderr)
    assert (here / "hook-report.txt").read_text().split() == ["WROTE"] + ["REFUSED"] * 8
    # The hook did see the worktree that was added, and could change nothing in it.
    assert (here / "hook-late.txt").read_text().split() == ["SEEN", "REFUSED", "REFUSED"]
    # In its own Git directory it writes what a commit writes, and not the names.
    assert (mine / "hook-allowed").read_text().strip() == "allowed"
    assert {name: (mine / name).read_bytes() for name in own_before} == own_before
    assert not {"config.worktree", "rebase-merge"} & set(os.listdir(mine))
    assert (where == "main") == ("commondir" not in os.listdir(mine))
    assert (root / ".git" / "worktrees" / "late" / "commondir").read_bytes() == late
    assert not (root / ".git" / "worktrees" / "late" / "config.worktree").exists()
    assert not (root / ".git" / "worktrees" / "planted").exists()
    assert (theirs / "commondir").read_bytes() == before[3]
    assert host_git("log", "refs/heads/other", "-1", "--format=%s")[1] == "theirs"
    mine = "refs/heads/main" if where == "main" else "refs/heads/side"
    assert host_git("log", mine, "-1", "--format=%s")[1] == "mine"
    assert _program_state(root) == before[0]
    assert (theirs / "HEAD").read_bytes() == before[1]
    assert set(os.listdir(theirs)) >= set(before[2])
    assert not {"config.worktree", "rebase-merge"} & set(os.listdir(theirs))


# A hook Git runs when it writes a reference, also the first one of a new repository.
_HOOK_IN_A_PREPARED_FOLDER = """#!/bin/sh
out=""
attempt() {
  if sh -c "$1" >/dev/null 2>&1; then out="$out WROTE"; else out="$out REFUSED"; fi
}
attempt 'echo "[alias]" >> "PROJECT/.git/config"'
attempt 'echo planted > "PROJECT/.git/hooks/post-commit"'
attempt 'echo "[alias]" >> "PROJECT/prepared/.git/config"'
echo "$out" >> "PROJECT/prepared-report.txt"
exit 0
"""


def test_native_git_init_gives_nothing_of_a_repository_that_exists(installed, monkeypatch):
    """Creating a repository follows the mode; the one around it is not part of that."""
    root, settings, host_git = _repository_project(installed, "git-init-inside")
    before = _program_state(root)
    requests = _requests(monkeypatch)
    project = str(root / ".git")
    # A change to the project's repository gets its Git directory, without the names.
    assert _git_job(settings, root, ["add", "README.md"]).returncode == 0
    assert project not in requests[-1]["deny_write"]
    assert str(root / ".git" / "config") in requests[-1]["deny_write"]
    # A new repository in a subfolder: the project's own stays closed as a whole.
    made = _git_job(settings, root, ["init", "-q", "-b", "main", "fresh"])
    assert made.returncode == 0, (made.stdout, made.stderr)
    assert project in requests[-1]["deny_write"]
    assert (root / "fresh" / ".git" / "HEAD").read_text().strip() == "ref: refs/heads/main"
    assert (root / "fresh" / ".git" / "config").is_file()
    assert _program_state(root) == before
    # A folder somebody prepared, with a hook for Git to run while it creates the rest.
    hooks = root / "prepared" / ".git" / "hooks"
    hooks.mkdir(parents=True)
    script = _HOOK_IN_A_PREPARED_FOLDER.replace("PROJECT", root.as_posix()).encode()
    for name in ("reference-transaction", "post-checkout", "post-index-change"):
        (hooks / name).write_bytes(script)
    prepared = _git_job(settings, root, ["init", "prepared"])
    # It is a change to the repository that is there, and the project's stays closed.
    assert project in requests[-1]["deny_write"]
    assert str(hooks) in requests[-1]["deny_write"]
    assert prepared.returncode != 0
    assert "permissions.git_metadata=true" in prepared.sandbox_policy["git_metadata"]
    assert _program_state(root) == before
    assert not (root / "prepared" / ".git" / "config").exists()
    report = root / "prepared-report.txt"
    if report.exists():  # the hook ran: everything it tried was refused
        assert set(report.read_text().split()) == {"REFUSED"}, report.read_text()
    # Asking Git to make the project's own repository again is such a change too.
    again = _git_job(settings, root, ["init"])
    assert str(root / ".git" / "config") in requests[-1]["deny_write"]
    assert _program_state(root) == before
    assert "permissions.git_metadata=true" in again.sandbox_policy["git_metadata"]
    # More than a plain init is not run without a person's decision.
    with pytest.raises(ConfigError, match="plain git init"):
        _git_job(settings, root, ["init", "--template=fresh", "templated"])
    assert not (root / "templated").exists()
    decided = _git_job(settings, root, ["init", "-q", "--bare", "bare.git"], decided=True)
    assert decided.returncode == 0, (decided.stdout, decided.stderr)
    assert (root / "bare.git" / "HEAD").is_file()


@pytest.mark.parametrize("reserved", [False, True])
def test_native_git_init_creates_the_project_repository(installed, reserved):
    root, settings = _project_without_reserved_names(installed, "git-init-project")
    root = root.resolve()
    if reserved:
        (root / ".git").mkdir()  # the empty entry a running command reserves the name with
    made = _git_job(settings, root, ["init", "-b", "main"])
    assert made.returncode == 0, (made.stdout, made.stderr)
    assert (root / ".git" / "HEAD").read_text().strip() == "ref: refs/heads/main"
    assert (root / ".git" / "config").is_file() and (root / ".git" / "hooks").is_dir()
    # Staging in it needs nobody either.
    done = _git_job(settings, root, ["add", "README.md"])
    assert done.returncode == 0, (done.stdout, done.stderr)


def test_native_git_init_of_a_repository_below_a_project_without_one_returns(installed):
    """The job looked upwards for the repository, from a folder above it, and never came back."""
    import subprocess
    import threading

    from alysis_code import windows_toolchain

    root, settings = _project_without_reserved_names(installed, "git-init-below")
    root = root.resolve()
    git = str(windows_toolchain.installed_tools()["git"][0])
    subprocess.run([git, "init", "-q", str(root / "app")], check=True, capture_output=True)
    before = (root / "app" / ".git" / "config").read_bytes()
    results = []

    def job():
        try:
            results.append(_git_job(settings, root, ["init", "app"]))
        except Exception as exc:  # noqa: BLE001 - the outcome is what the test looks at
            results.append(exc)

    thread = threading.Thread(target=job, daemon=True)
    thread.start()
    thread.join(120)
    assert not thread.is_alive(), "git init app did not return"
    (outcome,) = results
    assert not isinstance(outcome, Exception), outcome
    assert (root / "app" / ".git" / "config").read_bytes() == before
    assert "permissions.git_metadata=true" in outcome.sandbox_policy["git_metadata"]
    # A link named .git that leads nowhere is not looked past either.
    (root / "linked").mkdir()
    (root / "gone").mkdir()
    _link(root / "gone", root / "linked" / ".git")
    (root / "gone").rmdir()
    with pytest.raises(ConfigError, match="leads nowhere"):
        _git_job(settings, root, ["init", "linked"])


def test_native_git_job_refuses_a_repository_whose_hooks_are_a_link(installed):
    """The sandbox refuses a path by name; behind a link the name is another folder's."""
    root, settings, host_git = _repository_project(installed, "git-linked-hooks")
    elsewhere = root.parent / "hooks-elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(root / ".git" / "hooks")
    _link(elsewhere, root / ".git" / "hooks")
    head = host_git("rev-parse", "HEAD")[1]
    with pytest.raises(ConfigError, match="cannot be kept as they are"):
        _git_job(settings, root, ["commit", "--allow-empty", "-m", "through a link"])
    assert host_git("rev-parse", "HEAD")[1] == head
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("kind", ["foreground", "background"])
def test_native_command_cannot_change_the_repository_it_is_no_longer_held_for(installed, kind):
    """The helper is not asked to hold the repository; the read-only rule has to do it alone."""
    root, settings, _ = _repository_project(installed, "unheld-" + kind)
    before = _git_metadata(root)
    if kind == "foreground":
        completed = build_shell_runner_from_settings(settings, root).run(
            root=root, cwd=root, cmd=_TRY_THE_REPOSITORY, timeout_s=30
        )
        said = completed.stdout
    else:
        started = build_background_shell_runner_from_settings(settings, root).start(
            root=root, cwd=root, cmd=_TRY_THE_REPOSITORY + "; Start-Sleep -Seconds 60"
        )
        try:
            said = started.popen.stdout.readline().decode()
        finally:
            started.cleanup()
    assert said.strip() == ",".join(["REFUSED"] * 5), said
    assert _git_metadata(root) == before and (root / ".git").is_dir()
    assert not (root / "gone").exists()


def test_native_repository_cannot_go_away_while_a_background_command_runs(installed):
    """Under a rule that names a path, a folder that went away could be made anew by the command."""
    root, settings, _ = _repository_project(installed, "held-repository")
    started = _background(settings, root)
    try:
        with pytest.raises(PermissionError):
            (root / ".git").rename(root / "gone")
        assert (root / ".git" / "HEAD").is_file()
    finally:
        started.cleanup()
    (root / ".git").rename(root / "gone")
    (root / "gone").rename(root / ".git")


def test_native_repository_cannot_go_away_while_a_foreground_command_runs(installed):
    """Its reservations hold the repository; the helper is no longer asked to."""
    import threading

    root, settings, _ = _repository_project(installed, "held-foreground")
    results = []
    foreground = threading.Thread(
        target=lambda: results.append(
            build_shell_runner_from_settings(settings, root).run(
                root=root, cwd=root, cmd="Start-Sleep -Seconds 8; 'FOREGROUND_DONE'", timeout_s=60
            )
        )
    )
    foreground.start()
    try:
        time.sleep(3)
        assert foreground.is_alive()
        with pytest.raises(PermissionError):
            (root / ".git").rename(root / "gone")
        assert (root / ".git" / "HEAD").is_file()
    finally:
        foreground.join()
    assert "FOREGROUND_DONE" in results[0].stdout, (results[0].stdout, results[0].stderr)
    (root / ".git").rename(root / "gone")
    (root / "gone").rename(root / ".git")


def test_native_reserved_names_in_an_attached_folder_are_taken_back_too(installed):
    """A folder attached to the session is a project of the command like any other."""
    from alysis_code.project_folders import folder_scope

    root, settings = _project_without_reserved_names(installed, "with-attached-folder")
    attached = (root.parent / "attached-folder").resolve()
    attached.mkdir()
    (attached / "README.md").write_text("the person's file\n")
    with folder_scope(root, [str(attached)]):
        started = _background(settings, root)
    try:
        assert _entries(root) == _RESERVED_NAMES | {"README.md"}
        assert _entries(attached) == _RESERVED_NAMES | {"README.md"}
    finally:
        started.cleanup()
    assert _entries(root) == {"README.md"}
    assert _entries(attached) == {"README.md"}


def test_native_reserved_names_go_when_the_command_that_kept_the_project_busy_ends(
    installed, monkeypatch
):
    """The last background command stops while a foreground command is running."""
    import threading

    root, settings = _project_without_reserved_names(installed, "busy-at-stop")
    started = _background(settings, root)
    monkeypatch.setattr(mxc, "_RELEASE_LEASE_WAIT_S", 0.5)
    results = []
    foreground = threading.Thread(
        target=lambda: results.append(
            build_shell_runner_from_settings(settings, root).run(
                root=root, cwd=root, cmd="Start-Sleep -Seconds 8; 'FOREGROUND_DONE'", timeout_s=60
            )
        )
    )
    foreground.start()
    try:
        time.sleep(2)  # the foreground command took the project's lease at its start
        started.cleanup()
        assert foreground.is_alive()
        assert _entries(root) == _RESERVED_NAMES | {"README.md"}
    finally:
        foreground.join()
    assert "FOREGROUND_DONE" in results[0].stdout, (results[0].stdout, results[0].stderr)
    assert _entries(root) == {"README.md"}


def test_native_command_is_not_charged_with_the_names_taken_back_while_it_ran(installed):
    """A watched command starts in a project where an engine left reserved names."""
    from alysis_code._windows_git_reservations import GitReservations
    from alysis_code.agent.turn.snapshot import _run_with_command_mutation_detection
    from alysis_code.windows_mxc_reservations import BackgroundRecord

    root, settings = _project_without_reserved_names(installed, "watched-command")
    root = root.resolve()
    reservations = GitReservations()
    reservations.reserve(root, [str(root / name) for name in native._PROTECTED_FILES])
    BackgroundRecord(root, reservations.detach()).let_go()
    assert _entries(root) == set(native._PROTECTED_FILES) | {"README.md"}
    completed, touched = _run_with_command_mutation_detection(
        root=root,
        enabled=True,
        operation=lambda: build_shell_runner_from_settings(settings, root).run(
            root=root, cwd=root, cmd="Set-Content -LiteralPath made.txt -Value x", timeout_s=25
        ),
    )
    assert completed.returncode == 0, completed.stderr
    assert touched == ["made.txt"]
    assert _entries(root) == {"README.md", "made.txt"}


def test_native_git_job_takes_leftover_names_back_before_it_stages_everything(installed):
    """Otherwise "add -A" stages the empty files, and the engine deletes them later."""
    import subprocess

    from alysis_code import git_native, windows_toolchain
    from alysis_code._windows_git_reservations import GitReservations
    from alysis_code.windows_mxc_reservations import BackgroundRecord

    tools = windows_toolchain.installed_tools()
    if "git" not in tools:
        pytest.skip("the bundled Git is needed to make the repository")
    git = str(tools["git"][0])
    root, settings = _project_without_reserved_names(installed, "git-leftovers")
    root = root.resolve()
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}

    def host_git(*arguments):
        return subprocess.run(
            [git, *arguments], cwd=root, capture_output=True, text=True, env=environment, check=True
        ).stdout

    host_git("init", "-q", "-b", "main")
    host_git("config", "user.email", "probe@example.invalid")
    host_git("config", "user.name", "Probe")
    host_git("add", "-A")
    host_git("commit", "-q", "-m", "initial")
    reservations = GitReservations()
    reservations.reserve(root, [str(root / name) for name in native._PROTECTED_FILES])
    BackgroundRecord(root, reservations.detach()).let_go()
    (root / "feature.txt").write_text("work\n")
    result = git_native.run(
        settings=replace(settings, safe_git_writes=True),
        root=root,
        cwd=root,
        argv=["add", "-A"],
        timeout_s=60,
        cancel_check=None,
    )
    assert result.returncode == 0, result.stderr
    assert host_git("diff", "--cached", "--name-only").split() == ["feature.txt"]
    assert not any((root / name).exists() for name in native._PROTECTED_FILES)


def test_native_command_without_a_record_keeps_its_reserved_names_when_another_stops(
    installed, monkeypatch
):
    """A command that started in a busy project has no record to say it relies on them.

    What keeps its names is the helper, which has them open for as long as the
    command runs. Taking them back fails on that and is tried again later.
    """
    import threading

    from alysis_code._windows_git_reservations import GitLease

    root, settings = _project_without_reserved_names(installed, "unrecorded-command")
    recorded = _background(settings, root)
    monkeypatch.setattr(mxc, "_BACKGROUND_LEASE_WAIT_S", 0.5)
    taken, done = threading.Event(), threading.Event()

    def another_command():
        lease = GitLease(root.resolve(), 10, None)
        taken.set()
        done.wait(60)
        lease.close()

    thread = threading.Thread(target=another_command)
    thread.start()
    try:
        assert taken.wait(10)
        unrecorded = _background(settings, root)
    finally:
        done.set()
        thread.join()
    try:
        recorded.cleanup()
        assert _entries(root) == _RESERVED_NAMES | {"README.md"}
        completed = build_shell_runner_from_settings(settings, root).run(
            root=root, cwd=root, cmd=_TRY_TO_PLANT, timeout_s=25
        )
        assert completed.stdout.strip() == "REFUSED", (completed.stdout, completed.stderr)
        assert _entries(root) == _RESERVED_NAMES | {"README.md"}
    finally:
        recorded.cleanup()
        unrecorded.cleanup()
    # Nothing holds them any more; the next command takes them back.
    build_shell_runner_from_settings(settings, root).run(
        root=root, cwd=root, cmd="'done'", timeout_s=25
    )
    assert _entries(root) == {"README.md"}


def test_native_stopping_a_background_command_removes_its_scratch_folder(installed):
    """Also while a process of it still holds a file there open."""
    root, settings, _ = installed
    started = build_background_shell_runner_from_settings(settings, root).start(
        root=root,
        cwd=root,
        cmd=(
            "$held=[IO.File]::Open((Join-Path $env:TEMP 'held.txt'),'Create');"
            "[Console]::Out.WriteLine($env:TEMP); Start-Sleep -Seconds 120"
        ),
    )
    try:
        temp = Path(started.popen.stdout.readline().decode().strip())
        assert temp.is_dir() and (temp / "held.txt").exists()
        assert started.popen.poll() is None
    finally:
        started.cleanup()
    assert not temp.exists()
    assert not temp.parents[3].exists()  # the scratch folder itself


def test_native_command_that_cannot_write_gets_no_scratch_folder(installed):
    root, settings, _ = installed
    completed = build_shell_runner_from_settings(
        replace(settings, filesystem_access="read-only"), root
    ).run(
        root=root,
        cwd=root,
        cmd="[ordered]@{temp=$env:TEMP;profile=[string]$env:USERPROFILE}|ConvertTo-Json -Compress",
        timeout_s=25,
    )
    assert _scratch_report(completed) == {"temp": str(root), "profile": ""}


def _path_folder(parent, monkeypatch):
    """A folder with one program in it, put first on the engine's PATH."""
    folder = parent / "persons-tools"
    (folder / "lib").mkdir(parents=True)
    (folder / "probe-tool.cmd").write_text("@echo PROBE_TOOL_RAN\r\n")
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ["PATH"])
    return folder


def test_native_command_runs_a_program_from_the_persons_path(installed, monkeypatch):
    root, settings, _ = installed
    folder = _path_folder(root.parent, monkeypatch)
    beside = root.parent / "beside-the-tools"
    beside.mkdir()
    (beside / "private.txt").write_text("PRIVATE_CONTENT")
    completed = build_shell_runner_from_settings(settings, root).run(
        root=root,
        cwd=root,
        cmd=(
            "probe-tool; "
            f"try{{Set-Content -LiteralPath '{folder}\\planted.cmd' -Value x -ErrorAction Stop;"
            "'TOOLS_WRITTEN'}catch{'TOOLS_READ_ONLY'}; "
            f"try{{Get-Content -LiteralPath '{beside}\\private.txt' -ErrorAction Stop}}"
            "catch{'BESIDE_UNREADABLE'}"
        ),
        timeout_s=25,
    )
    assert "PROBE_TOOL_RAN" in completed.stdout, (completed.stdout, completed.stderr)
    assert "TOOLS_READ_ONLY" in completed.stdout and "BESIDE_UNREADABLE" in completed.stdout
    assert "PRIVATE_CONTENT" not in completed.stdout
    assert not (folder / "planted.cmd").exists()


def test_native_link_inside_a_path_folder_does_not_open_what_it_points_at(installed, monkeypatch):
    """A PATH folder is not searched for links, so the sandbox has to refuse them."""
    import subprocess

    root, settings, _ = installed
    folder = _path_folder(root.parent, monkeypatch)
    outside = root.parent / "outside-every-grant"
    outside.mkdir()
    (outside / "canary.txt").write_text("OUTSIDE_CANARY")
    (outside / "linked.txt").write_text("LINKED_ORIGINAL")
    junction = folder / "lib" / "junction"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True
    )
    assert made.returncode == 0, made.stderr
    os.link(outside / "linked.txt", folder / "lib" / "hard.txt")
    try:
        completed = build_shell_runner_from_settings(settings, root).run(
            root=root,
            cwd=root,
            cmd=(
                "probe-tool; "
                f"try{{Get-Content -LiteralPath '{junction}\\canary.txt' -ErrorAction Stop}}"
                "catch{'JUNCTION_REFUSED'}; "
                f"try{{Set-Content -LiteralPath '{folder}\\lib\\hard.txt' -Value changed "
                "-ErrorAction Stop;'HARD_LINK_WRITTEN'}catch{'HARD_LINK_READ_ONLY'}"
            ),
            timeout_s=25,
        )
    finally:
        os.rmdir(junction)
    assert "PROBE_TOOL_RAN" in completed.stdout, (completed.stdout, completed.stderr)
    assert "JUNCTION_REFUSED" in completed.stdout and "OUTSIDE_CANARY" not in completed.stdout
    assert "HARD_LINK_READ_ONLY" in completed.stdout
    assert (outside / "linked.txt").read_text() == "LINKED_ORIGINAL"


def test_native_command_that_cannot_write_does_not_get_the_persons_path(installed, monkeypatch):
    root, settings, _ = installed
    _path_folder(root.parent, monkeypatch)
    completed = build_shell_runner_from_settings(
        replace(settings, filesystem_access="read-only"), root
    ).run(
        root=root,
        cwd=root,
        cmd="if (Get-Command probe-tool -ErrorAction SilentlyContinue) {'FOUND'} else {'ABSENT'}",
        timeout_s=25,
    )
    assert completed.stdout.strip() == "ABSENT", (completed.stdout, completed.stderr)


_HOME_CONTENT = {
    ".toolstate/settings.toml": "TOOL_SETTINGS",
    ".toolstate/credentials.toml": "TOOL_CREDENTIAL",
    ".ssh/id_ed25519": "SSH_KEY",
    ".othertool/state.txt": "OTHER_TOOL_STATE",
    ".othertool/browser/profile/Local State": "DEEP_PROFILE",
    "Documents/letter.txt": "PERSONS_LETTER",
    ".gitconfig": "HOME_IDENTITY",
    ".npmrc": "NPM_TOKEN",
    "AppData/Roaming/probe-tool/state.txt": "ROAMING_TOOL_STATE",
    "AppData/Roaming/Messenger/session.key": "MESSENGER_SESSION",
    "AppData/Roaming/chat/Local State": "CHAT_PROFILE",
    "AppData/Local/browser/data": "LOCAL_DATA",
}
_READ_THE_HOME = (
    "$h = $env:USERPROFILE; "
    "function Peek($p) { try { Get-Content -LiteralPath (Join-Path $h $p) -Raw -ErrorAction Stop } "
    "catch { 'REFUSED ' + $p } }; "
    "'HOME=' + $h; 'ROAMING=' + $env:APPDATA; 'LOCAL=' + $env:LOCALAPPDATA; 'TEMP=' + $env:TEMP; "
    + "; ".join(f"Peek '{name}'" for name in _HOME_CONTENT)
    + "; try { Set-Content -LiteralPath (Join-Path $h '.toolstate\\planted') -Value x "
    "-ErrorAction Stop; 'HOME_WRITTEN' } catch { 'HOME_READ_ONLY' }; "
    "try { Set-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'cache.txt') -Value x "
    "-ErrorAction Stop; 'LOCAL_WRITTEN' } catch { 'LOCAL_READ_ONLY' }"
)


def _persons_home(parent, monkeypatch):
    """A profile with a tool's state, credentials and the person's own files in it.

    The engine is told it is the person's home; nothing of the real profile
    of the machine the test runs on is involved.
    """
    home = parent / "home"
    for name, content in _HOME_CONTENT.items():
        path = home.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _use_home(monkeypatch, home, home / "AppData" / "Roaming")
    return home


def _home_files(home):
    return sorted(path.relative_to(home).as_posix() for path in home.rglob("*") if path.is_file())


def test_native_command_reads_what_tools_keep_in_the_persons_home_and_writes_none_of_it(
    installed, monkeypatch
):
    """rustup's cargo found no compiler: the home it looked in was an empty scratch folder."""
    import ctypes

    root, settings, _ = installed
    tools = _path_folder(root.parent, monkeypatch)
    # "chat" has its program on the PATH, so only what its folder holds keeps
    # it unreadable. "Messenger" has one too, in a PATH folder the command
    # itself can write: that is no evidence, a command could have put it there.
    (tools / "chat.cmd").write_text("@echo chat\r\n")
    forged = root / "tools"
    forged.mkdir()
    (forged / "Messenger.cmd").write_text("@echo forged\r\n")
    monkeypatch.setenv("PATH", str(forged) + os.pathsep + os.environ["PATH"])
    home = _persons_home(root.parent, monkeypatch)
    before = _home_files(home)
    # Other spellings of the refused credential file.
    credential = home / ".toolstate" / "credentials.toml"
    short = ctypes.create_unicode_buffer(600)
    ctypes.windll.kernel32.GetShortPathNameW(str(credential), short, 600)
    spellings = {"\\\\?\\" + str(credential), short.value or str(credential)}
    command = _READ_THE_HOME + "".join(
        f"; try {{ [IO.File]::ReadAllText('{spelling}') }} catch {{ 'REFUSED spelling' }}"
        for spelling in sorted(spellings)
    )
    completed = build_shell_runner_from_settings(settings, root).run(
        root=root, cwd=root, cmd=command, timeout_s=30
    )
    said = completed.stdout
    assert f"HOME={home}" in said and f"ROAMING={home}\\AppData\\Roaming" in said, (
        said,
        completed.stderr,
    )
    # A tool's settings, Git's identity, and the roaming folder named like a
    # program on the PATH.
    for readable in ("TOOL_SETTINGS", "OTHER_TOOL_STATE", "HOME_IDENTITY", "ROAMING_TOOL_STATE"):
        assert readable in said, (readable, said)
    for private in (
        "TOOL_CREDENTIAL",
        "SSH_KEY",
        "DEEP_PROFILE",
        "PERSONS_LETTER",
        "NPM_TOKEN",
        "MESSENGER_SESSION",
        "CHAT_PROFILE",
        "LOCAL_DATA",
    ):
        assert private not in said, (private, said)
    assert said.count("REFUSED ") == 8 + len(spellings), said
    assert "HOME_READ_ONLY" in said and "LOCAL_WRITTEN" in said, said
    # What it writes goes to a local application data and a temp folder of
    # its own, outside the home and gone with the command.
    own = {
        line.split("=", 1)[0]: Path(line.split("=", 1)[1])
        for line in said.splitlines()
        if "=" in line
    }
    assert not own["LOCAL"].is_relative_to(home) and not own["TEMP"].is_relative_to(home)
    assert not own["LOCAL"].exists()
    assert _home_files(home) == before


def test_native_credential_file_the_person_approved_stays_readable(installed, monkeypatch):
    """Refusing it by its name must not undo an approval that names the file."""
    root, settings, _ = installed
    home = _persons_home(root.parent, monkeypatch)
    approved = home / ".toolstate" / "credentials.toml"
    completed = build_shell_runner_from_settings(
        replace(settings, additional_read_files=(str(approved),)), root
    ).run(root=root, cwd=root, cmd=f"[IO.File]::ReadAllText('{approved}')", timeout_s=25)
    assert "TOOL_CREDENTIAL" in completed.stdout, (completed.stdout, completed.stderr)


def test_native_background_command_reads_the_persons_home_too(installed, monkeypatch):
    root, settings, _ = installed
    home = _persons_home(root.parent, monkeypatch)
    started = build_background_shell_runner_from_settings(settings, root).start(
        root=root,
        cwd=root,
        cmd=(
            "[Console]::Out.WriteLine((Get-Content -LiteralPath "
            "(Join-Path $env:USERPROFILE '.toolstate\\settings.toml') -Raw)); "
            "Start-Sleep -Seconds 60"
        ),
    )
    try:
        assert started.popen.stdout.readline().strip() == b"TOOL_SETTINGS"
    finally:
        started.cleanup()
    assert sorted(path.name for path in home.iterdir()) == sorted(
        {name.split("/")[0] for name in _HOME_CONTENT}
    )


def test_native_command_that_cannot_write_keeps_the_home_unreadable(installed, monkeypatch):
    """Like the PATH folders: a read-only command gets nothing beyond its folders."""
    root, settings, _ = installed
    home = _persons_home(root.parent, monkeypatch)
    completed = build_shell_runner_from_settings(
        replace(settings, filesystem_access="read-only"), root
    ).run(
        root=root,
        cwd=root,
        cmd=(
            f"try {{ Get-Content -LiteralPath '{home}\\.toolstate\\settings.toml' -Raw "
            "-ErrorAction Stop } catch { 'HOME_UNREADABLE' }; 'TOLD=' + $env:USERPROFILE"
        ),
        timeout_s=25,
    )
    assert "HOME_UNREADABLE" in completed.stdout, (completed.stdout, completed.stderr)
    assert "TOOL_SETTINGS" not in completed.stdout and str(home) not in completed.stdout


def test_native_rustup_cargo_finds_its_toolchain_and_checks_a_crate_offline(installed, monkeypatch):
    """The failure this was found with, on a machine that has Rust from rustup."""
    profile = _machine_home(monkeypatch)
    cargo = profile / ".cargo" / "bin"
    if not (cargo / "cargo.exe").is_file() or not (profile / ".rustup" / "toolchains").is_dir():
        pytest.skip("needs Rust installed with rustup")
    root, settings, _ = installed
    monkeypatch.setenv("PATH", str(cargo) + os.pathsep + os.environ["PATH"])
    (root / "src").mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname = "probe"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (root / "src" / "main.rs").write_text('fn main() {\n    println!("ran");\n}\n')
    completed = build_shell_runner_from_settings(settings, root).run(
        root=root,
        cwd=root,
        cmd='cmd /c "cargo --version 2>&1 && rustc --version 2>&1 && cargo check --offline 2>&1"',
        timeout_s=240,
    )
    said = completed.stdout
    assert completed.returncode == 0, (said, completed.stderr)
    assert "no default is configured" not in said
    assert "cargo " in said and "rustc " in said and "Finished" in said, said


@pytest.mark.parametrize("access", ["workspace-write", "read-only"])
def test_native_npm_and_node_children_do_not_pollute_project_with_compile_cache(installed, access):
    root, settings, _ = installed
    (root / "package.json").write_text(
        json.dumps({"private": True, "scripts": {"test": "node cache-probe.cjs"}})
    )
    (root / "cache-probe.cjs").write_text(
        "const cp=require('node:child_process'),a=require('node:assert/strict');"
        "require('node:module').enableCompileCache();"
        "if(process.argv.length===2){"
        " const child=cp.spawnSync(process.execPath,[__filename,'child'],{encoding:'utf8'});"
        " a.equal(child.status,0,child.stderr);a.ok(child.stdout.includes('CACHE_CHILD_OK'));"
        " console.log('CACHE_PARENT_OK');"
        "}else console.log('CACHE_CHILD_OK');"
    )
    completed = build_shell_runner_from_settings(
        replace(settings, filesystem_access=access), root
    ).run(root=root, cwd=root, cmd="npm.cmd test; exit $LASTEXITCODE", timeout_s=30)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "CACHE_PARENT_OK" in completed.stdout
    assert not (root / "node-compile-cache").exists(), "npm polluted the project with its cache"


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
def test_native_successful_rerun_resolves_its_own_failed_verification(installed, tool):
    from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect
    from alysis_code.tools.shell import shell_run
    from alysis_code.verify_gate import run_task_verification, verify_run_result_to_payload

    root, settings, _ = installed
    (root / "package.json").write_text(
        json.dumps({"private": True, "scripts": {"test": "node test.cjs"}})
    )
    (root / "test.cjs").write_text(
        "require('node:assert/strict').equal(require('./value.cjs'),42);console.log('RERUN_OK');"
    )
    state = TurnExecutionState(execution_requested=True, touched_repo_paths={"value.cjs"})
    contexts = []
    for value in [0, 42]:
        (root / "value.cjs").write_text(f"module.exports={value};")
        if value == 42:
            # What the person's home holds changes between a check and its
            # rerun: a tool writes a file there. The rerun is the same command.
            appeared = Path(os.environ["USERPROFILE"]) / ".appeared-meanwhile"
            appeared.mkdir()
            (appeared / "state.txt").write_text("x")
            (appeared.parent / ".new-settings").write_text("x")
        state.note_verification_relevant_edit()
        command = "npm.cmd test"
        if tool == "shell_run":
            arguments = {"cmd": command, "cwd": "."}
            result = shell_run(
                root=root, cmd=command, runner=mxc.WindowsMxcRunner(settings), timeout_s=30
            )
            code = result["exit_code"]
            contexts.append(result.get("execution_context"))
        else:
            arguments = {"commands": [command]}
            executed = run_task_verification(
                root=root,
                commands=[command],
                artifact_path=root.parent / "verify.txt",
                cfg=AppConfig(
                    model="unused",
                    extra_fields={
                        "shell_sandbox": {"backend": "windows-mxc"},
                        "verify_sandbox": {"mode": "strict"},
                    },
                ),
                timeout_s=30,
            )
            result = verify_run_result_to_payload(root=root, result=executed)
            code = result["command_results"][0]["exit_code"]
            contexts.append(result["command_results"][0].get("execution_context"))
        assert (code == 0) == (value == 42), result
        _record_tool_effect(
            root=root,
            state=state,
            tool_name=tool,
            arguments=arguments,
            status="ok",
            result=result,
            known_verification_commands=["npm test"],
            scope_environment_known=False,
        )
        if value == 0:
            assert state.last_verification_passed is False
            assert state.observed_verification_failures
    assert state.last_verification_passed is True
    assert not state.observed_verification_failures
    assert contexts[0] and contexts[0] == contexts[1]


@pytest.mark.parametrize("preview", [False, True])
def test_native_preview_is_explicit_and_stop_closes_listener(installed, preview):
    import urllib.request

    from alysis_code.terminal_manager import TerminalManager

    root, settings, node = installed
    settings = replace(settings, preview_access="local" if preview else "auto")
    (root / "preview.cjs").write_text(
        "const h=require('http'),f=require('fs');const s=h.createServer((q,r)=>r.end('PREVIEW'));"
        "s.listen(0,'127.0.0.1',()=>f.writeFileSync('port.txt',String(s.address().port)));"
    )
    runner = build_background_shell_runner_from_settings(settings, root)
    manager = TerminalManager(runner=runner, settings=settings)
    try:
        identifier = manager.start(
            root=root, cwd=root, cmd=f"& '{node}' './preview.cjs'; exit $LASTEXITCODE"
        )
        deadline = time.monotonic() + 15
        while not (root / "port.txt").exists():
            assert time.monotonic() < deadline, manager.read(identifier)
            time.sleep(0.05)
        url = f"http://127.0.0.1:{int((root / 'port.txt').read_text())}"
        if preview:
            with urllib.request.urlopen(url, timeout=3) as response:
                assert response.read() == b"PREVIEW"
        else:
            with pytest.raises(OSError):
                urllib.request.urlopen(url, timeout=1)
        manager.kill(identifier)
        with pytest.raises(OSError):
            urllib.request.urlopen(url, timeout=1)
    finally:
        manager.shutdown_all()


@pytest.mark.parametrize("access", ["workspace-write", "read-only"])
@pytest.mark.parametrize("network", ["off", "on"])
@pytest.mark.parametrize("execution", ["foreground", "background"])
def test_native_multiple_folders_and_descendants_keep_boundary(
    installed, access, network, execution
):
    root, settings, node = installed
    second = root.parent / "second project's folder"
    second.mkdir()
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "canary").write_text("unchanged")
    (second / "AGENTS.md").write_text("second instructions")
    settings = replace(
        settings,
        additional_workspace_roots=(str(second),),
        filesystem_access=access,
        network=network,
    )
    script = root / "multi.cjs"
    script.write_text(
        "const fs=require('node:fs'),cp=require('node:child_process'),path=require('node:path');"
        "const roots=" + json.dumps([str(root), str(second)]) + ";"
        "function denied(f){try{f()}catch(e){if(['EACCES','EPERM'].includes(e.code))return;throw e}throw Error('escaped')}"
        "for(const root of roots){const p=path.join(root,'ordinary.txt');"
        + (
            "fs.writeFileSync(p,'allowed');"
            if access == "workspace-write"
            else "denied(()=>fs.writeFileSync(p,'bad'));"
        )
        + "for(const name of ['AGENTS.md','.git/config','.codex/config.toml','.github/skills/unsafe'])"
        "denied(()=>fs.writeFileSync(path.join(root,name),'bad'));}"
        "denied(()=>fs.readFileSync(" + json.dumps(str(outside / "canary")) + "));"
        "denied(()=>fs.writeFileSync(" + json.dumps(str(outside / "canary")) + ",'bad'));"
        "if(!process.argv.includes('child')){const c=cp.spawnSync(process.execPath,[__filename,'child'],{encoding:'utf8',cwd:roots[1]});"
        "if(c.status!==0)throw Error(c.stderr+' '+c.stdout);}console.log('MULTI_OK');"
    )
    # Existing metadata files are needed in read-only mode; no host reservations occur.
    for directory in (second / ".git", second / ".codex", second / ".github/skills"):
        directory.mkdir(parents=True)
    runner = build_shell_runner_from_settings(settings, root)
    command = f"& '{str(node).replace(chr(39), chr(39) * 2)}' '{str(script).replace(chr(39), chr(39) * 2)}'; exit $LASTEXITCODE"
    if execution == "foreground":
        result = runner.run(root=root, cwd=second, cmd=command, timeout_s=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "MULTI_OK" in result.stdout
    else:
        started = build_background_shell_runner_from_settings(settings, root).start(
            root=root, cwd=second, cmd=command
        )
        try:
            stdout, stderr = started.popen.communicate(timeout=30)
            assert started.popen.returncode == 0, stdout + stderr
            assert b"MULTI_OK" in stdout
        finally:
            if started.popen.poll() is None:
                started.popen.kill()
                started.popen.communicate(timeout=5)
            started.cleanup()
    assert (outside / "canary").read_text() == "unchanged"
    assert (second / "AGENTS.md").read_text() == "second instructions"


def test_native_private_state_denies_access_and_pins_parent_until_exit(installed, monkeypatch):
    root, settings, node = installed
    parent = root / "private-state"
    config, data = parent / "config", parent / "data"
    config.mkdir(parents=True)
    data.mkdir()
    (config / "secret").write_text("synthetic-private")
    control = parent / "desktop-control.json"
    control.write_text("synthetic-control")
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(config))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(data))
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(control)]))
    moved = root / "private-state-moved"
    parent.rename(moved)  # Original host can rename the exact parent while idle.
    moved.rename(parent)
    script = root / "private-probe.cjs"
    script.write_text(
        "const fs=require('node:fs');"
        "for(const op of [()=>fs.readFileSync('private-state/config/secret'),"
        "()=>fs.writeFileSync('private-state/config/secret','bad'),"
        "()=>fs.readFileSync('private-state/desktop-control.json'),"
        "()=>fs.renameSync('private-state','escaped-private')]){"
        "try{op();throw Error('private access escaped')}catch(e){"
        "if(!['EACCES','EPERM','EBUSY'].includes(e.code))throw e}}"
        "fs.writeFileSync('private-ready','1');"
        "const timer=setInterval(()=>{if(!fs.existsSync('private-release'))return;"
        "clearInterval(timer);console.log('PRIVATE_STATE_DENIED')},20);"
    )
    runner = build_shell_runner_from_settings(settings, root)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            runner.run,
            root=root,
            cwd=root,
            cmd=f"& '{node}' './private-probe.cjs'; exit $LASTEXITCODE",
            timeout_s=15,
        )
        try:
            deadline = time.monotonic() + 10
            while not (root / "private-ready").exists():
                assert not pending.done(), pending.result() if pending.done() else None
                assert time.monotonic() < deadline, "private probe never became ready"
                time.sleep(0.02)
            with pytest.raises(OSError) as denied:
                parent.rename(moved)
            assert denied.value.winerror in {5, 32}, denied.value
        finally:
            (root / "private-release").write_text("1")
        completed = pending.result(timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PRIVATE_STATE_DENIED" in completed.stdout
    assert (config / "secret").read_text() == "synthetic-private"
    assert control.read_text() == "synthetic-control"
    parent.rename(moved)  # Lifetime pin released after the job drained.
    moved.rename(parent)


@pytest.mark.parametrize("prefix_name", ["prefix", "base_prefix", "desktop"])
def test_native_host_runtime_modules_are_readable_but_cannot_be_replaced(
    installed, monkeypatch, prefix_name
):
    root, settings, node = installed
    runtime = root / "synthetic-python"
    library = runtime / "Lib"
    library.mkdir(parents=True)
    original = library / "late_import.py"
    original.write_text("SYNTHETIC_ORIGINAL")
    # Patch only metadata used to construct the policy, never a real installation.
    if prefix_name == "desktop":
        monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(runtime)]))
    else:
        monkeypatch.setattr(native.sys, prefix_name, str(runtime))
    script = root / "runtime-probe.cjs"
    script.write_text(
        "const fs=require('node:fs'),assert=require('node:assert/strict');"
        "assert.equal(fs.readFileSync('synthetic-python/Lib/late_import.py','utf8'),'SYNTHETIC_ORIGINAL');"
        "fs.writeFileSync('staged-module','replacement');"
        "for(const op of [()=>fs.writeFileSync('synthetic-python/Lib/late_import.py','bad'),"
        "()=>fs.writeFileSync('synthetic-python/Lib/new_import.py','bad'),"
        "()=>fs.renameSync('synthetic-python','renamed-python'),"
        "()=>fs.renameSync('staged-module','synthetic-python/Lib/late_import.py')]){"
        "assert.throws(op,e=>['EACCES','EPERM','EBUSY'].includes(e.code))}"
        "fs.writeFileSync('ordinary-output','still writable');console.log('HOST_RUNTIME_PROTECTED');"
    )
    runner = build_shell_runner_from_settings(settings, root)
    completed = runner.run(
        root=root,
        cwd=root,
        cmd=f"& '{node}' './runtime-probe.cjs'; exit $LASTEXITCODE",
        timeout_s=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "HOST_RUNTIME_PROTECTED" in completed.stdout
    assert original.read_text() == "SYNTHETIC_ORIGINAL"
    assert not (library / "new_import.py").exists()
    assert (root / "ordinary-output").read_text() == "still writable"
    moved = root / "renamed-python"
    runtime.rename(moved)
    moved.rename(runtime)


def test_native_extra_folder_cannot_write_future_desktop_runtime_generations(
    installed, monkeypatch
):
    root, settings, node = installed
    extra = root.parent / "approved-extra"
    runtime = extra / "cli-runtime"
    active = runtime / "active"
    active.mkdir(parents=True)
    canary = active / "canary.txt"
    canary.write_text("unchanged")
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(runtime)]))
    settings = replace(settings, additional_workspace_roots=(str(extra),))
    script = root / "runtime-generations.cjs"
    script.write_text(
        "const fs=require('node:fs'),p=require('node:path'),a=require('node:assert/strict');"
        "const extra=" + json.dumps(str(extra)) + ";const runtime=p.join(extra,'cli-runtime');"
        "a.equal(fs.readFileSync(p.join(runtime,'active/canary.txt'),'utf8'),'unchanged');"
        "for(const op of [()=>fs.writeFileSync(p.join(runtime,'active/canary.txt'),'bad'),"
        "()=>fs.mkdirSync(p.join(runtime,'future')),"
        "()=>fs.renameSync(runtime,p.join(extra,'renamed-runtime'))])"
        "a.throws(op,e=>['EACCES','EPERM','EBUSY'].includes(e.code));"
        "fs.writeFileSync(p.join(extra,'ordinary.txt'),'allowed');"
        "console.log('RUNTIME_GENERATIONS_PROTECTED');"
    )
    runner = build_shell_runner_from_settings(settings, root)
    result = runner.run(
        root=root,
        cwd=root,
        cmd=f"& '{node}' './runtime-generations.cjs'; exit $LASTEXITCODE",
        timeout_s=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RUNTIME_GENERATIONS_PROTECTED" in result.stdout
    assert canary.read_text() == "unchanged"
    assert not (runtime / "future").exists()
    assert (extra / "ordinary.txt").read_text() == "allowed"


def test_native_automatic_system_reads_exclude_windows_temp(installed):
    root, settings, _ = installed
    runner = build_shell_runner_from_settings(settings, root)
    marker = Path(os.environ["SystemRoot"]) / "Temp" / ("alysis-mxc-canary-" + uuid.uuid4().hex)
    nonce = uuid.uuid4().hex.encode()
    with marker.open("xb") as stream:
        stream.write(nonce)
    identity = (marker.stat().st_dev, marker.stat().st_ino)
    try:
        assert marker.read_bytes() == nonce  # Exact host positive control.
        _, encoded = runner._prepare(root=root, cwd=root, cmd="Write-Output probe")
        import json

        assert Path(os.environ["SystemRoot"]) not in map(Path, json.loads(encoded)["read"])
        command = (
            f"try {{ Get-Content -LiteralPath '{marker}' -Raw -ErrorAction Stop | Out-Null; exit 91 }} "
            "catch [System.UnauthorizedAccessException] { Write-Output 'WINDOWS_TEMP_DENIED' }"
        )
        result = runner.run(root=root, cwd=root, cmd=command, timeout_s=20)
        assert result.returncode == 0 and "WINDOWS_TEMP_DENIED" in result.stdout, result
        assert marker.read_bytes() == nonce
    finally:
        # Remove only this exact synthetic file, never the Temp directory.
        if (
            marker.stat().st_dev,
            marker.stat().st_ino,
        ) == identity and marker.read_bytes() == nonce:
            marker.unlink()


def test_native_engine_reservations_block_creation_rename_links_and_streams(installed):
    from alysis_code.sandbox_bookkeeping import capture_instruction_creations

    root, settings, node = installed
    (root / "AGENTS.md").unlink()
    (root / ".git").rmdir()
    (root / ".github/skills").rmdir()
    (root / ".github").rmdir()
    code = (
        "const fs=require('node:fs');fs.writeFileSync('ordinary-output','ok');"
        "fs.writeFileSync('staged-file','staged');fs.mkdirSync('staged-dir');"
        "const ops=[()=>fs.mkdirSync('.git'),()=>fs.mkdirSync('.GiT'),"
        "()=>fs.renameSync('staged-dir','.git'),()=>fs.writeFileSync('AGENTS.md','bad'),"
        "()=>fs.renameSync('staged-file','AGENTS.md'),()=>fs.linkSync('staged-file','.git/injected'),"
        "()=>fs.writeFileSync('AGENTS.md:payload','bad'),()=>fs.renameSync('staged-dir','.github/skills/injected')];"
        "for(const [i,op] of ops.entries()){try{op();throw Error('guard bypass '+i)}catch(e){"
        "if(!['EPERM','EACCES',...(i<2?['EEXIST']:[]),...([2,4,7].includes(i)?['EBUSY']:[])].includes(e.code))throw e;console.log(i+':'+e.code)}}"
    )
    (root / "reservations.cjs").write_text(code)
    runner = build_shell_runner_from_settings(settings, root)
    with capture_instruction_creations(root) as receipts:
        completed = runner.run(
            root=root,
            cwd=root,
            cmd=f"& '{node}' './reservations.cjs'; exit $LASTEXITCODE",
            timeout_s=15,
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert receipts == []
    assert not (root / "AGENTS.md").exists()
    assert (root / "CLAUDE.md").read_text() == "protected-original"
    assert (root / "ordinary-output").read_text() == "ok"
    assert not (root / ".git").exists()
    assert not (root / ".github").exists()


def test_native_engine_background_uses_same_helper_and_original_cleanup(installed):
    root, settings, _ = installed
    runner = build_background_shell_runner_from_settings(settings, root)
    started = runner.start(root=root, cwd=root, cmd="[Console]::Out.WriteLine('MXC_BACKGROUND_OK')")
    try:
        started.popen.wait(timeout=15)
        stdout = started.popen.stdout.read().decode()
        stderr = started.popen.stderr.read().decode()
        assert started.popen.returncode == 0, stderr
        assert "MXC_BACKGROUND_OK" in stdout
        assert started.started_argv[-1] == "mxc-run-request"
        assert started.termination_mode == "direct"
    finally:
        started.cleanup()


@pytest.mark.parametrize("access", ["workspace-write", "read-only"])
def test_native_empty_workspace_reservations_and_repeat_run(installed, access):
    from alysis_code.sandbox_bookkeeping import capture_instruction_creations

    fixture_root, settings, node = installed
    root = fixture_root.parent / "empty-workspace"
    root.mkdir()
    runner = build_shell_runner_from_settings(replace(settings, filesystem_access=access), root)
    if access == "read-only":
        code = (
            "try{require('node:fs').mkdirSync('.git');throw Error('readonly bypass')}"
            "catch(e){if(!['EPERM','EACCES'].includes(e.code))throw e}"
        )
    else:
        code = (
            "require('node:fs').writeFileSync('ordinary-output','ok');"
            "try{require('node:fs').writeFileSync('.github/skills/injected','bad');"
            "throw Error('metadata bypass')}catch(e){if(!['EPERM','EACCES'].includes(e.code))throw e}"
        )
    code += ";console.log('EMPTY_WORKSPACE_OK')"
    escaped = code.replace("'", "''")
    for _ in range(2):
        with capture_instruction_creations(root) as receipts:
            completed = runner.run(
                root=root,
                cwd=root,
                cmd=f"& '{node}' -e '{escaped}'; exit $LASTEXITCODE",
                timeout_s=15,
            )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "EMPTY_WORKSPACE_OK" in completed.stdout
        assert receipts == []
        if access == "read-only":
            assert list(root.iterdir()) == []
        else:
            assert all(not (root / name).exists() for name in native._PROTECTED_FILES)
            assert all(not (root / name).exists() for name in native._PROTECTED_DIRS)
            assert (root / "ordinary-output").read_text() == "ok"
            assert {p.name for p in root.iterdir()} == {"ordinary-output"}


def test_native_engine_readonly_mapping_is_enforced(installed):
    root, settings, node = installed
    runner = build_shell_runner_from_settings(
        replace(settings, filesystem_access="read-only"), root
    )
    code = "try{require('node:fs').writeFileSync('forbidden.txt','bad');process.exitCode=1}catch(e){if(!['EACCES','EPERM'].includes(e.code))process.exitCode=2;console.log(e.code)}"
    escaped = code.replace("'", "''")
    completed = runner.run(
        root=root, cwd=root, cmd=f"& '{node}' -e '{escaped}'; exit $LASTEXITCODE", timeout_s=15
    )
    assert completed.returncode == 0, completed.stderr
    assert "EPERM" in completed.stdout or "EACCES" in completed.stdout
    assert not (root / "forbidden.txt").exists()


def test_native_invalid_environment_has_no_persistent_preparation(installed, monkeypatch):
    fixture_root, settings, _ = installed
    root = fixture_root.parent / "invalid-environment"
    root.mkdir()
    runner = build_shell_runner_from_settings(settings, root)
    monkeypatch.delenv("LOCALAPPDATA")
    with pytest.raises(ConfigError, match="LOCALAPPDATA"):
        runner.run(root=root, cwd=root, cmd="throw 'MUST_NOT_RUN'", timeout_s=10)
    assert list(root.iterdir()) == []


def test_native_engine_concurrent_commands_have_independent_jobs(installed):
    root, settings, _ = installed
    runner = build_shell_runner_from_settings(settings, root)

    def run(index):
        return runner.run(
            root=root,
            cwd=root,
            cmd=f"Start-Sleep -Milliseconds 250; Set-Content './parallel-{index}' 'ok'",
            timeout_s=15,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    assert all((root / f"parallel-{i}").read_text().strip() == "ok" for i in range(2))
