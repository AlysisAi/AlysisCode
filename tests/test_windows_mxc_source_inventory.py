"""Real inventory producer with an inert Git port; never invoke builder main.

The APP fixture is an exact declaration excerpt from the named immutable blob.
It is a correspondence oracle, not a replacement APP validator or qualification.
All filesystem effects are confined to pytest-owned synthetic source files.
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
BUILDER = HERE.parent / "src/alysis_code/windows_mxc_candidate_builder.py"
ARCHIVE = HERE.parent / "scripts/build_windows_mxc.py"
APP_DECLARATION = HERE / "fixtures" / "windows_mxc_app_source_paths.json"
SOURCE_COMMIT = "83d5fbe9e2126e562542ce0ce8a1f94374057618"
SYNTHETIC_SOURCES = {
    "native/windows-sandbox/src/main.rs": b"// synthetic sandbox input\n",
    "native/windows-sandbox/vendor/srt-win/LICENSE": b"synthetic attribution\n",
    "native/git-ssh-sign/src/main.rs": b"// synthetic signer input\n",
    "src/alysis_code/windows_mxc_candidate_builder.py": b"# synthetic active producer input\n",
    "src/alysis_code/windows_mxc_contract.py": b"# synthetic contract input\n",
    "hatch_build.py": b"# synthetic wheel hook input\n",
    "pyproject.toml": b"# synthetic project input\n",
    "uv.lock": b"# synthetic lock input; no dependency tool\n",
    "scripts/build_windows_mxc.py": b"# synthetic builder input; never executed\n",
    "scripts/prepare_windows_git.py": b"# synthetic Git preparation input\n",
}


@pytest.fixture
def app_source_paths():
    frozen = json.loads(APP_DECLARATION.read_text(encoding="utf-8"))
    line = frozen["source_line"]
    prefix = "const sourcePaths = "
    assert line.startswith(prefix) and line.endswith(";")
    # Historical beta inventory remains immutable; new local candidates also bind the wheel hook.
    return [*json.loads(line[len(prefix) : -1]), "hatch_build.py"]


@pytest.fixture
def builder(monkeypatch):
    # Import only the canonical module, whose __main__ guard must stay inactive.
    # Undo its src-path insertion without changing the calling runner's list.
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("_mxc_inventory_control", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def unexpected_effect(*args, **kwargs):
        raise AssertionError("No process effect is permitted by this inventory control")

    # Replace this module's port, not subprocess globally. No Git/cargo/helper.
    monkeypatch.setattr(
        module, "subprocess", SimpleNamespace(check_output=unexpected_effect, run=unexpected_effect)
    )
    return module


@pytest.fixture
def inventory_case(tmp_path, builder, app_source_paths, monkeypatch):
    root = tmp_path.resolve()
    for name, data in SYNTHETIC_SOURCES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    state = SimpleNamespace(
        # Exercise actual producer sorting/deduplication of tracked names.
        names=[*reversed(SYNTHETIC_SOURCES), "scripts/build_windows_mxc.py"],
        commit=SOURCE_COMMIT,
        calls=[],
    )

    def check_output(argv, *, cwd):
        assert cwd == root
        state.calls.append(list(argv))
        if argv[:4] == ["git", "ls-files", "-z", "--"]:
            # Independent frozen APP declaration, never derived from builder.
            assert argv[4:] == app_source_paths, "Producer/consumer source membership differs"
            return ("\0".join(state.names) + "\0").encode()
        assert argv == ["git", "rev-parse", "HEAD"], "Unexpected Git/effect dispatch"
        return (state.commit + "\n").encode()

    monkeypatch.setattr(builder.subprocess, "check_output", check_output)
    return root, state


def test_local_inventory_extends_historical_beta_scope_with_wheel_hook(builder, app_source_paths):
    assert builder.SOURCE_PATHS == app_source_paths


def test_real_producer_binds_complete_names_bytes_and_exact_commit(builder, inventory_case):
    root, state = inventory_case
    inventory = builder.source_inventory(root)
    assert inventory == {
        "schema_version": 1,
        "source_commit": SOURCE_COMMIT,
        "files": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in sorted(SYNTHETIC_SOURCES.items())
        },
    }
    assert list(inventory["files"]) == sorted(SYNTHETIC_SOURCES)
    assert state.calls == [
        ["git", "ls-files", "-z", "--", *builder.SOURCE_PATHS],
        ["git", "rev-parse", "HEAD"],
    ]


@pytest.mark.parametrize("missing_index", range(8))
def test_each_missing_declared_member_is_detected(
    builder, inventory_case, monkeypatch, missing_index
):
    root, state = inventory_case
    changed = list(builder.SOURCE_PATHS)
    del changed[missing_index]
    monkeypatch.setattr(builder, "SOURCE_PATHS", changed)
    with pytest.raises(AssertionError, match="Producer/consumer source membership differs"):
        builder.source_inventory(root)
    assert len(state.calls) == 1  # No identity/build/helper dispatch after mismatch.


@pytest.mark.parametrize("extra", ["native/unreviewed", "scripts/unreviewed.py"])
def test_extra_declared_member_is_detected(builder, inventory_case, monkeypatch, extra):
    root, _state = inventory_case
    monkeypatch.setattr(builder, "SOURCE_PATHS", [*builder.SOURCE_PATHS, extra])
    with pytest.raises(AssertionError, match="Producer/consumer source membership differs"):
        builder.source_inventory(root)


@pytest.mark.parametrize("name", tuple(SYNTHETIC_SOURCES))
def test_missing_tracked_resource_refuses_before_identity_dispatch(builder, inventory_case, name):
    root, state = inventory_case
    (root / name).unlink()
    with pytest.raises(ValueError, match="Aliased or missing build source"):
        builder.source_inventory(root)
    assert len(state.calls) == 1


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_aliased_source_refuses(builder, inventory_case, alias, request):
    root, state = inventory_case
    path = root / "scripts/prepare_windows_git.py"
    target = root / "owned-alias-target"
    path.rename(target)
    if alias == "symlink":
        request.getfixturevalue("require_symlinks")
        path.symlink_to(target)
    else:
        path.hardlink_to(target)
    with pytest.raises(ValueError, match="Aliased or missing build source"):
        builder.source_inventory(root)
    assert len(state.calls) == 1


@pytest.mark.parametrize("names", [[], ["scripts/prepare_windows_git.py"]])
def test_untracked_builder_refuses(builder, inventory_case, names):
    root, state = inventory_case
    state.names = names
    with pytest.raises(ValueError, match="MXC builder must be tracked"):
        builder.source_inventory(root)
    assert len(state.calls) == 1


def test_changed_source_commit_or_bytes_cannot_reuse_inventory(builder, inventory_case):
    root, state = inventory_case
    before = builder.source_inventory(root)
    state.commit = "8620a3b86b14cd6d1c8e4eff1351bf7b6f966d14"
    changed_commit = builder.source_inventory(root)
    assert changed_commit["source_commit"] == state.commit
    assert changed_commit != before
    assert changed_commit["files"] == before["files"]
    state.commit = SOURCE_COMMIT
    name = "native/git-ssh-sign/src/main.rs"
    updated = b"// changed synthetic signer input\n"
    (root / name).write_bytes(updated)
    changed_bytes = builder.source_inventory(root)
    assert changed_bytes["source_commit"] == before["source_commit"]
    assert changed_bytes["files"][name] == hashlib.sha256(updated).hexdigest()
    assert changed_bytes != before


def test_archived_recipe_is_exact_inventory_input_and_active_location(builder):
    expected = "e2027d7bfddc4466c222fc7d0df8f3f856f9e3e38008ddc3f2a5ecdaeedea57c"
    sources = json.loads(
        (HERE.parent / "src/alysis_code/assets/windows-mxc/SOURCE.json").read_bytes()
    )
    assert sources["files"]["scripts/build_windows_mxc.py"] == expected
    assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() == expected
    assert builder.REPO == HERE.parent
    assert builder.CANDIDATE_RECIPE_VERSION == 3
    assert builder.CANDIDATE_SOURCE == BUILDER.relative_to(HERE.parent).as_posix()


@pytest.mark.parametrize(
    "name",
    [
        "hatch_build.py",
        "native/git-ssh-sign/src/main.rs",
        "scripts/prepare_windows_git.py",
        "src/alysis_code/windows_mxc_candidate_builder.py",
    ],
)
def test_each_future_provenance_input_changes_active_inventory(builder, inventory_case, name):
    root, _state = inventory_case
    before = builder.source_inventory(root)
    (root / name).write_bytes(b"changed source bytes\n")
    after = builder.source_inventory(root)
    assert before["source_commit"] == after["source_commit"]
    assert before["files"][name] != after["files"][name]
    assert {key for key in before["files"] if before["files"][key] != after["files"][key]} == {name}


def test_active_recipe_must_itself_be_tracked(builder, inventory_case):
    root, state = inventory_case
    state.names.remove(builder.CANDIDATE_SOURCE)
    with pytest.raises(ValueError, match="Active MXC candidate builder must be tracked"):
        builder.source_inventory(root)
    assert len(state.calls) == 1


def test_candidate_refuses_non_windows_before_effects(builder, monkeypatch, tmp_path):
    monkeypatch.setattr(builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sys, "argv", [str(BUILDER), "--output", str(tmp_path / "new")])
    with pytest.raises(SystemExit) as exc:
        builder.main()
    assert exc.value.code == 2
    assert not (tmp_path / "new").exists()


def test_candidate_refuses_existing_output_before_effects(builder, monkeypatch, tmp_path):
    monkeypatch.setattr(builder.platform, "system", lambda: "Windows")
    monkeypatch.setattr(builder.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(sys, "argv", [str(BUILDER), "--output", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        builder.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("mutation", [None, "contract", "source", "helper"])
def test_active_main_retains_contract_and_before_after_guards(
    builder, inventory_case, monkeypatch, mutation
):
    root, state = inventory_case
    output = root / "new-bundle"
    helper = root / "native/windows-sandbox/target-mxc/release/alysis-sandbox.exe"
    monkeypatch.setattr(builder, "REPO", root)
    monkeypatch.setattr(builder.platform, "system", lambda: "Windows")
    monkeypatch.setattr(builder.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(sys, "argv", [str(BUILDER), "--output", str(output)])
    original_git = builder.subprocess.check_output
    effects = []

    def inert_compile(argv, *, check):
        assert check is True
        assert argv == [
            "cargo",
            "+1.90.0",
            "build",
            "--locked",
            "--manifest-path",
            str(root / "native/windows-sandbox/Cargo.toml"),
            "--features",
            "mxc",
            "-j",
            "2",
            "--target-dir",
            str(helper.parent.parent),
            "--release",
        ]
        effects.append("compile")
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"inert helper bytes, never executed")

    def original_ports(argv, **kwargs):
        if argv[0] == "git":
            return original_git(argv, **kwargs)
        assert argv == [str(helper), "mxc-protocol"]
        assert kwargs == {"timeout": 15}
        effects.append("contract")
        contract = builder.helper_contract()
        if mutation == "contract":
            contract = {**contract, "unexpected": True}
        elif mutation == "source":
            (root / "scripts/prepare_windows_git.py").write_bytes(b"changed during build")
        elif mutation == "helper":
            helper.write_bytes(b"changed during protocol read")
        return json.dumps(contract).encode()

    monkeypatch.setattr(builder.subprocess, "run", inert_compile)
    monkeypatch.setattr(builder.subprocess, "check_output", original_ports)
    if mutation:
        with pytest.raises(ValueError, match="does not match|changed during build"):
            builder.main()
        assert not output.exists()
    else:
        builder.main()
        recorded = json.loads((output / "SOURCE.json").read_bytes())
        assert recorded["files"][builder.CANDIDATE_SOURCE]
        assert recorded["files"]["native/git-ssh-sign/src/main.rs"]
        assert recorded["files"]["scripts/prepare_windows_git.py"]
        assert (output / "alysis-sandbox.exe").read_bytes() == helper.read_bytes()
        assert (
            json.loads((output / "manifest.json").read_bytes())["source_inventory_sha256"]
            == hashlib.sha256((output / "SOURCE.json").read_bytes()).hexdigest()
        )
    assert effects == ["compile", "contract"]


def test_wheel_hook_must_itself_be_tracked(builder, inventory_case):
    root, state = inventory_case
    state.names.remove("hatch_build.py")
    with pytest.raises(ValueError, match="Candidate wheel hook must be tracked"):
        builder.source_inventory(root)
    assert len(state.calls) == 1
