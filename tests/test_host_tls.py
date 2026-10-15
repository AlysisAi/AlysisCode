from __future__ import annotations

from pathlib import Path

import certifi

from alysis_code.host_tls import EXTRA_CA_CERTS_ENV, apply_extra_ca_certs

_CORP_CA = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBszCCAVmgAwIBAgIUAcorpCA0000000000000000000000000wCgYIKoZIzj0EAwIw\n"
    "-----END CERTIFICATE-----\n"
)


def _corp_bundle(tmp_path: Path, text: str = _CORP_CA) -> Path:
    path = tmp_path / "corp-ca.pem"
    # Bytes, not text: write_text would translate "\n" to "\r\n" on Windows and the
    # merged-bundle assertions below compare exact bytes.
    path.write_bytes(text.encode("utf-8"))
    return path


def test_unset_variable_is_a_no_op(tmp_path: Path) -> None:
    env: dict[str, str] = {"PATH": "/bin"}
    assert apply_extra_ca_certs(env, cache_dir=tmp_path / "cache") is None
    assert env == {"PATH": "/bin"}
    assert not (tmp_path / "cache").exists()


def test_extra_bundle_is_merged_after_certifi_and_exported(tmp_path: Path) -> None:
    corp = _corp_bundle(tmp_path)
    env = {EXTRA_CA_CERTS_ENV: str(corp)}
    warnings: list[str] = []

    merged = apply_extra_ca_certs(env, cache_dir=tmp_path / "cache", warn=warnings.append)

    assert merged is not None and merged.is_file()
    assert warnings == []
    assert env["SSL_CERT_FILE"] == str(merged)
    assert env["REQUESTS_CA_BUNDLE"] == str(merged)
    assert env["CURL_CA_BUNDLE"] == str(merged)
    content = merged.read_bytes()
    base = Path(certifi.where()).read_bytes().rstrip(b"\r\n")
    # certifi's roots come first and intact; the corporate CA is appended as its own block.
    assert content.startswith(base + b"\n")
    assert content.endswith(_CORP_CA.encode())
    assert (
        content.count(b"-----BEGIN CERTIFICATE-----")
        == base.count(b"-----BEGIN CERTIFICATE-----") + 1
    )


def test_merged_bundle_is_content_addressed_and_reused(tmp_path: Path) -> None:
    corp = _corp_bundle(tmp_path)
    first = apply_extra_ca_certs({EXTRA_CA_CERTS_ENV: str(corp)}, cache_dir=tmp_path / "cache")
    assert first is not None
    stamp = first.stat().st_mtime_ns
    second = apply_extra_ca_certs({EXTRA_CA_CERTS_ENV: str(corp)}, cache_dir=tmp_path / "cache")
    assert second == first
    assert first.stat().st_mtime_ns == stamp, "an existing merged bundle is not rewritten"
    assert len(list((tmp_path / "cache").iterdir())) == 1, "no temp files are left behind"


def test_explicit_ssl_cert_file_is_extended_not_replaced(tmp_path: Path) -> None:
    corp = _corp_bundle(tmp_path)
    user_bundle = tmp_path / "user-bundle.pem"
    user_bundle.write_bytes(b"-----BEGIN CERTIFICATE-----\nUSERROOT\n-----END CERTIFICATE-----\n")
    env = {EXTRA_CA_CERTS_ENV: str(corp), "SSL_CERT_FILE": str(user_bundle)}

    merged = apply_extra_ca_certs(env, cache_dir=tmp_path / "cache")

    assert merged is not None
    assert env["SSL_CERT_FILE"] == str(merged)
    content = merged.read_text(encoding="utf-8")
    assert content.startswith("-----BEGIN CERTIFICATE-----\nUSERROOT"), (
        "the user's bundle is the base"
    )
    assert content.index("USERROOT") < content.index("corpCA")
    assert content.count("-----BEGIN CERTIFICATE-----") == 2, "certifi is not mixed in"


def test_child_bundle_variables_never_override_an_explicit_value(tmp_path: Path) -> None:
    corp = _corp_bundle(tmp_path)
    env = {EXTRA_CA_CERTS_ENV: str(corp), "REQUESTS_CA_BUNDLE": "/opt/explicit.pem"}
    merged = apply_extra_ca_certs(env, cache_dir=tmp_path / "cache")
    assert merged is not None
    assert env["REQUESTS_CA_BUNDLE"] == "/opt/explicit.pem"
    assert env["CURL_CA_BUNDLE"] == str(merged)


def test_missing_or_non_pem_bundle_is_reported_and_leaves_trust_untouched(tmp_path: Path) -> None:
    warnings: list[str] = []
    env = {EXTRA_CA_CERTS_ENV: str(tmp_path / "absent.pem")}
    assert apply_extra_ca_certs(env, cache_dir=tmp_path / "cache", warn=warnings.append) is None
    assert "SSL_CERT_FILE" not in env
    assert warnings and "cannot read" in warnings[0]

    warnings.clear()
    not_pem = _corp_bundle(tmp_path, "this is a DER file or garbage")
    env = {EXTRA_CA_CERTS_ENV: str(not_pem)}
    assert apply_extra_ca_certs(env, cache_dir=tmp_path / "cache", warn=warnings.append) is None
    assert "SSL_CERT_FILE" not in env
    assert warnings and "no PEM certificate" in warnings[0]
    assert not (tmp_path / "cache").exists()
