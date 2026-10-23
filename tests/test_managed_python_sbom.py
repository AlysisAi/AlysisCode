from copy import deepcopy

import pytest

from scripts.release.add_managed_python_sbom import add_python


def _inputs() -> tuple[dict, dict, dict]:
    pin = {"url": "https://example.invalid/python.tar.gz", "sha256": "a" * 64, "size": 10}
    receipt = {
        "target": "win32-arm64",
        "distribution": "test-distribution",
        "pin": pin,
        "version": "3.12.15",
        "platform": "win32",
        "machine": "ARM64",
        "openssl": "OpenSSL 3.5.9 29 Sep 2026",
        "expat": "expat_2.8.5",
    }
    lock = {
        "version": "3.12.15",
        "distribution": "test-distribution",
        "targets": {"win32-arm64": deepcopy(pin)},
    }
    sbom = {
        "metadata": {"component": {"bom-ref": "application"}},
        "components": [
            {"bom-ref": "pkg:generic/openssl@3.5.9", "name": "openssl", "version": "3.5.9"}
        ],
        "dependencies": [
            {"ref": "application", "dependsOn": ["crypto"]},
            {"ref": "pkg:generic/openssl@3.5.9"},
        ],
    }
    return sbom, receipt, lock


def test_runtime_sbom_reuses_static_openssl_and_preserves_existing_edges() -> None:
    sbom, receipt, lock = _inputs()
    add_python(sbom, receipt, lock)
    assert len([item for item in sbom["components"] if item["name"] == "openssl"]) == 1
    assert sbom["dependencies"][0]["dependsOn"][0] == "crypto"
    python = next(item for item in sbom["components"] if item["name"] == "cpython")
    assert python["externalReferences"][0]["hashes"][0]["content"] == receipt["pin"]["sha256"]


@pytest.mark.parametrize(
    "field,value", [("sha256", "b" * 64), ("size", 11), ("url", "https://example.invalid/other")]
)
def test_runtime_receipt_cannot_change_the_committed_download(field: str, value: object) -> None:
    sbom, receipt, lock = _inputs()
    receipt["pin"][field] = value
    with pytest.raises(ValueError, match="distribution pin"):
        add_python(sbom, receipt, lock)
