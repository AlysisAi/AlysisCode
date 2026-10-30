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
    program_data.mkdir()
    canary = program_data / "canary"
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
        "assert Path(tempfile.gettempdir()).resolve()==root\n"
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
    assert not list(workspace.glob("tmp*"))


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
