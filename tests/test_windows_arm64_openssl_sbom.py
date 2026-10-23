from copy import deepcopy

import pytest

from scripts.release.add_native_openssl_sbom import add_openssl


def _sbom() -> dict:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": [{"name": "cryptography", "bom-ref": "crypto"}],
        "dependencies": [{"ref": "crypto", "dependsOn": ["cffi"]}],
    }


def test_static_library_preserves_existing_dependency_edges() -> None:
    sbom = _sbom()
    add_openssl(sbom, "OpenSSL 3.5.9 29 Sep 2026")
    assert sbom["dependencies"][0]["dependsOn"] == ["cffi", "pkg:generic/openssl@3.5.9"]
    assert sbom["components"][1]["externalReferences"][0]["hashes"][0]["alg"] == "SHA-256"


@pytest.mark.parametrize("version", ["OpenSSL 3.5.8 old", "OpenSSL 3.5.90 wrong", "LibreSSL 3.5.9"])
def test_unexpected_linked_library_fails_before_sbom_mutation(version: str) -> None:
    sbom = _sbom()
    original = deepcopy(sbom)
    with pytest.raises(ValueError, match="pinned OpenSSL"):
        add_openssl(sbom, version)
    assert sbom == original


def test_missing_dependency_edge_fails_before_sbom_mutation() -> None:
    sbom = _sbom()
    sbom["dependencies"] = []
    original = deepcopy(sbom)
    with pytest.raises(ValueError, match="dependency record"):
        add_openssl(sbom, "OpenSSL 3.5.9 29 Sep 2026")
    assert sbom == original
