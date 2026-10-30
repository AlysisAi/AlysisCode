"""Access to the authoritative Forge Host Protocol v2 JSON Schema."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .errors import ProtocolValidationError
from .models import PROTOCOL_VERSION

SCHEMA_RELATIVE_PATH = Path("forge_host_protocol/alysis-code.schema.json")


@lru_cache(maxsize=1)
def schema_path() -> Path:
    """Locate the authoritative schema in a source tree or installed wheel."""

    candidate = Path(__file__).with_name("alysis-code.schema.json")
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"Forge host schema not found: {SCHEMA_RELATIVE_PATH}")


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """Load and minimally integrity-check the single wire schema source."""

    try:
        value = json.loads(schema_path().read_text(encoding="utf-8"))
        schema_version = value["$defs"]["protocolVersion"]["const"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProtocolValidationError(
            "invalid_schema", "Forge host protocol schema cannot be loaded"
        ) from exc
    if schema_version != PROTOCOL_VERSION:
        raise ProtocolValidationError(
            "schema_version_mismatch",
            "Forge host schema and Python protocol versions do not match",
        )
    return value


def schema_sha256() -> str:
    """Return a stable digest that adapters can pin during integration tests."""

    canonical = json.dumps(load_schema(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
