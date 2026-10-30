"""R4 inert delimiter and per-launch directory discriminators; no live effects."""

import subprocess
from pathlib import Path

import pytest
from test_approval_review import _Surface
from test_lr49_extra_bypasses import Vector, gate_harness
from test_lr49_github_credential_gate import harness

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason
from alysis_code.config import AppConfig
from alysis_code.shell_command_lexer import split_simple_commands

gate_harness = gate_harness
harness = harness

SPACES = ["\u00a0", "\u2003", "\u202f", "\v", "\f", "\x1c", "\x85", "\u2028", "\u2029", "\u3000"]


@pytest.mark.parametrize("space", SPACES)
@pytest.mark.parametrize("dialect", ["posix", "cmd", "powershell"])
@pytest.mark.parametrize("route", ["full", "auto", "ssh"])
def test_dialect_whitespace_never_certifies_unknown_program(gate_harness, space, dialect, route):
    _, surface, dispatched, _, run = gate_harness
    command = "echo" + space + "./publish"
    error = run(Vector("r4-whitespace", command, dialect), route)
    assert isinstance(error, ApprovalDeclinedError)
    assert not dispatched
    cards = [r for r in surface.requests if r.kind == "shell_run"]
    assert len(cards) == 1 and cards[0].command == command


@pytest.mark.parametrize("space", SPACES)
@pytest.mark.parametrize("dialect", ["posix", "cmd", "powershell"])
def test_quoted_unicode_data_is_preserved(space, dialect):
    name = "fixture" + space + "name.txt"
    parsed = split_simple_commands('cat "' + name + '"', dialect=dialect)
    assert parsed is not None and parsed[0].words == ("cat", name)
    if dialect == "posix":
        assert github_write_reason('cat "' + name + '"', dialect=dialect) is None


@pytest.mark.parametrize("space", SPACES)
@pytest.mark.parametrize(
    "template",
    ["{s}echo fixture", '"{s}echo" fixture', '"echo{s}" fixture', "echo fixture;{s}echo opaque"],
)
@pytest.mark.parametrize("dialect", ["posix", "cmd"])
def test_control_and_quoted_executable_whitespace_require_person(space, template, dialect):
    assert github_write_reason(template.format(s=space), dialect=dialect)


@pytest.mark.parametrize("dialect", ["posix", "cmd", "powershell"])
def test_real_ascii_blanks_and_newlines(dialect):
    parsed = split_simple_commands("echo\tfixture\necho next", dialect=dialect)
    assert parsed is not None
    assert [c.words for c in parsed] == [("echo", "fixture"), ("echo", "next")]


