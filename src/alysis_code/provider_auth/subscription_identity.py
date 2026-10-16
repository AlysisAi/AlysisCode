"""Validate SIWC identity before activating a subscription registration."""

from __future__ import annotations

import base64
import json
import math
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .base import ProviderAuthError

ISSUER = "https://auth.openai.com"


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_id_token(
    token: str,
    *,
    client_id: str,
    nonce: str | None,
    discovery: dict[str, Any],
    client_factory: Any,
) -> dict[str, Any]:
    """No unverified JWT claim can establish account identity or permission."""
    try:
        if not token or len(token) > 64_000:
            raise ValueError()
        header_part, claims_part, signature_part = token.split(".")
        header = json.loads(_decode(header_part))
        claims = json.loads(_decode(claims_part))
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise ValueError()
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError()
        jwks_uri = str(discovery.get("jwks_uri") or "")
        parsed = urlsplit(jwks_uri)
        if (
            discovery.get("issuer") != ISSUER
            or parsed.scheme != "https"
            or parsed.hostname != "auth.openai.com"
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError()
        with client_factory(timeout=20.0) as client:
            response = client.get(jwks_uri)
            response.raise_for_status()
        keys = response.json().get("keys")
        if not isinstance(keys, list):
            raise ValueError()
        matches = [
            key
            for key in keys
            if isinstance(key, dict)
            and key.get("kid") == header["kid"]
            and key.get("kty") == "RSA"
            and key.get("use", "sig") == "sig"
            and key.get("alg", "RS256") == "RS256"
        ]
        if len(matches) != 1:
            raise ValueError()
        key = matches[0]
        public_key = rsa.RSAPublicNumbers(
            int.from_bytes(_decode(key["e"]), "big"),
            int.from_bytes(_decode(key["n"]), "big"),
        ).public_key()
        if public_key.key_size < 2048:
            raise ValueError()
        public_key.verify(
            _decode(signature_part),
            f"{header_part}.{claims_part}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        now = time.time()
        audience = claims.get("aud")
        if not math.isfinite(float(claims.get("exp") or 0)):
            raise ValueError()
        if (
            claims.get("iss") != ISSUER
            or not (audience == client_id or isinstance(audience, list) and client_id in audience)
            or isinstance(audience, list)
            and len(audience) > 1
            and claims.get("azp") != client_id
            or float(claims.get("exp") or 0) <= now
            or float(claims.get("nbf") or 0) > now + 30
            or float(claims.get("iat") or 0) > now + 30
            or nonce is not None
            and claims.get("nonce") != nonce
            or not isinstance(claims.get("sub"), str)
            or not claims["sub"].strip()
        ):
            raise ValueError()
        return claims
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        InvalidSignature,
        httpx.HTTPError,
    ) as exc:
        raise ProviderAuthError(
            "ChatGPT identity could not be verified. Start a fresh sign-in."
        ) from exc
