"""Read-only, post-failure PowerShell startup controls for disposable Windows CI.

Each fixed query runs once, for at most 30 seconds. Reports never contain raw
stdout, stderr, exception text, environment values or user-selected commands.
This does not change the production ten-second deadline or any policy setting.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alysis_code import windows_sandbox as native  # noqa: E402

TIMEOUT_SECONDS = 30
STARTUP_MARKER = "ALYSIS_POLICY_STARTUP_OK"
STARTUP_QUERY = f"$ErrorActionPreference='Stop'; [Console]::Out.Write('{STARTUP_MARKER}')"
UNQUALIFIED_POLICY_QUERY = (
    "$ErrorActionPreference='Stop'; [Console]::Out.Write((Get-ExecutionPolicy).ToString())"
)


def fixed_shell() -> Path:
    """Use the operating system directory, never PATH or a caller executable."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_directory = kernel.GetSystemDirectoryW
    get_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_directory(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def minimal_environment(shell: Path, inherited: dict[str, str]) -> dict[str, str]:
    # Kept byte-for-byte equivalent to the production query's environment.
    # The focused parity test compares both real call arguments to detect drift.
    system_root = shell.parents[3]
    environment = {
        "SYSTEMROOT": str(system_root),
        "WINDIR": str(system_root),
        "PATH": os.pathsep.join((str(shell.parent), str(system_root / "System32"))),
        "PSModulePath": str(shell.parent / "Modules"),
    }
    # dict(os.environ) loses Windows' case-insensitive mapping behavior and
    # normally exposes uppercase keys, unlike os.environ.get in production.
    preference = next(
        (value for key, value in inherited.items() if key.upper() == "PSEXECUTIONPOLICYPREFERENCE"),
        None,
    )
    if preference is not None:
        if preference not in native._EXECUTION_POLICIES | {"Undefined"}:
            raise ValueError("invalid_host_policy_preference")
        environment["PSExecutionPolicyPreference"] = preference
    return environment


def query(shell: Path, environment: dict[str, str], kind: str) -> dict[str, object]:
    if kind not in {"startup", "policy", "unqualified_policy"}:
        raise ValueError("unknown_diagnostic_kind")
    command = {
        "startup": STARTUP_QUERY,
        "policy": native._EXECUTION_POLICY_QUERY,
        "unqualified_policy": UNQUALIFIED_POLICY_QUERY,
    }[kind]
    expected = {STARTUP_MARKER} if kind == "startup" else native._EXECUTION_POLICIES
    started = time.monotonic()
    record: dict[str, object] = {"query": kind, "timeout_seconds": TIMEOUT_SECONDS}
    try:
        result = subprocess.run(
            [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=shell.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        record["returncode"] = result.returncode
        if result.returncode:
            record["category"] = "nonzero_exit"
        elif result.stderr.strip():
            record["category"] = "stderr_present"
        else:
            value = result.stdout.decode("ascii").strip()
            if value in expected:
                record.update(category="success", value=value)
            else:
                record["category"] = "unexpected_stdout"
    except subprocess.TimeoutExpired:
        record["category"] = "timeout"
    except OSError as error:
        record["category"] = "start_failed"
        for name in ("winerror", "errno"):
            code = getattr(error, name, None)
            if isinstance(code, int):
                record[name] = code
    except (UnicodeError, ValueError):
        record["category"] = "invalid_response"
    except subprocess.SubprocessError:
        record["category"] = "process_failed"
    record["duration_seconds"] = round(time.monotonic() - started, 3)
    return record


def diagnose(shell: Path, inherited: dict[str, str]) -> dict[str, object]:
    try:
        minimal = minimal_environment(shell, inherited)
    except ValueError:
        return {"schema": 1, "category": "invalid_host_policy_preference", "cases": []}
    cases = []
    # No retries and no pre-test warming. These controls run only after the real
    # boundary test has failed; their outcomes cannot make that test successful.
    for label, environment in (("minimal", minimal), ("inherited", inherited)):
        for kind in ("startup", "policy", "unqualified_policy"):
            case = query(shell, environment, kind)
            case["environment"] = label
            cases.append(case)
    return {"schema": 1, "cases": cases}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        report = {"schema": 1, "category": "unsupported_platform", "cases": []}
    else:
        try:
            shell = fixed_shell()
        except OSError as error:
            report = {"schema": 1, "category": "system_directory_unavailable", "cases": []}
            code = getattr(error, "winerror", None)
            if isinstance(code, int):
                report["winerror"] = code
        else:
            report = diagnose(shell, dict(os.environ))
    encoded = json.dumps(report, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
