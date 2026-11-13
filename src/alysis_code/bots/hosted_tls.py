"""Private HTTPS without writing the Key Vault TLS key to persistent storage."""

from __future__ import annotations

import os
import ssl
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from urllib.parse import urlsplit

import uvicorn


def stream_certificate(hosted):
    """Add the stream DNS identity without replacing the existing trust anchor.

    Older private installations use a self-signed CA as their server identity.
    Its key already lives in RAM. Issue a leaf in RAM so existing pinned clients
    and inspected guest HTTPS can both verify the same service.
    """
    certificate, key = hosted.tls_certificate, hosted.tls_private_key
    hostname = urlsplit(getattr(hosted, "computer_stream_url", None) or "").hostname
    if not certificate or not hostname:
        return certificate, key
    try:
        ip_address(hostname)
        return certificate, key
    except ValueError:
        hostname = hostname.encode("idna").decode("ascii")

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    authority = x509.load_pem_x509_certificate(certificate.encode())
    names = list(authority.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
    if x509.DNSName(hostname) in names:
        return certificate, key
    if not authority.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("The configured TLS certificate cannot issue a stream identity.")
    signing_key = serialization.load_pem_private_key(key.encode(), password=None)
    if signing_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) != authority.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ):
        raise ValueError("TLS authority and key do not match.")
    now = datetime.now(timezone.utc)
    if not authority.not_valid_before_utc <= now < authority.not_valid_after_utc:
        raise ValueError("The configured TLS authority is not currently valid.")
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(authority.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(max(authority.not_valid_before_utc, now - timedelta(minutes=1)))
        .not_valid_after(authority.not_valid_after_utc)
        .add_extension(
            x509.SubjectAlternativeName([*names, x509.DNSName(hostname)]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority.public_key()),
            critical=False,
        )
        .sign(
            signing_key,
            None
            if isinstance(signing_key, (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey))
            else hashes.SHA256(),
        )
    )
    return (
        leaf.public_bytes(serialization.Encoding.PEM).decode() + certificate,
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )


def server_config(app, hosted):
    if hosted.tls_certificate is None:
        return uvicorn.Config(
            app,
            host="127.0.0.1",
            port=hosted.port,
            access_log=False,
            ws_max_size=4 * 1024 * 1024,
            ws_max_queue=2,
            ws_per_message_deflate=False,
        )
    if not hasattr(os, "memfd_create"):
        raise RuntimeError("Hosted TLS requires the Linux memory-file facility")
    certificate, key = stream_certificate(hosted)
    # Uvicorn/OpenSSL take paths. Load their SSL context while the descriptors
    # exist, then close them; only the in-memory SSL context retains the key.
    with ExitStack() as resources:
        paths = []
        for label, value in (
            ("certificate", certificate),
            ("key", key),
        ):
            descriptor = os.memfd_create("alysis-bots-tls-" + label, os.MFD_CLOEXEC)
            resources.callback(os.close, descriptor)
            with os.fdopen(os.dup(descriptor), "wb") as stream:
                stream.write(value.encode())
            paths.append(f"/proc/self/fd/{descriptor}")
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=hosted.port,
            access_log=False,
            ssl_certfile=paths[0],
            ssl_keyfile=paths[1],
            ssl_version=ssl.PROTOCOL_TLS_SERVER,
            ws_max_size=4 * 1024 * 1024,
            ws_max_queue=2,
            ws_per_message_deflate=False,
        )
        config.load()
        config.ssl.minimum_version = ssl.TLSVersion.TLSv1_2
    return config