@pytest.mark.parametrize("replacement", ["unchanged", "directory", "symlink"])
@pytest.mark.parametrize("route", ["batch", "fallback", "consumer"])
def test_each_actual_verification_launch_keeps_original_directory(
    harness, monkeypatch, replacement, route
):
    import test_lr49_github_credential_gate as owner

    build, _, _, root = harness
    cfg = AppConfig(
        model="inert", verify_commands=["pytest -q tests/test_a.py", "pytest -q tests/test_b.py"]
    )
    cfg.extra_fields = {"verify_sandbox": {"mode": "off"}}
    original = owner.build_tools
    monkeypatch.setattr(owner, "build_tools", lambda **kw: original(**kw, cfg=cfg))
    (root / "tests").mkdir()
    for name in ["test_a.py", "test_b.py"]:
        (root / "tests" / name).write_text("def test_inert(): pass\n")
    (root / "fixture.txt").write_text("inert")
    evidence = root.parent / "external-evidence"
    evidence.mkdir()
    monkeypatch.setattr(
        build.store, "runtime_artifact_path", lambda *parts: evidence / Path(parts[-1]).name
    )
    calls = []
    original_inode = root.stat().st_ino

    def capture(self, *, root: object, cwd, cmd, **kwargs):
        calls.append((cmd, Path(cwd).stat().st_ino))
        if len(calls) == 1 and replacement != "unchanged":
            project.rename(project.parent / "retired")
            if replacement == "symlink":
                target = project.parent / "replacement"
                target.mkdir()
                project.symlink_to(target, target_is_directory=True)
            else:
                project.mkdir()
        if route == "fallback" and len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 127, "", "/bin/sh: pytest: command not found")
        return subprocess.CompletedProcess(cmd, 0, "1 passed in 0.01s", "")

    project = root
    monkeypatch.setattr("alysis_code.sandbox_runner.HostShellRunner.run", capture)
    args = {}
    if route == "consumer":
        args = {
            "consumer_profile": {
                "profile": "cli",
                "artifacts": ["fixture.txt"],
                "checks": [{"command": "python first.py"}, {"command": "python second.py"}],
            }
        }
    surface = _Surface(allow=True)
    payload = None
    try:
        payload = build(surface, "fullaccess", True, verification=True)["verify_run"].run(args)
    except Exception:
        # Existing artifact validation can fail after the captured loop; never
        # treat that failure as preventing an already captured second dispatch.
        pass
    cards = [r for r in surface.requests if r.metadata.get("always_ask_person")]
    assert len(cards) == 2
    assert all(r.metadata["cwd"] == str(project) for r in cards)
    if replacement == "unchanged":
        assert len(calls) == (3 if route == "fallback" else 2)
        if route != "consumer":
            assert all(inode == original_inode for _, inode in calls)
    else:
        assert len(calls) == 1, calls
        if payload is not None:
            assert payload.get("status") != "passed"


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
@pytest.mark.parametrize("fallback", [False, True])
def test_refusal_preserves_prior_output_and_never_restores_cache_over_replacement(
    tmp_path, monkeypatch, replacement, fallback
):
    from alysis_code.verify_gate import run_task_verification

    root = tmp_path / "project"
    root.mkdir()
    (root / ".pytest_cache").mkdir()
    (root / ".pytest_cache" / "original").write_text("original")
    commands = ["pytest -q first.py", "pytest -q second.py"]
    calls = []
    cfg = AppConfig(model="inert")
    cfg.extra_fields = {"verify_sandbox": {"mode": "off"}}

    def capture(self, *, cwd, cmd, **kw):
        calls.append(cmd)
        root.rename(tmp_path / "retired")
        target = tmp_path / "replacement" if replacement == "symlink" else root
        target.mkdir()
        if replacement == "symlink":
            root.symlink_to(target, target_is_directory=True)
        (target / ".pytest_cache").mkdir()
        (target / ".pytest_cache" / "replacement").write_text("retain me")
        return subprocess.CompletedProcess(
            cmd,
            127 if fallback else 0,
            "FIRST_CAPTURED_OUTPUT",
            "/bin/sh: pytest: command not found" if fallback else "",
        )

    monkeypatch.setattr("alysis_code.sandbox_runner.HostShellRunner.run", capture)
    artifact = tmp_path / "external-result.txt"
    result = run_task_verification(root=root, commands=commands, artifact_path=artifact, cfg=cfg)
    assert len(calls) == 1
    assert result.status == "failed"
    refused = result.command_results[-1]
    assert refused.non_execution_reason == "execution_directory_changed"
    assert refused.real_execution is False
    assert refused.command == commands[0 if fallback else 1]
    assert "FIRST_CAPTURED_OUTPUT" in artifact.read_text()
    assert "execution_directory_changed" in artifact.read_text()
    assert (root / ".pytest_cache" / "replacement").read_text() == "retain me"
    assert not (root / ".pytest_cache" / "original").exists()


@pytest.mark.parametrize("space", SPACES)
@pytest.mark.parametrize(
    "template", ['"gh{s}" pr view 1', '"{s}gh" pr view 1', 'command "gh{s}" pr view 1']
)
def test_quoted_executable_is_not_trimmed_into_github_read(gate_harness, space, template):
    _, surface, dispatched, _, run = gate_harness
    command = template.format(s=space)
    error = run(Vector("r4-quoted-program", command), "full")
    assert isinstance(error, ApprovalDeclinedError)
    assert not dispatched
    assert [r.command for r in surface.requests if r.kind == "shell_run"] == [command]


@pytest.mark.parametrize(
    "space", ["\u00a0", "\u2003", "\u202f", "\v", "\f", "\u2028", "\u2029", "\u3000"]
)
def test_powershell_uses_its_documented_blank_categories(space):
    parsed = split_simple_commands("echo" + space + "fixture", dialect="powershell")
    assert parsed is not None and parsed[0].words == ("echo", "fixture")
    assert github_write_reason("echo" + space + "fixture", dialect="powershell")


@pytest.mark.parametrize("dialect", ["posix", "cmd", "powershell"])
@pytest.mark.parametrize("control", ["\x00", "\x1c", "\x1d", "\x1e", "\x1f", "\x85"])
def test_unsupported_unquoted_controls_are_not_blanks(dialect, control):
    assert split_simple_commands("echo" + control + "opaque", dialect=dialect) is None
