"""Real CLI -> local mock provider -> shell tool -> installed native helper.

No external provider, credentials, or internet are used. This deliberately keeps
the model transport in the trusted engine while checking the actual tool child.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.qa import mock_llm

pytestmark = pytest.mark.skipif(
    platform.system() != "Windows" or os.environ.get("ALYSIS_RUN_INSTALLED_SANDBOX_TESTS") != "1",
    reason="requires explicitly provisioned Windows sandbox test VM",
)


@pytest.mark.parametrize("access", ["read-only", "workspace-write"])
def test_real_agent_shell_tool_enters_appcontainer(tmp_path, monkeypatch, access):
    root = tmp_path / "workspace"
    root.mkdir()
    # A basetemp inside the engine checkout must not make CLI repository
    # discovery select that ancestor (including its Cargo hardlinks). Give the
    # synthetic workspace its own real Git boundary without user templates.
    subprocess.run(
        ["git", "-c", "init.templateDir=", "init", "--quiet", str(root)],
        check=True,
        capture_output=True,
        timeout=15,
    )
    (root / "README.md").write_text("# Native agent diagnostic\n", encoding="utf-8")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    original = mock_llm._tool_response
    command = r"""
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public static class AgentTokenProbe {
 [DllImport("advapi32.dll", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr p, uint a, out IntPtr t);
 [DllImport("advapi32.dll", SetLastError=true)] public static extern bool GetTokenInformation(IntPtr t, int c, out int v, int n, out int r);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
}' -ErrorAction Stop;
$t=[IntPtr]::Zero; $v=0; $r=0;
if (-not [AgentTokenProbe]::OpenProcessToken([IntPtr](-1), 8, [ref]$t)) { throw 'token open failed' };
try {
 if (-not [AgentTokenProbe]::GetTokenInformation($t, 29, [ref]$v, 4, [ref]$r)) { throw 'token query failed' };
 if ($v -ne 1) { throw 'tool executed outside AppContainer' };
 'NATIVE_AGENT_APPCONTAINER_CONFIRMED'
} finally { [AgentTokenProbe]::CloseHandle($t) | Out-Null }
"""
    expected_root = str(root).replace("'", "''")
    command = (
        f"if ($PWD.ProviderPath.TrimEnd('\\') -ne '{expected_root}') "
        '{ throw "wrong synthetic workspace: $($PWD.ProviderPath)" }; ' + command
    )

    def tool_response(name, arguments, **kwargs):
        if name == "shell_run":
            arguments = {"cmd": command}
        return original(name, arguments, **kwargs)

    monkeypatch.setattr(mock_llm, "_tool_response", tool_response)
    with mock_llm.MockLLMServer() as server:
        config = {
            "base_url": server.base_url,
            "model": "qa-mock-model",
            "default_mode": "auto",
            "stream": False,
            "routing_mode": "code_only",
            "subagents_enabled": False,
            "skills_enabled": False,
            "update_check_enabled": False,
            "verify_commands": ["Write-Output 'observation-only'"],
            "profiles": {
                "mock": {
                    "name": "mock",
                    "protocol": "openai_compat",
                    "base_url": server.base_url,
                    "api_key_env": "ALYSIS_API_KEY",
                    "default_model": "qa-mock-model",
                }
            },
            "active_profile": "mock",
            "default_workspace_path": str(root),
        }
        (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
        # Explicit isolated state and dummy provider keys. No user credential
        # profile is loaded or transmitted to this loopback-only test server.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("ALYSIS_", "SYLLIPTOR_", "OPENAI_", "ANTHROPIC_"))
        }
        for key in ("ALYSIS_WINDOWS_SANDBOX_HELPER", "ALYSIS_WINDOWS_SANDBOX_SHA256"):
            env[key] = os.environ[key]
        env.update(
            {
                "ALYSIS_CONFIG_DIR": str(config_dir),
                "ALYSIS_DATA_DIR": str(tmp_path / "data"),
                "ALYSIS_API_KEY": "local-test-placeholder",
                "ALYSIS_SHELL_SANDBOX_BACKEND": "windows-native",
                "ALYSIS_SHELL_SANDBOX_MODE": "strict",
                "ALYSIS_VERIFY_SANDBOX_MODE": "strict",
                "ALYSIS_SHELL_SANDBOX_NETWORK": "off",
                "ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS": access,
                "ALYSIS_TUI": "0",
                "ALYSIS_UPDATE_CHECK_ENABLED": "0",
                "ALYSIS_SKILLS_ENABLED": "0",
                "ALYSIS_CONTEXT_WINDOW": "128000",
                "ALYSIS_MAX_OUTPUT_TOKENS": "4096",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PYTHONUTF8": "1",
                "NO_COLOR": "1",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alysis_code",
                "run",
                "Run the shell diagnostic and report its result. This is observation only, not an implementation task.",
                "--path",
                str(root),
                "--mode",
                "auto",
                "--yes",
                "--no-stream",
                "--no-subagents",
                "--max-steps",
                "4",
                "--no-log",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
            check=False,
        )
        tool_results = [
            message.get("content", "")
            for request in server.requests
            for message in request.get("messages", [])
            if message.get("role") == "tool"
        ]
    assert result.returncode == 0, result.stdout + result.stderr
    # The serialized result also echoes cmd/effective_cmd, which contain the
    # marker even when execution fails. Only successful child stdout is proof.
    parsed_results = []
    for content in tool_results:
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            parsed_results.append(payload)
    assert any(
        type(payload.get("exit_code")) is int
        and payload["exit_code"] == 0
        and isinstance(payload.get("stdout"), str)
        and payload["stdout"].strip() == "NATIVE_AGENT_APPCONTAINER_CONFIRMED"
        for payload in parsed_results
    ), json.dumps(parsed_results, indent=2) + "\n" + result.stdout + "\n" + result.stderr
    assert not (
        Path(os.environ["LOCALAPPDATA"]) / "alysis-sandbox" / "execution-pending.json"
    ).exists()
