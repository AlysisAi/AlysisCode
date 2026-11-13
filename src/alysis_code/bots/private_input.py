"""Ephemeral encryption for human input over the authenticated operator RPC.

Only the guest public key crosses the existing provider channel. Private keys
and plaintext stay in memory; command arguments contain authenticated ciphertext.
This protects typed input from command metadata, not from the trusted OS/provider.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DOMAIN = b"alysis-bots-human-input-v1"


def _encode(value):
    return base64.b64encode(value).decode("ascii")


def _decode(value):
    return base64.b64decode(value, validate=True)


def _aad(session, request_id, sequence):
    return json.dumps([session, request_id, sequence], separators=(",", ":")).encode()


def _key(private, public, session):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=session.encode(), info=DOMAIN).derive(
        private.exchange(X25519PublicKey.from_public_bytes(public))
    )


def seal_input(public_key, session, request_id, sequence, arguments):
    """The authenticated provider's key receipt pins this allocation's receiver."""
    private = X25519PrivateKey.generate()
    nonce = os.urandom(12)
    plaintext = json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode()
    if len(plaintext) > 33000:
        raise ValueError("Human input exceeds its transport allowance.")
    ciphertext = AESGCM(_key(private, _decode(public_key), session)).encrypt(
        nonce, plaintext, _aad(session, request_id, sequence)
    )
    return {
        "sequence": sequence,
        "public_key": _encode(private.public_key().public_bytes_raw()),
        "nonce": _encode(nonce),
        "ciphertext": _encode(ciphertext),
    }


class PrivateInput:
    def __init__(self, session):
        self.session = session
        self._private = X25519PrivateKey.generate()
        self._sequence = 0

    @property
    def public_key(self):
        return _encode(self._private.public_key().public_bytes_raw())

    def open(self, request_id, envelope):
        try:
            if set(envelope) != {"sequence", "public_key", "nonce", "ciphertext"}:
                raise ValueError
            sequence = envelope["sequence"]
            if type(sequence) is not int or sequence != self._sequence + 1:
                raise ValueError
            ciphertext = _decode(envelope["ciphertext"])
            nonce = _decode(envelope["nonce"])
            if len(ciphertext) > 33016 or len(nonce) != 12:
                raise ValueError
            plaintext = AESGCM(
                _key(self._private, _decode(envelope["public_key"]), self.session)
            ).decrypt(nonce, ciphertext, _aad(self.session, request_id, sequence))
            arguments = json.loads(plaintext)
            if not isinstance(arguments, dict) or set(arguments) != {"surface", "event"}:
                raise ValueError
            if arguments["surface"] not in {"browser", "desktop"} or not isinstance(
                arguments["event"], dict
            ):
                raise ValueError
            # Consume before dispatch: a failed or uncertain input cannot replay.
            self._sequence = sequence
            return arguments
        except Exception:
            raise ValueError("Private computer input rejected; no automatic retry.") from None
