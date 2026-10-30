"""Fresh-process CLI parity; no real vault, network, provider or desktop activity."""

from __future__ import annotations

import json
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cancellation probes")
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/auth_status_startup"
ARGS = ["auth", "status", "openai-codex", "--json"]
HEAVY = (
    "alysis_code.agent",
    "alysis_code.agent_loop",
    "alysis_code.cli_impl.forge",
    "alysis_code.cli_impl.commands.forge",
    "alysis_code.forge_host",
    "alysis_code.forge_host_cli",
    "alysis_code.forge_host_client",
    "alysis_code.ide.stdio_bridge",
    "alysis_code.assets",
    "alysis_code.conflict_auto_resolver",
    "alysis_code.cli_impl.commands.cli_surface",
)


def heavy_modules(audit):
    return [
        name
        for name in audit["modules"]
        if any(name == p or name.startswith(p + ".") for p in HEAVY)
    ]


def environment(root, case="valid", extra=None):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("home", "config", "data", "tmp"):
        (root / name).mkdir(exist_ok=True)
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "PYTHONUSERBASE")
        if key in os.environ
    }
    env.update(
        {
            "HOME": str(root / "home"),
            "TMPDIR": str(root / "tmp"),
            "ALYSIS_CONFIG_DIR": str(root / "config"),
            "ALYSIS_DATA_DIR": str(root / "data"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "PYTHONPATH": os.pathsep.join([str(FIXTURE), str(ROOT / "src"), *sys.path]),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
            "ALYSIS_OAUTH_KEY_SOURCE": "filesystem-random",
            "PYTHONDONTWRITEBYTECODE": "1",
            "W5_AUTH_CASE": case,
            "W5_AUTH_AUDIT": str(root / "audit.json"),
            "NO_COLOR": "1",
            "COLUMNS": "120",
        }
    )
    env.update(extra or {})
    return env


def command(entry, args, *, repeat=False):
    if repeat:
        code = "import sys; from alysis_code.cli import app; "
        code += "app(args=sys.argv[1:], prog_name='alysis-code', standalone_mode=False); " * 2
        return [sys.executable, "-c", code, *args]
    if entry in {"alysis_code", "alysis_code.cli"}:
        return [sys.executable, "-m", entry, *args]
    return [sys.executable, str(entry), *args]


def run(
    root,
    *,
    args=ARGS,
    case="valid",
    full=False,
    entry="alysis_code.cli",
    extra=None,
    repeat=False,
    stop=None,
):
    env = environment(root, case, extra)
    if full:
        env["W5_AUTH_FULL"] = "1"
    audit_path = Path(env["W5_AUTH_AUDIT"])
    audit_path.unlink(missing_ok=True)
    audit_path.with_suffix(".ready").unlink(missing_ok=True)
    started = time.perf_counter()
    process = subprocess.Popen(
        command(entry, args, repeat=repeat),
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        if stop is not None:
            ready = Path(env["W5_AUTH_AUDIT"]).with_suffix(".ready")
            deadline = time.monotonic() + 15
            while not ready.exists():
                assert process.poll() is None, process.communicate()
                assert time.monotonic() < deadline, "child did not reach the scripted vault"
                time.sleep(0.01)
            os.killpg(process.pid, stop)
        out, err = process.communicate(timeout=45)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    elapsed = time.perf_counter() - started
    audit = json.loads(audit_path.read_text()) if audit_path.exists() else None
    assert "Error in sitecustomize" not in err, err
    return process.returncode, out, err, audit, elapsed


def test_status_dispatch_excludes_heavy_import_graph(tmp_path):
    code, out, err, audit, _ = run(tmp_path)
    assert code == 0 and not err
    assert json.loads(out)["authenticated"] is True
    assert heavy_modules(audit) == []
    assert audit["events"] == [
        ["config_read"],
        ["vault_read", "provider_auth_tokens.json"],
        ["keyring", False],
    ]


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "missing",
        "malformed",
        "unreadable",
        "refresh_failure",
        "revoked",
        "timeout",
        "removed_during_refresh",
    ],
)
def test_status_output_and_fresh_reads_match_full_cli(tmp_path, case):
    full = run(tmp_path, full=True, case=case)
    thin = run(tmp_path, case=case)
    assert thin[:3] == full[:3]
    assert thin[3]["events"] == full[3]["events"]
    assert full[0] == 0 and json.loads(full[1])["authenticated"] is (case == "valid")
    reads = sum(event[0] == "vault_read" for event in thin[3]["events"])
    assert reads == (
        3
        if case == "revoked"
        else 2
        if case in {"refresh_failure", "timeout", "removed_during_refresh"}
        else 1
    )
    assert heavy_modules(full[3]) and not heavy_modules(thin[3])


