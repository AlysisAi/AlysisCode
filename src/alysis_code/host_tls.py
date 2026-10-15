"""Trust additional CA certificates on top of the bundled roots.

httpx -- the CLI's only HTTP client -- reads ``SSL_CERT_FILE`` when it builds its
TLS context, but treats it as a *replacement* for certifi's bundle rather than an
addition. Pointing that variable at a corporate proxy's CA alone would break every
public HTTPS endpoint. The IDE extension therefore passes the extra bundle as
``ALYSIS_EXTRA_CA_CERTS``; this module merges it with the current base bundle into a
content-addressed cache file and exports that as ``SSL_CERT_FILE`` (plus the
``requests``/curl spellings for child processes) before any client is created.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, MutableMapping
from pathlib import Path

EXTRA_CA_CERTS_ENV = "ALYSIS_EXTRA_CA_CERTS"
_BASE_BUNDLE_ENV = "SSL_CERT_FILE"
_CHILD_BUNDLE_ENVS = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_PEM_MARKER = b"-----BEGIN CERTIFICATE-----"

Warn = Callable[[str], None]


def apply_extra_ca_certs(
    environ: MutableMapping[str, str] | None = None,
    *,
    cache_dir: Path | None = None,
    warn: Warn | None = None,
) -> Path | None:
    """Merge ``ALYSIS_EXTRA_CA_CERTS`` into the trust bundle and export it.

    Returns the merged bundle path, or ``None`` when nothing was applied. Never
    raises: a bad bundle is reported through ``warn`` and the process keeps its
    default trust store, which is strictly better than starting with none.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(EXTRA_CA_CERTS_ENV) or "").strip()
    if not raw:
        return None

    addition = _read_pem(Path(raw), what=EXTRA_CA_CERTS_ENV, warn=warn)
    if addition is None:
        return None

    base_path = _base_bundle_path(env, warn=warn)
    if base_path is None:
        return None
    base = _read_pem(base_path, what="base CA bundle", warn=warn)
    if base is None:
        return None

    # A trailing newline on each part keeps the last BEGIN/END block of the base
    # bundle from running into the first block of the addition.
    merged = base.rstrip(b"\r\n") + b"\n" + addition.rstrip(b"\r\n") + b"\n"
    digest = hashlib.sha256(merged).hexdigest()[:16]
    target_dir = cache_dir if cache_dir is not None else _default_cache_dir()
    target = target_dir / f"ca-bundle-{digest}.pem"
    try:
        if not target.is_file():
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            tmp.write_bytes(merged)
            os.replace(tmp, target)
    except OSError as exc:
        _emit(warn, f"{EXTRA_CA_CERTS_ENV} ignored: cannot write merged bundle: {exc}")
        return None

    env[_BASE_BUNDLE_ENV] = str(target)
    for name in _CHILD_BUNDLE_ENVS:
        env.setdefault(name, str(target))
    return target


def _base_bundle_path(env: MutableMapping[str, str], *, warn: Warn | None) -> Path | None:
    # An explicit bundle from the user's shell is the base to extend, not something
    # to discard; otherwise start from certifi like httpx itself does.
    explicit = (env.get(_BASE_BUNDLE_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    try:
        import certifi
    except ImportError as exc:  # pragma: no cover - certifi ships with httpx
        _emit(warn, f"{EXTRA_CA_CERTS_ENV} ignored: certifi unavailable: {exc}")
        return None
    return Path(certifi.where())


def _read_pem(path: Path, *, what: str, warn: Warn | None) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError as exc:
        _emit(warn, f"{EXTRA_CA_CERTS_ENV} ignored: cannot read {what} {path}: {exc}")
        return None
    if _PEM_MARKER not in data:
        _emit(warn, f"{EXTRA_CA_CERTS_ENV} ignored: {what} {path} contains no PEM certificate")
        return None
    return data


def _default_cache_dir() -> Path:
    from platformdirs import user_cache_dir

    from .branding import CANONICAL_APP_NAME

    return Path(user_cache_dir(CANONICAL_APP_NAME, CANONICAL_APP_NAME)) / "tls"


def _emit(warn: Warn | None, message: str) -> None:
    if warn is not None:
        warn(message)
