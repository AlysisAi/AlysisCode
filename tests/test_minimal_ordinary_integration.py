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
@pytest.mark.parametrize("verb", ["Read", "Read from", "Διάβασε το"])
def test_mentions_keep_their_spelling_and_get_no_role_from_the_wording(tmp_path, source, verb):
    contract = build_acceptance_contract(
        root=tmp_path, instruction=f"{verb} {source} and create result.txt."
    )
    roles = {ref.display_path: ref.role for ref in contract.path_refs}
    assert roles == {
        source.replace("\\", "/"): AcceptancePathRole.UNKNOWN_REFERENCE,
        "result.txt": AcceptancePathRole.UNKNOWN_REFERENCE,
    }
    # Detection neither creates files nor authorizes reading an external root.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("instruction", ["Discuss docs/read.txt.", "Create docs/read.txt."])
def test_a_verb_in_a_filename_or_before_it_is_not_a_role(tmp_path, instruction):
    contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert {ref.display_path: ref.role for ref in contract.path_refs} == {
        "docs/read.txt": AcceptancePathRole.UNKNOWN_REFERENCE
    }
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
