"""Bounded metadata-only reparse attacks after native PSEC activation."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def qualify(helper: Path, node: Path, work: Path, request, record):
    source = Path(__file__).resolve().parents[1] / "tests/fixtures/windows_mxc_reparse.py"
    fixture = work / "reparse-probe.py"
    shutil.copyfile(source, fixture)
    spec = importlib.util.spec_from_file_location("mxc_reparse_probe", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    python = (Path(sys.base_prefix) / "python.exe").resolve(strict=True)
    outside = work.parent / "reparse-outside"
    outside.mkdir()
    (outside / "canary").write_text("original")

    def payload(argv, protected):
        value = request(argv)
        value["read"].append(str(python.parent))
        value["deny_read"].append(str(outside))
        value["deny_write"].append(str(protected))
        return value

    def run(name, argv, value=None):
        process = subprocess.run(
            [str(a) for a in argv],
            cwd=work,
            capture_output=True,
            input=None if value is None else json.dumps(value).encode(),
            timeout=20,
        )
        event = {
            "name": name,
            "exit_code": process.returncode,
            "stdout": process.stdout.decode(errors="replace"),
            "stderr": process.stderr.decode(errors="replace"),
        }
        record(event)
        assert process.returncode == 0, event

    control = work.parent / "host-metadata-control"
    control.mkdir()
    control_target = work.parent / "host-metadata-target"
    control_target.mkdir()
    run(
        "host_metadata_reparse_control",
        [python, "-I", "-S", "-B", fixture, control, control_target, "allow"],
    )
    for kind in ["outside", "inside"]:
        protected = work / (".protected-" + kind)
        protected.mkdir()
        target = outside if kind == "outside" else work / "ordinary-target"
        target.mkdir(exist_ok=True)
        argv = [python, "-I", "-S", "-B", fixture, protected, target, "deny"]
        run(
            "contained_metadata_reparse_" + kind,
            [helper, "mxc-run-request"],
            payload(argv, protected),
        )
        assert not os.path.isjunction(protected)

    for kind in ["outside", "inside"]:
        ancestor = work / ("writable-ancestor-" + kind)
        ancestor.mkdir()
        protected = ancestor / "protected"
        protected.write_text("protected-original")
        target = outside if kind == "outside" else work / "ancestor-target"
        target.mkdir(exist_ok=True)
        (target / "protected").write_text("target-original")
        argv = [python, "-I", "-S", "-B", fixture, ancestor, target, "deny-ancestor"]
        run(
            "contained_writable_ancestor_reparse_" + kind,
            [helper, "mxc-run-request"],
            payload(argv, protected),
        )
        assert not os.path.isjunction(ancestor)
        assert protected.read_text() == "protected-original"
        assert (target / "protected").read_text() == "target-original"
        assert not (ancestor / "stolen").exists()

    # Metadata-only host mutation can evade sharing locks; it must not grant
    # outside writes, and final guard verification must reject the changed path.
    protected = work / ".host-raced"
    protected.mkdir()
    ready = work / "reparse-ready"
    release = work / "reparse-release"
    code = """const fs=require('node:fs');fs.writeFileSync('reparse-ready','1');
    const timer=setInterval(()=>{if(!fs.existsSync('reparse-release'))return;clearInterval(timer);
    try{fs.writeFileSync('.host-raced/injected','bad');console.log('BYPASS');process.exitCode=1}
    catch(e){console.log(e.code);process.exitCode=['EPERM','EACCES'].includes(e.code)?0:2}},20);"""
    process = subprocess.Popen(
        [str(helper), "mxc-run-request"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=work,
    )
    try:
        process.stdin.write(json.dumps(payload([node, "-e", code], protected)).encode())
        process.stdin.close()
        process.stdin = None
        deadline = time.monotonic() + 8
        while not ready.exists():
            assert process.poll() is None and time.monotonic() < deadline, "no live checkpoint"
            time.sleep(0.02)
        mutation = module.set_junction(protected, outside)
        assert mutation["error"] == 0, mutation
        release.write_text("1")
        stdout, stderr = process.communicate(timeout=18)
        event = {
            "name": "host_metadata_reparse_after_activation",
            "host_mutation": mutation,
            "exit_code": process.returncode,
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
        }
        record(event)
        assert process.returncode == 125 and stdout.strip() in (b"EPERM", b"EACCES"), event
        assert not (outside / "injected").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    assert (outside / "canary").read_text() == "original"
