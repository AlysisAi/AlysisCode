"""Run the real configured Hatch hook and metadata renderer, never build a wheel.

The entire native tree remains present. All mutations occur in pytest-owned
copies; the signed assets, hook and SOURCE inventory are never repaired here.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from hatchling.builders.wheel import WheelBuilder

ROOT = Path(__file__).resolve().parents[1]
BUILDER = "scripts/build_windows_mxc.py"
RECORDED = "e2027d7bfddc4466c222fc7d0df8f3f856f9e3e38008ddc3f2a5ecdaeedea57c"
DRIFTED = "48970399f29be22bba22c30ad23e4cb66c39466cb1f96c2c07af3e72f96a69ce"


@pytest.fixture
def metadata_source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("src", "native", "scripts"):
        # Keep every source; compiler caches can be several GB after qualification.
        shutil.copytree(
            ROOT / name,
            root / name,
            ignore=shutil.ignore_patterns("target", "target-*", "__pycache__"),
        )
    for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE", "hatch_build.py", "uv.lock"):
        shutil.copy2(ROOT / name, root / name)
    assert (root / "native/windows-sandbox/Cargo.toml").is_file()
    return root


def selected_comparisons(root):
    inventory = json.loads((root / "src/alysis_code/assets/windows-mxc/SOURCE.json").read_text())
    selected = {
        name: expected
        for name, expected in inventory["files"].items()
        if name.startswith("native/windows-sandbox/")
        or name in {"src/alysis_code/windows_mxc_contract.py", BUILDER}
    }
    assert len(selected) == 289  # Public native inputs; private qualification reports excluded.
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
        for name, expected in selected.items()
    }


def prepare_metadata_only(root, output):
    builder = WheelBuilder(str(root))
    builder.metadata.validate_fields()
    hooks = builder.get_build_hooks(str(output))
    assert set(hooks) == {"custom"}  # Actual pyproject selection, no hook stub.
    hooks["custom"].initialize("standard", builder.get_default_build_data())
    metadata = builder.config.core_metadata_constructor(builder.metadata)
    output.mkdir()
    (output / "METADATA").write_text(metadata, encoding="utf-8", newline="\n")
    assert not list(output.glob("*.whl"))
    return metadata


def test_recorded_builder_passes_actual_hook_and_metadata(metadata_source, tmp_path):
    root = metadata_source
    assert hashlib.sha256((root / BUILDER).read_bytes()).hexdigest() == RECORDED
    comparisons = selected_comparisons(root)
    assert all(comparisons.values())
    metadata = prepare_metadata_only(root, tmp_path / "metadata")
    assert "Name: alysis-code" in metadata.splitlines()
    print(
        json.dumps(
            {
                "selected_inventory_comparisons": len(comparisons),
                "matches": sum(comparisons.values()),
                "real_hook": "passed",
                "metadata_sha256": hashlib.sha256(metadata.encode()).hexdigest(),
                "wheel_built": False,
            }
        )
    )


def test_exact_merged_builder_reproduces_original_refusal(metadata_source, tmp_path):
    root = metadata_source
    data = (
        (root / BUILDER)
        .read_bytes()
        .replace(
            b'    "native/windows-sandbox",\n',
            b'    "native/windows-sandbox",\n    "native/git-ssh-sign",\n',
        )
        .replace(
            b'    "scripts/build_windows_mxc.py",\n',
            b'    "scripts/build_windows_mxc.py",\n    "scripts/prepare_windows_git.py",\n',
        )
    )
    assert hashlib.sha256(data).hexdigest() == DRIFTED
    (root / BUILDER).write_bytes(data)
    comparisons = selected_comparisons(root)
    assert [name for name, matches in comparisons.items() if not matches] == [BUILDER]
    with pytest.raises(ValueError, match="Rebuild/sign.*scripts/build_windows_mxc.py"):
        prepare_metadata_only(root, tmp_path / "refused")
    assert not (tmp_path / "refused").exists()


@pytest.mark.parametrize(
    "path,reason",
    [
        ("native/windows-sandbox/Cargo.toml", "Rebuild/sign"),
        ("src/alysis_code/windows_mxc_contract.py", "Rebuild/sign"),
        ("src/alysis_code/assets/windows-mxc/SOURCE.json", "bundle integrity"),
    ],
)
def test_native_contract_or_inventory_drift_still_refuses(metadata_source, tmp_path, path, reason):
    target = metadata_source / path
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match=reason):
        prepare_metadata_only(metadata_source, tmp_path / "refused")
    assert not (tmp_path / "refused").exists()
