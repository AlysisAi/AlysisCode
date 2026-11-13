"""Bounded binary library transport. Payloads never enter model context."""

import base64
import binascii

from pydantic import Field, field_validator

from .models import FileInput, Input

MAX_FILE_BYTES = 50 * 1024 * 1024
INLINE_FILE_BYTES = 2 * 1024 * 1024
FILE_CHUNK_BYTES = 256 * 1024
MAX_ENCODED_BYTES = 4 * ((INLINE_FILE_BYTES + 2) // 3)


def decode_file(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_ENCODED_BYTES:
        raise ValueError("File exceeds the 2 MiB transfer limit.")
    try:
        payload = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid file encoding.") from None
    if len(payload) > INLINE_FILE_BYTES:
        raise ValueError("File exceeds the 2 MiB transfer limit.")
    return payload


class BinaryFileInput(Input):
    previous_file_id: str | None = Field(default=None, min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=160)
    data_base64: str = Field(max_length=MAX_ENCODED_BYTES)

    @field_validator("name")
    @classmethod
    def simple_name(cls, value):
        return FileInput(name=value, content="").name

    @field_validator("data_base64")
    @classmethod
    def valid_payload(cls, value):
        decode_file(value)
        return value


class FileCursor(Input):
    created_at: float = Field(ge=0, allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=64)


class FileQuery(Input):
    include_versions: bool = Field(default=False, strict=True)
    series_id: str | None = Field(default=None, min_length=1, max_length=64)
    query: str = Field(default="", max_length=500)
    before: FileCursor | None = None
    limit: int = Field(default=20, ge=1, le=100, strict=True)
    source_task_id: str | None = Field(default=None, min_length=1, max_length=64)