@pytest.mark.parametrize(
    "entry", ["alysis_code", "alysis_code.cli", "alysis-code", "alysis", "path alias", "managed"]
)
def test_status_default_and_aliased_entrypoints(tmp_path, entry):
    if entry == "managed":
        entry = ROOT / "scripts/release/managed_cli_entry.py"
    elif entry not in {"alysis_code", "alysis_code.cli"}:
        launcher = tmp_path / "launcher.py"
        launcher.write_text("from alysis_code.cli import app\napp()\n")
        alias = tmp_path / entry
        alias.symlink_to(launcher)
        entry = alias
    full = run(tmp_path, entry=entry, full=True)
    code, out, err, audit, _ = run(tmp_path, entry=entry)
    assert (code, out, err) == full[:3]
    assert audit["events"] == full[3]["events"]
    assert code == 0 and not err
    assert json.loads(out)["authenticated"] is True
    assert not heavy_modules(audit)


@pytest.mark.parametrize(
    "args",
    [
        ["auth", "list", "--json"],
        ["auth", "status", "openai-codex"],
        ["auth", "status", "--json"],
        ["auth", "status", "--json", "openai-codex"],
        ["auth", "status", "openai-codex", "--json", "--help"],
        ["auth", "nonsense"],
        ["--version"],
        ["--help"],
        ["auth", "status", "--json", "--bad-option"],
    ],
)
def test_other_invocations_still_use_full_cli(tmp_path, args):
    result = run(tmp_path, args=args, entry="alysis_code")
    assert heavy_modules(result[3])
    assert result[0] == (2 if "nonsense" in args or "--bad-option" in args else 0)


def test_unsupported_provider_errors_match_full_cli(tmp_path):
    args = ["auth", "status", "not-a-provider", "--json"]
    full, thin = run(tmp_path, args=args, full=True), run(tmp_path, args=args)
    assert thin[:3] == full[:3]
    assert json.loads(thin[1])["authenticated"] is False
    assert json.loads(thin[1])["error"]
    assert thin[3]["events"] == full[3]["events"]


@pytest.mark.parametrize("case", ["changed_between_calls", "revoked_between_calls"])
def test_each_call_rechecks_changed_vault_without_caching(tmp_path, case):
    full = run(tmp_path, case=case, full=True, repeat=True)
    thin = run(tmp_path, case=case, repeat=True)
    assert thin[:3] == full[:3] and thin[3]["events"] == full[3]["events"]
    assert [json.loads(line)["authenticated"] for line in thin[1].splitlines()] == [True, False]
    assert thin[3]["events"].count(["config_read"]) == 2
    assert thin[3]["events"].count(["vault_read", "provider_auth_tokens.json"]) == (
        4 if case == "revoked_between_calls" else 2
    )


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_stop_during_vault_read_matches_full_cli(tmp_path, signum):
    full = run(tmp_path / "full", case="block", full=True, stop=signum)
    thin = run(tmp_path / "thin", case="block", stop=signum)
    assert thin[:3] == full[:3]
    assert thin[0] != 0 and not thin[1]


def test_interrupt_during_refresh_matches_full_cli(tmp_path):
    full = run(tmp_path, case="interrupt", full=True)
    thin = run(tmp_path, case="interrupt")
    assert thin[:3] == full[:3]
    assert thin[0] == 130 and not thin[1]
    assert thin[3]["events"] == full[3]["events"]


