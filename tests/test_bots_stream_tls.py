from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from alysis_code.bots.hosted_tls import stream_certificate


def authority(*, ca=True):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test authority")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ip_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return SimpleNamespace(
        tls_certificate=cert.public_bytes(serialization.Encoding.PEM).decode(),
        tls_private_key=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        computer_stream_url="wss://δοκιμή.example/internal/computer-stream",
    )


def test_stream_leaf_preserves_trust_and_old_ip_identity():
    config = authority()
    original = vars(config).copy()
    chain, pem_key = stream_certificate(config)
    leaf, root = x509.load_pem_x509_certificates(chain.encode())
    leaf.verify_directly_issued_by(root)
    assert root.public_bytes(serialization.Encoding.PEM).decode() == config.tls_certificate
    assert vars(config) == original
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["localhost", "xn--jxalpdlp.example"]
    assert names.get_values_for_type(x509.IPAddress) == [ip_address("127.0.0.1")]
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert (
        ExtendedKeyUsageOID.SERVER_AUTH
        in leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    )
    assert leaf.not_valid_after_utc == root.not_valid_after_utc
    key = serialization.load_pem_private_key(pem_key.encode(), password=None)
    assert key.public_key().public_numbers() == leaf.public_key().public_numbers()


@pytest.mark.parametrize(
    "url",
    [None, "wss://127.0.0.1/internal/computer-stream", "wss://localhost/internal/computer-stream"],
)
def test_existing_tls_identity_is_unchanged(url):
    config = authority()
    config.computer_stream_url = url
    assert stream_certificate(config) == (config.tls_certificate, config.tls_private_key)


def test_stream_cannot_issue_from_a_leaf_or_mismatched_key():
    with pytest.raises(ValueError, match="cannot issue"):
        stream_certificate(authority(ca=False))
    config = authority()
    config.tls_private_key = authority().tls_private_key
    with pytest.raises(ValueError, match="do not match"):
        stream_certificate(config)
