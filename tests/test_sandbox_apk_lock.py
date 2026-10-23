from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sandbox/lock_apk_inputs.py"
SPEC = importlib.util.spec_from_file_location("apk_lock", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
LOCKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOCKER)


def receipt(name: str = "python-3.12", version: str = "3.12.15-r0") -> str:
    return (
        f"P:{name}\nV:{version}\nA:x86_64\nC:Q1DSTsCbyHVrFZwdcjUjoSU1n2mgM=\n"
        "F:usr\nF:usr/bin\nR:python\nZ:Q1DSTsCbyHVrFZwdcjUjoSU1n2mgM=\n"
    )


def inventory(*records: str) -> str:
    return "Installed APK records:\n" + "\n".join(records)


def test_installed_receipt_ignores_file_records_and_sorts_packages() -> None:
    rows = LOCKER.parse_inventory(
        inventory(receipt(), receipt("apk-tools", "2.14.10-r16")), "x86_64"
    )
    assert [row["name"] for row in rows] == ["apk-tools", "python-3.12"]
    assert rows[1]["version"] == "3.12.15-r0"


@pytest.mark.parametrize("name", ["../escape", "--help", "pkg/path", "pkg\nP:other"])
def test_rejects_unsafe_package_names(name: str) -> None:
    with pytest.raises(ValueError):
        LOCKER.parse_inventory(inventory(receipt(name)), "x86_64")


@pytest.mark.parametrize("version", ["1.0-r0/../../escape", "1.0-r0?redirect", "$(id)-r0"])
def test_rejects_unsafe_package_versions(version: str) -> None:
    with pytest.raises(ValueError):
        LOCKER.parse_inventory(inventory(receipt(version=version)), "x86_64")


def test_rejects_duplicate_or_incomplete_identities() -> None:
    with pytest.raises(ValueError, match="Duplicate installed"):
        LOCKER.parse_inventory(inventory(receipt(), receipt()), "x86_64")
    with pytest.raises(ValueError, match="Incomplete"):
        LOCKER.parse_inventory(inventory(receipt().replace("A:x86_64\n", "")), "x86_64")
    with pytest.raises(ValueError, match="architecture"):
        LOCKER.parse_inventory(inventory(receipt()), "aarch64")


def test_rejects_summary_without_complete_database() -> None:
    with pytest.raises(ValueError, match="Missing"):
        LOCKER.parse_inventory("python-3.12-3.12.15-r0\n", "x86_64")
