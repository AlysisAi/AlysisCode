"""Narrow current-main corrections; real grammar/policy, no native launch."""

import builtins

import pytest

from alysis_code.agent.acceptance_contract import AcceptancePathRole, build_acceptance_contract
from alysis_code.config import ConfigError
from alysis_code.git_native import run
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.mark.parametrize(
    "source",
    [
        "docs/alpha.txt",
        r"C:\Users\test\Windows App\alpha.txt",
        r"\\server\share\Windows App\alpha.txt",
    ],
)
@pytest.mark.parametrize("verb", ["Read", "Read from"])
def test_bare_read_and_existing_read_from_keep_input_role(tmp_path, source, verb):
    contract = build_acceptance_contract(
        root=tmp_path, instruction=f"{verb} {source} and create result.txt."
    )
    roles = {ref.display_path: ref.role for ref in contract.path_refs}
    assert roles[source.replace("\\", "/")] == AcceptancePathRole.EXISTING_INPUT
    assert roles["result.txt"] == AcceptancePathRole.REQUIRED_OUTPUT
    # A declaration neither creates files nor authorizes reading an external root.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "instruction,role",
    [
        ("Discuss docs/read.txt.", AcceptancePathRole.UNKNOWN_REFERENCE),
        ("Create docs/read.txt.", AcceptancePathRole.REQUIRED_OUTPUT),
    ],
)
def test_read_word_in_filename_is_not_an_input_predicate(tmp_path, instruction, role):
    contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert {ref.display_path: ref.role for ref in contract.path_refs} == {"docs/read.txt": role}
    assert list(tmp_path.iterdir()) == []


def test_attempt_remains_nonpromissory(tmp_path):
    contract = build_acceptance_contract(
        root=tmp_path, instruction="Attempt to read docs/alpha.txt and create docs/denied.txt."
    )
    assert {ref.display_path: ref.role for ref in contract.path_refs} == {
        "docs/alpha.txt": AcceptancePathRole.UNKNOWN_REFERENCE,
        "docs/denied.txt": AcceptancePathRole.UNKNOWN_REFERENCE,
    }
    assert contract.allowed_output_paths == set()


@pytest.mark.parametrize(
    "settings,argv,message",
    [
        (ShellSandboxSettings(filesystem_access="read-only"), ["reset", "--hard"], "disabled"),
        (ShellSandboxSettings(safe_git_writes=False), ["reset", "--hard"], "disabled"),
        (ShellSandboxSettings(), [], "Unsupported literal"),
        (ShellSandboxSettings(), ["commit", "bad\0argument"], "Unsupported literal"),
        (ShellSandboxSettings(), ["commit", "--interactive"], "non-interactive"),
    ],
)
def test_refused_native_git_never_imports_preparation(
    tmp_path, monkeypatch, settings, argv, message
):
    original = builtins.__import__
    attempted = []

    def observe_import(name, *args, **kwargs):
        if name in {
            "_windows_git_reservations",
            "windows_mxc",
            "windows_private_state",
            "windows_toolchain",
        }:
            attempted.append(name)
            raise AssertionError("refused request reached native preparation")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", observe_import)
    with pytest.raises(ConfigError, match=message):
        run(
            settings=settings,
            root=tmp_path,
            cwd=tmp_path,
            argv=argv,
            timeout_s=30,
            cancel_check=None,
        )
    assert attempted == []
    assert list(tmp_path.iterdir()) == []