@pytest.mark.parametrize("entry", ["alysis_code", "alysis_code.cli"])
@pytest.mark.parametrize("bundle", ["valid", "missing", "malformed", "explicit_child_bundles"])
def test_tls_and_stdio_environment_match_full_cli(tmp_path, entry, bundle):
    base = tmp_path / "base.pem"
    base.write_bytes(b"-----BEGIN CERTIFICATE-----\nBASE\n-----END CERTIFICATE-----\n")
    extra = tmp_path / "extra.pem"
    if bundle == "malformed":
        extra.write_bytes(b"invalid certificate")
    elif bundle != "missing":
        extra.write_bytes(b"-----BEGIN CERTIFICATE-----\nEXTRA\n-----END CERTIFICATE-----\n")
    env = {
        "SSL_CERT_FILE": str(base),
        "ALYSIS_EXTRA_CA_CERTS": str(extra),
        "PYTHONIOENCODING": "ascii:strict",
    }
    if bundle == "explicit_child_bundles":
        env.update(
            {
                "REQUESTS_CA_BUNDLE": "/synthetic/requests.pem",
                "CURL_CA_BUNDLE": "/synthetic/curl.pem",
            }
        )
    full = run(tmp_path, entry=entry, full=True, extra=env)
    thin = run(tmp_path, entry=entry, extra=env)
    assert thin[:3] == full[:3] and thin[0] == 0
    assert thin[3]["tls"] == full[3]["tls"]
    assert thin[3]["stdio_errors"] == full[3]["stdio_errors"] == ["backslashreplace"] * 2
    assert thin[3]["events"] == full[3]["events"]
    if bundle in {"missing", "malformed"}:
        assert "ALYSIS_EXTRA_CA_CERTS ignored:" in thin[2]
        assert thin[3]["tls"]["SSL_CERT_FILE"] == str(base)
    else:
        assert not thin[2]
        # Preserve the existing canonical-facade import on the .cli module
        # entry (which applies TLS a second time), without importing the graph.
        merged = Path(thin[3]["tls"]["SSL_CERT_FILE"]).read_bytes()
        assert merged.count(b"EXTRA") == (2 if entry == "alysis_code.cli" else 1)
        assert merged.startswith(base.read_bytes())
        if bundle == "explicit_child_bundles":
            for key in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
                assert thin[3]["tls"][key] == env[key]
    assert not heavy_modules(thin[3])


@pytest.mark.parametrize("required", ["1", "0"])
def test_clean_build_environment_check_is_preserved_before_vault_access(tmp_path, required):
    env = {"ALYSIS_REQUIRE_CLEAN_BUILD": required}
    full = run(tmp_path, full=True, extra=env)
    thin = run(tmp_path, extra=env)
    assert thin[:3] == full[:3]
    assert thin[3]["events"] == full[3]["events"]
    if required == "1":
        assert thin[0] == 2 and not thin[1]
        assert thin[2].startswith("alysis: ")
        assert thin[3]["events"] == []
    else:
        assert thin[0] == 0 and json.loads(thin[1])["authenticated"] is True


def test_explicit_provider_argument_and_config_alias_are_preserved(tmp_path):
    real_config = tmp_path / "real config"
    real_config.mkdir()
    # Even a delegated default still uses native status for the explicitly
    # requested provider; the production callback owns this distinction.
    (real_config / "config.json").write_text(
        json.dumps(
            {
                "execution": {"backend": "delegated", "runtime": "openai-codex"},
                "agent_runtimes": {
                    "openai-codex": {"adapter": "codex-cli", "executable": "fixture-codex"},
                },
            }
        )
    )
    alias = tmp_path / "config alias"
    alias.symlink_to(real_config, target_is_directory=True)
    env = {"ALYSIS_CONFIG_DIR": str(alias)}
    args = ["auth", "status", " openai-codex ", "--json"]
    full = run(tmp_path, full=True, extra=env, args=args)
    thin = run(tmp_path, extra=env, args=args)
    assert thin[:3] == full[:3] and thin[0] == 0
    assert json.loads(thin[1])["authenticated"] is True
    assert thin[3]["events"] == full[3]["events"]
    assert not heavy_modules(thin[3])


@pytest.mark.parametrize("config_text", ["{invalid", "[]"])
def test_config_errors_match_full_cli_without_reading_vault(tmp_path, config_text):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(config_text)
    full = run(tmp_path, full=True)
    thin = run(tmp_path)
    assert thin[:3] == full[:3]
    assert thin[0] == 1 and not thin[1] and "ConfigError:" in thin[2]
    assert thin[3]["events"] == full[3]["events"] == [["config_read"]]


def test_whole_command_timing_with_scripted_vault(tmp_path):
    measured = {"full": [], "dispatch": []}
    for _ in range(5):
        full = run(tmp_path, full=True)
        thin = run(tmp_path)
        assert thin[:3] == full[:3]
        assert thin[3]["events"] == full[3]["events"]
        measured["full"].append(full[4])
        measured["dispatch"].append(thin[4])
    print(
        "W5_AUTH_WHOLE_COMMAND "
        + json.dumps(
            {
                "seconds": measured,
                "median_seconds": {
                    key: statistics.median(values) for key, values in measured.items()
                },
            }
        )
    )
