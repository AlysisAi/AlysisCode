"""Fixed, contained startup observations after an installed smoke failure.

Uses the real request policy and lifecycle. Only the diagnostic PowerShell
source differs. No permissions, production deadlines or commands are changed.
Failed workspaces are retained; pending recovery prevents further launches.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alysis_code import windows_sandbox as native  # noqa: E402
from alysis_code.config import ConfigError  # noqa: E402
from alysis_code.sandbox_settings import ShellSandboxSettings  # noqa: E402

ENTRY = "ALYSIS_DIAGNOSTIC_POWERSHELL_ENTRY"
DONE = "ALYSIS_DIAGNOSTIC_BOOTSTRAP_DONE"


def stack_observer() -> Path | None:
    """Build the host-only observer in a disposable directory, only after failure."""
    if not shutil.which("dotnet"):
        return None
    project = Path(tempfile.mkdtemp(prefix="alysis-stack-observer-"))
    source = Path(__file__).with_name("windows_sandbox_stacks")
    for name in ("Stacks.csproj", "Program.cs"):
        shutil.copyfile(source / name, project / name)
    try:
        result = subprocess.run(
            ["dotnet", "build", str(project / "Stacks.csproj"), "-o", str(project / "out")],
            capture_output=True,
            timeout=90,
        )
        if result.returncode == 0:
            return project / "out/Stacks.dll"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def probe_lines(output: str | bytes) -> list[str]:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return [line[:160] for line in output.splitlines() if line.startswith("ALYSIS_CLR_")][:24]


def thread_probe(report: dict[str, object]) -> Path | None:
    directory = Path(tempfile.mkdtemp(prefix="alysis-clr-thread-probe-"))
    executable = directory / "ThreadProbe.exe"
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    source = Path(__file__).with_name("windows_sandbox_stacks") / "ThreadProbe.cs"
    try:
        build = subprocess.run(
            [str(compiler), "/nologo", "/platform:x64", f"/out:{executable}", str(source)],
            capture_output=True,
            timeout=30,
        )
        if build.returncode != 0:
            report.update(
                category="compile_failed",
                returncode=build.returncode,
                compiler_codes=sorted(
                    {
                        code.decode("ascii")
                        for code in re.findall(rb"\bCS\d{4}\b", build.stdout + build.stderr)
                    }
                ),
            )
            return None
        control = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
        report.update(
            category="host_control_failed",
            returncode=control.returncode,
            runtime_probe_lines=probe_lines(control.stdout),
        )
        expected = [
            "ENTRY",
            "EVENT",
            "SEMAPHORE",
            "THREAD",
            "COM_MTA",
            "POOL_QUEUE",
            "POOL_COMPLETED",
            "SELF_WORKER_ACCESS",
            "NATIVE_POOL_QUEUE",
            "NATIVE_POOL_COMPLETED",
        ]
        if control.returncode == 0 and all(
            f"ALYSIS_CLR_{name}=True" in control.stdout.splitlines() for name in expected
        ):
            report["category"] = "passed"
            return executable
    except subprocess.TimeoutExpired as error:
        report.update(
            category="host_control_timeout", runtime_probe_lines=probe_lines(error.output or "")
        )
    except (OSError, subprocess.SubprocessError) as error:
        report.update(category="unavailable", exception_type=type(error).__name__)
    return None


def main() -> None:
    report: dict[str, object] = {"schema": 1, "diagnostic_only": True, "cases": []}
    if os.name != "nt" or os.environ.get("ALYSIS_RUN_INSTALLED_SANDBOX_TESTS") != "1":
        report["category"] = "requires_installed_windows_test_environment"
        print(json.dumps(report))
        return
    journal = Path(os.environ["LOCALAPPDATA"]) / "alysis-sandbox/execution-pending.json"
    original_builder = native.build_request
    original_popen = subprocess.Popen
    observer = stack_observer()
    probe_control: dict[str, object] = {}
    probe = thread_probe(probe_control)
    report["thread_probe_host_control"] = probe_control
    report["stack_observer_available"] = observer is not None
    report["thread_probe_host_control_passed"] = probe is not None
    cases = []
    for kind in ("powershell_entry", "production_bootstrap", "clr_threads"):
        if kind == "clr_threads" and probe is None:
            continue
        if journal.exists():
            report["category"] = "recovery_pending_no_further_launch"
            break
        root = Path(tempfile.mkdtemp(prefix="alysis-startup-observation-"))

        def request(*args, _kind=kind, **kwargs):
            payload = original_builder(*args, **kwargs)
            if _kind == "clr_threads":
                payload["argv"] = [str(probe)]
                return payload
            if payload["argv"][-2] != "-Command":
                raise ValueError("unexpected diagnostic shell argument layout")
            source = f"[Console]::Out.WriteLine('{ENTRY}'); "
            if _kind == "production_bootstrap":
                source += payload["argv"][-1]
            payload["argv"][-1] = source
            return payload

        record: dict[str, object] = {"query": kind, "timeout_seconds": 30}
        output: str | bytes = ""
        stderr: str | bytes = ""
        observations = []
        threads = []
        finished = threading.Event()

        def observe(process, finished=finished, observations=observations):
            if finished.wait(5) or process.poll() is not None:
                return
            try:
                with original_popen(
                    ["dotnet", str(observer), str(process.pid)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ) as reader:
                    try:
                        stdout, _ = reader.communicate(timeout=15)
                        observations.append(json.loads(stdout))
                    except (subprocess.SubprocessError, ValueError):
                        reader.kill()
                        reader.communicate(timeout=5)
                        observations.append({"category": "observation_failed"})
            except OSError:
                observations.append({"category": "observer_launch_failed"})

        def popen(argv, *args, _observe=observe, _threads=threads, **kwargs):
            process = original_popen(argv, *args, **kwargs)
            if observer is not None and argv == [
                os.environ["ALYSIS_WINDOWS_SANDBOX_HELPER"],
                "run-request",
            ]:
                thread = threading.Thread(target=_observe, args=(process,), daemon=True)
                _threads.append(thread)
                thread.start()
            return process

        try:
            with (
                patch.object(native, "build_request", request),
                patch.object(subprocess, "Popen", popen),
            ):
                result = native.WindowsNativeRunner(
                    ShellSandboxSettings(
                        backend="windows-native",
                        network="off",
                        windows_read_roots=(str(probe.parent),) if kind == "clr_threads" else (),
                    )
                ).run(root=root, cwd=root, cmd=f"[Console]::Out.WriteLine('{DONE}')", timeout_s=30)
            output = result.stdout
            stderr = result.stderr
            record.update(category="completed", returncode=result.returncode)
        except subprocess.TimeoutExpired as error:
            output = error.output or ""
            stderr = error.stderr or ""
            record["category"] = "timeout"
        except (ConfigError, OSError, subprocess.SubprocessError):
            record["category"] = "launch_or_cleanup_failed"
        finally:
            finished.set()
            for thread in threads:
                thread.join(timeout=20)
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        record["helper_launch_marker_observed"] = "srt-win: run_lockdown: caller_in_job=" in stderr
        record["child_resumed_marker_observed"] = "assigned+resumed" in stderr
        record["stdout_length"] = len(output)
        record["stderr_length"] = len(stderr)
        record["managed_stacks"] = observations
        if kind == "clr_threads":
            record["runtime_probe_lines"] = probe_lines(output)
        record["powershell_entry_reached"] = ENTRY in output.splitlines()
        record["bootstrap_completed"] = DONE in output.splitlines()
        record["recovery_pending"] = journal.exists()
        record["workspace_retained"] = True
        cases.append(record)
    report["cases"] = cases
    encoded = json.dumps(report, indent=2) + "\n"
    destination = (
        Path(__file__).resolve().parents[1]
        / "native/windows-sandbox/target/startup-diagnostics.json"
    )
    destination.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
