"""First-party account discovery. Never emit a plaintext account credential."""

from __future__ import annotations

import base64
import json
import re
import sys
from urllib.parse import urlsplit

import httpx

from .. import alysis_cloud
from ..config import load_persisted_profile_keys

ACCOUNT_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
DEVICE_KEY = re.compile(r"slk_[A-Za-z0-9_-]{1,256}")


class CloudAccessError(RuntimeError):
    def __init__(self, status="unavailable"):
        self.status = status
        super().__init__(status)


def discover(key, *, client=None):
    if not isinstance(key, str) or not DEVICE_KEY.fullmatch(key):
        raise CloudAccessError("sign_in_required")
    try:
        # Only the product's account endpoint receives this device credential.
        response = (client or httpx).get(
            alysis_cloud.cloud_bots_url(),
            headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
            timeout=8,
            follow_redirects=False,
        )
        if response.status_code == 401:
            raise CloudAccessError("sign_in_required")
        if response.status_code != 200 or len(response.content) > 65536:
            raise CloudAccessError()
        value = response.json()
        if (
            value.get("schema_version") != 1
            or value.get("status") not in ("available", "upgrade_required")
            or not ACCOUNT_ID.fullmatch(value.get("account_id", ""))
            or not isinstance(value.get("plan"), str)
        ):
            raise CloudAccessError()
        result = {k: value[k] for k in ("status", "account_id", "plan")}
        if "storage_limit_bytes" in value:
            limit = value["storage_limit_bytes"]
            if type(limit) is not int or not 0 <= limit <= 2**63 - 1:
                raise CloudAccessError()
            result["storage_limit_bytes"] = limit
        for field in ("address", "account_url"):
            if value.get(field):
                url = urlsplit(value[field])
                if (
                    url.scheme != "https"
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                    or (field == "address" and url.path not in ("", "/"))
                ):
                    raise CloudAccessError()
                result[field] = value[field].rstrip("/")
        if value.get("certificate"):
            cert = value["certificate"]
            if not isinstance(cert, str) or len(cert) > 32768 or "PRIVATE KEY" in cert:
                raise CloudAccessError()
            result["certificate"] = cert
        return result
    except CloudAccessError:
        raise
    except Exception:
        raise CloudAccessError() from None


def desktop_access(
    *, connect=False, stream=None, key_loader=load_persisted_profile_keys, client=None
):
    try:
        key = key_loader().get(alysis_cloud.PROFILE_KEY)
        value = discover(key, client=client)
        if connect and value["status"] == "available" and value.get("address"):
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa

            raw = (stream or sys.stdin.buffer).readline(8193)
            if len(raw) > 8192:
                raise CloudAccessError()
            public = serialization.load_pem_public_key(json.loads(raw)["public_key"].encode())
            if not isinstance(public, rsa.RSAPublicKey) or public.key_size != 2048:
                raise CloudAccessError()
            sealed = public.encrypt(
                key.encode(),
                padding.OAEP(
                    mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
                ),
            )
            value["sealed_credential"] = base64.b64encode(sealed).decode()
        return {"protocol": 1, **value}
    except CloudAccessError as exc:
        return {"protocol": 1, "status": exc.status}
    except Exception:
        return {"protocol": 1, "status": "unavailable"}
