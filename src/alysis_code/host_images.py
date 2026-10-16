from __future__ import annotations

import hashlib
import hmac
import math
import mimetypes
import os
import re
import stat
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

from .branding import env_get

IDE_HOST_IMAGE_ROOT_ENV = "ALYSIS_IDE_HOST_IMAGE_ROOT"
IDE_HOST_IMAGE_SHA256_ENV = "ALYSIS_IDE_HOST_IMAGE_SHA256"
IDE_HOST_IMAGE_BYTE_LENGTH_ENV = "ALYSIS_IDE_HOST_IMAGE_BYTE_LENGTH"
IDE_HOST_IMAGE_ENV_KEYS = frozenset(
    {
        IDE_HOST_IMAGE_ROOT_ENV,
        IDE_HOST_IMAGE_SHA256_ENV,
        IDE_HOST_IMAGE_BYTE_LENGTH_ENV,
    }
)

_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_BYTE_LENGTH_PATTERN = re.compile(r"^[1-9][0-9]{0,19}$")
_DATA_IMAGE_URL_PATTERN = re.compile(
    r"data:image/[a-z0-9.+-]+(?:;[a-z0-9=.+-]+)*;base64,[a-z0-9+/=_-]+",
    re.IGNORECASE,
)
HOST_SCREEN_REDACTION_MARKER = "[screen image omitted]"


class HostImageValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ValidatedHostImage:
    path: Path
    root: Path
    mime_type: str
    size_bytes: int
    sha256: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class HostImageCoordinateTransform:
    width_pixels: int
    height_pixels: int
    backing_scale: float
    logical_x: float
    logical_y: float
    logical_width: float
    logical_height: float
    coordinate_space: str = "global_display_points"
    pixel_origin: str = "top-left"


@dataclass(frozen=True, slots=True)
class HostImageBinding:
    """One-shot, request-scoped authority to read one staged host image."""

    root: Path
    sha256: str
    byte_length: int
    coordinate_transform: HostImageCoordinateTransform


_ACTIVE_IDE_HOST_IMAGE_BINDING: ContextVar[HostImageBinding | None] = ContextVar(
    "active_ide_host_image_binding",
    default=None,
)


_PREPARED_HOST_IMAGE: ContextVar[tuple[ValidatedHostImage, bytes] | None] = ContextVar(
    "prepared_host_image", default=None
)


def prepared_host_image(raw_path: str) -> tuple[ValidatedHostImage, bytes] | None:
    """A verified, request-local image may outlive its deleted staging file."""
    prepared = _PREPARED_HOST_IMAGE.get()
    binding = _ACTIVE_IDE_HOST_IMAGE_BINDING.get()
    if prepared is None or binding is None:
        return None
    image, raw = prepared
    if (
        Path(raw_path) != image.path
        or image.root != binding.root
        or image.sha256 != binding.sha256
        or len(raw) != binding.byte_length
    ):
        _raise("host_image_binding_mismatch", "Prepared image does not match this turn.")
    return prepared


class HostScreenRedactor:
    """Turn-scoped redaction for host paths and encoded screen pixels."""

    def __init__(self) -> None:
        self._paths: set[str] = set()
        self._payloads: set[str] = set()

    def add_path(self, value: str | os.PathLike[str]) -> None:
        raw = os.fspath(value)
        if not raw:
            return
        self._paths.add(raw)
        self._paths.add(os.path.realpath(raw))

    def add_data_url(self, value: str) -> None:
        raw = str(value or "")
        if not raw:
            return
        self._payloads.add(raw)
        separator_index = raw.find(",")
        if separator_index >= 0 and separator_index + 1 < len(raw):
            self._payloads.add(raw[separator_index + 1 :])

    def contains_sensitive_material(self, value: Any) -> bool:
        return any(self._text_contains_sensitive_material(text) for text in self._strings(value))

    def redact_text(self, value: str) -> str:
        redacted = _DATA_IMAGE_URL_PATTERN.sub(HOST_SCREEN_REDACTION_MARKER, str(value))
        for payload in sorted(self._payloads, key=len, reverse=True):
            if payload:
                redacted = redacted.replace(payload, HOST_SCREEN_REDACTION_MARKER)
        for path in sorted(self._paths, key=len, reverse=True):
            if path:
                redacted = re.sub(
                    re.escape(path),
                    lambda _match: HOST_SCREEN_REDACTION_MARKER,
                    redacted,
                    flags=re.IGNORECASE,
                )
        return redacted

    def redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {self.redact_value(key): self.redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            replacements = {
                field.name: self.redact_value(getattr(value, field.name)) for field in fields(value)
            }
            try:
                return replace(value, **replacements)
            except (TypeError, ValueError):
                return value
        return value

    def _text_contains_sensitive_material(self, text: str) -> bool:
        lowered = text.casefold()
        if "data:image/" in lowered or ";base64," in lowered:
            return True
        if any(path and path.casefold() in lowered for path in self._paths):
            return True
        return any(payload and payload in text for payload in self._payloads)

    def _strings(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            nested: list[str] = []
            for key, item in value.items():
                nested.extend(self._strings(key))
                nested.extend(self._strings(item))
            return nested
        if isinstance(value, (list, tuple)):
            nested = []
            for item in value:
                nested.extend(self._strings(item))
            return nested
        if is_dataclass(value) and not isinstance(value, type):
            nested = []
            for field in fields(value):
                nested.extend(self._strings(getattr(value, field.name)))
            return nested
        return []


def _raise(code: str, message: str) -> None:
    raise HostImageValidationError(code, message)


def without_ide_host_image_env(env: Mapping[str, str]) -> dict[str, str]:
    """Remove main-process-only host-image capabilities from a child environment."""

    blocked = {key.upper() for key in IDE_HOST_IMAGE_ENV_KEYS}
    return {key: value for key, value in env.items() if key.upper() not in blocked}


def parse_host_image_binding_descriptor(
    descriptor: Mapping[str, Any],
    *,
    max_bytes: int,
) -> HostImageBinding:
    """Validate an authenticated IDE descriptor without retaining its raw payload."""

    if not isinstance(descriptor, Mapping):
        _raise("host_image_binding_invalid", "Host image binding descriptor is invalid.")
    expected_keys = {"root", "sha256", "byte_length", "coordinate_transform"}
    if set(descriptor) != expected_keys:
        _raise("host_image_binding_invalid", "Host image binding descriptor is invalid.")

    raw_root = descriptor.get("root")
    raw_digest = descriptor.get("sha256")
    raw_size = descriptor.get("byte_length")
    raw_transform = descriptor.get("coordinate_transform")
    if not isinstance(raw_root, str) or not isinstance(raw_digest, str):
        _raise("host_image_binding_invalid", "Host image binding descriptor is invalid.")
    if isinstance(raw_size, bool) or not isinstance(raw_size, int):
        _raise("host_image_binding_invalid", "Host image binding descriptor is invalid.")
    root = _canonical_absolute_path(
        raw_root,
        code="host_image_root_invalid",
        label="Host image root",
    )
    if raw_size <= 0 or raw_size > max_bytes:
        _raise(
            "host_image_binding_invalid",
            "Host image byte-length binding is outside the supported range.",
        )
    if _SHA256_PATTERN.fullmatch(raw_digest) is None:
        _raise("host_image_binding_invalid", "Host image digest binding is invalid.")
    transform = _parse_host_image_coordinate_transform(raw_transform)
    return HostImageBinding(
        root=root,
        sha256=raw_digest,
        byte_length=raw_size,
        coordinate_transform=transform,
    )


def _parse_host_image_coordinate_transform(value: Any) -> HostImageCoordinateTransform:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "width_pixels",
        "height_pixels",
        "backing_scale",
        "logical_bounds",
        "coordinate_space",
        "pixel_origin",
    }:
        _raise("host_image_binding_invalid", "Host image coordinate transform is invalid.")
    bounds = value.get("logical_bounds")
    if not isinstance(bounds, Mapping) or set(bounds) != {"x", "y", "width", "height"}:
        _raise("host_image_binding_invalid", "Host image coordinate transform is invalid.")

    width_pixels = _strict_bounded_int(value.get("width_pixels"), minimum=1, maximum=8192)
    height_pixels = _strict_bounded_int(value.get("height_pixels"), minimum=1, maximum=8192)
    backing_scale = _finite_number(value.get("backing_scale"), maximum=4.0)
    logical_x = _finite_number(bounds.get("x"), maximum=100_000.0, allow_negative=True)
    logical_y = _finite_number(bounds.get("y"), maximum=100_000.0, allow_negative=True)
    logical_width = _finite_number(bounds.get("width"), maximum=100_000.0)
    logical_height = _finite_number(bounds.get("height"), maximum=100_000.0)
    if (
        isinstance(value.get("schema_version"), bool)
        or value.get("schema_version") != 1
        or value.get("coordinate_space") != "global_display_points"
        or value.get("pixel_origin") != "top-left"
        or backing_scale < 1.0
        or logical_width <= 0
        or logical_height <= 0
        or abs(width_pixels - logical_width * backing_scale) > 2.0
        or abs(height_pixels - logical_height * backing_scale) > 2.0
    ):
        _raise("host_image_binding_invalid", "Host image coordinate transform is invalid.")
    return HostImageCoordinateTransform(
        width_pixels=width_pixels,
        height_pixels=height_pixels,
        backing_scale=backing_scale,
        logical_x=logical_x,
        logical_y=logical_y,
        logical_width=logical_width,
        logical_height=logical_height,
    )


def _strict_bounded_int(value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _raise("host_image_binding_invalid", "Host image coordinate transform is invalid.")
    return value


def _finite_number(value: Any, *, maximum: float, allow_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _raise("host_image_binding_invalid", "Host image coordinate transform is invalid.")
    result = float(value)
    if not math.isfinite(result) or abs(result) > maximum or (not allow_negative and result < 0):
        _raise("host_image_binding_invalid", "Host image coordinate transform is invalid.")
    return result


def active_host_image_coordinate_instruction() -> str | None:
    """Return safe, path-free pixel-to-point guidance for the current one-shot image."""

    binding = _ACTIVE_IDE_HOST_IMAGE_BINDING.get()
    if binding is None:
        return None
    transform = binding.coordinate_transform
    return (
        "[Trusted host screen coordinate map: the image is "
        f"{transform.width_pixels}x{transform.height_pixels} pixels with a top-left origin. "
        "It covers logical bounds "
        f"x={transform.logical_x:g}, y={transform.logical_y:g}, "
        f"width={transform.logical_width:g}, height={transform.logical_height:g} "
        f"in {transform.coordinate_space} at backing scale {transform.backing_scale:g}. "
        "Map image pixel (px, py) to an action point as "
        f"({transform.logical_x:g} + px / {transform.backing_scale:g}, "
        f"{transform.logical_y:g} + py / {transform.backing_scale:g}). "
        "Prefer fresh semantic element IDs over coordinates.]"
    )


@contextmanager
def use_ide_host_image_binding(
    binding: HostImageBinding, *, prepared: tuple[ValidatedHostImage, bytes] | None = None
) -> Iterator[None]:
    """Scope a host-image capability to the current authenticated request context."""

    if not isinstance(binding, HostImageBinding):
        _raise("host_image_binding_invalid", "Host image binding descriptor is invalid.")
    token = _ACTIVE_IDE_HOST_IMAGE_BINDING.set(binding)
    prepared_token = _PREPARED_HOST_IMAGE.set(prepared)
    try:
        yield
    finally:
        _PREPARED_HOST_IMAGE.reset(prepared_token)
        _ACTIVE_IDE_HOST_IMAGE_BINDING.reset(token)


def configured_ide_host_image_root() -> Path | None:
    """Return the active request root, or the legacy main-process root if present."""

    active_binding = _ACTIVE_IDE_HOST_IMAGE_BINDING.get()
    if active_binding is not None:
        return active_binding.root
    raw_root = env_get(IDE_HOST_IMAGE_ROOT_ENV, "") or ""
    return Path(raw_root) if raw_root else None


def _canonical_absolute_path(
    raw_path: str | os.PathLike[str],
    *,
    code: str,
    label: str,
) -> Path:
    raw_text = os.fspath(raw_path)
    candidate = Path(raw_text)
    if not raw_text or not candidate.is_absolute():
        _raise(code, f"{label} must be an absolute canonical path.")
    normalized = os.path.normpath(raw_text)
    if normalized != raw_text:
        _raise(code, f"{label} must not contain traversal or non-canonical components.")
    return Path(normalized)


def _reject_symlink_components(path: Path, *, code: str, label: str) -> None:
    anchor = Path(path.anchor)
    current = anchor
    for component in path.parts[1:]:
        current = current / component
        try:
            inspected = os.lstat(current)
        except FileNotFoundError:
            _raise(code, f"{label} does not exist.")
        except OSError:
            _raise(code, f"{label} could not be inspected safely.")
        if stat.S_ISLNK(inspected.st_mode):
            _raise("host_image_symlink_rejected", f"{label} may not contain symlinks.")


def _require_private_posix_object(
    inspected: os.stat_result,
    *,
    code: str,
    label: str,
) -> None:
    if os.name != "posix":
        return
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and inspected.st_uid != getuid():
        _raise("host_image_owner_mismatch", f"{label} must be owned by the current user.")
    if stat.S_IMODE(inspected.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
        _raise(code, f"{label} must not grant group or other permissions.")


def resolve_ide_host_image_root() -> Path:
    configured_root = configured_ide_host_image_root()
    raw_root = os.fspath(configured_root) if configured_root is not None else ""
    if not raw_root:
        _raise(
            "host_image_root_missing",
            f"{IDE_HOST_IMAGE_ROOT_ENV} is required for host screen images.",
        )
    root = _canonical_absolute_path(
        raw_root,
        code="host_image_root_invalid",
        label="Host image root",
    )
    _reject_symlink_components(
        root,
        code="host_image_root_invalid",
        label="Host image root",
    )
    try:
        canonical = root.resolve(strict=True)
        inspected = os.lstat(root)
    except OSError:
        _raise("host_image_root_invalid", "Host image root could not be inspected safely.")
    if canonical != root or not stat.S_ISDIR(inspected.st_mode):
        _raise(
            "host_image_root_invalid",
            "Host image root must be a canonical, non-symlink directory.",
        )
    _require_private_posix_object(
        inspected,
        code="host_image_root_insecure",
        label="Host image root",
    )
    return root


def _resolve_ide_host_image_binding(*, max_bytes: int) -> tuple[int, str]:
    active_binding = _ACTIVE_IDE_HOST_IMAGE_BINDING.get()
    if active_binding is not None:
        expected_size = active_binding.byte_length
        if expected_size <= 0 or expected_size > max_bytes:
            _raise(
                "host_image_binding_invalid",
                "Host image byte-length binding is outside the supported range.",
            )
        if _SHA256_PATTERN.fullmatch(active_binding.sha256) is None:
            _raise("host_image_binding_invalid", "Host image digest binding is invalid.")
        return expected_size, active_binding.sha256

    raw_size = env_get(IDE_HOST_IMAGE_BYTE_LENGTH_ENV, "") or ""
    raw_digest = env_get(IDE_HOST_IMAGE_SHA256_ENV, "") or ""
    if not raw_size or not raw_digest:
        _raise(
            "host_image_binding_missing",
            "Host image integrity binding is required.",
        )
    if _BYTE_LENGTH_PATTERN.fullmatch(raw_size) is None:
        _raise(
            "host_image_binding_invalid",
            "Host image byte-length binding is invalid.",
        )
    expected_size = int(raw_size, 10)
    if expected_size <= 0 or expected_size > max_bytes:
        _raise(
            "host_image_binding_invalid",
            "Host image byte-length binding is outside the supported range.",
        )
    if _SHA256_PATTERN.fullmatch(raw_digest) is None:
        _raise(
            "host_image_binding_invalid",
            "Host image digest binding is invalid.",
        )
    return expected_size, raw_digest


def validate_ide_host_image(
    raw_path: str | os.PathLike[str],
    *,
    max_bytes: int,
    supported_mime_types: Collection[str],
) -> ValidatedHostImage:
    root = resolve_ide_host_image_root()
    expected_size, expected_digest = _resolve_ide_host_image_binding(max_bytes=max_bytes)
    candidate = _canonical_absolute_path(
        raw_path,
        code="host_image_path_invalid",
        label="Host image path",
    )
    try:
        candidate.relative_to(root)
    except ValueError:
        _raise(
            "host_image_path_outside_root",
            "Host image path must stay inside the configured host image root.",
        )
    if candidate == root:
        _raise("image_not_file", "Host image path must be a regular file.")
    _reject_symlink_components(
        candidate,
        code="image_not_found",
        label="Host image path",
    )
    try:
        canonical = candidate.resolve(strict=True)
        inspected = os.lstat(candidate)
    except FileNotFoundError:
        _raise("image_not_found", "Host image path does not exist.")
    except OSError:
        _raise("image_not_found", "Host image path could not be inspected safely.")
    if canonical != candidate:
        _raise(
            "host_image_path_invalid",
            "Host image path must be canonical and may not traverse symlinks.",
        )
    if not stat.S_ISREG(inspected.st_mode):
        _raise("image_not_file", "Host image path must be a regular file.")
    _require_private_posix_object(
        inspected,
        code="host_image_file_insecure",
        label="Host image file",
    )
    size = int(inspected.st_size)
    if size != expected_size:
        _raise(
            "host_image_size_mismatch",
            "Host image size does not match its integrity binding.",
        )
    if size > max_bytes:
        _raise(
            "image_too_large",
            f"Image exceeds the maximum supported size ({max_bytes} bytes).",
        )
    mime_type, _encoding = mimetypes.guess_type(os.fspath(candidate))
    if mime_type not in supported_mime_types:
        _raise("unsupported_image_type", "Unsupported image type.")
    return ValidatedHostImage(
        path=candidate,
        root=root,
        mime_type=mime_type,
        size_bytes=size,
        sha256=expected_digest,
        device=int(inspected.st_dev),
        inode=int(inspected.st_ino),
    )


def read_validated_host_image(
    image: ValidatedHostImage,
    *,
    max_bytes: int,
) -> bytes:
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags |= nofollow
    try:
        descriptor = os.open(image.path, flags)
    except OSError as exc:
        raise HostImageValidationError(
            "image_not_found",
            "Host image path could not be opened safely.",
        ) from exc
    try:
        inspected = os.fstat(descriptor)
        if not stat.S_ISREG(inspected.st_mode):
            _raise("image_not_file", "Host image path must be a regular file.")
        if (int(inspected.st_dev), int(inspected.st_ino)) != (image.device, image.inode):
            _raise("host_image_changed", "Host image changed before it could be read.")
        if int(inspected.st_size) != image.size_bytes:
            _raise("host_image_changed", "Host image changed before it could be read.")
        _require_private_posix_object(
            inspected,
            code="host_image_file_insecure",
            label="Host image file",
        )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(raw) > max_bytes:
        _raise(
            "image_too_large",
            f"Image exceeds the maximum supported size ({max_bytes} bytes).",
        )
    if len(raw) != image.size_bytes:
        _raise("host_image_changed", "Host image changed before it could be read.")
    digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(digest, image.sha256):
        _raise(
            "host_image_digest_mismatch",
            "Host image content does not match its integrity binding.",
        )
    return raw


def is_configured_host_image_candidate(
    raw_path: str | os.PathLike[str],
    *,
    workspace_root: Path,
) -> bool:
    configured_root = configured_ide_host_image_root()
    raw_root = os.fspath(configured_root) if configured_root is not None else ""
    if not raw_root:
        return False
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    # The host capability root is authoritative even when it overlaps the
    # workspace. Classifying by workspace containment first would downgrade a
    # host-owned screen capture to an ordinary workspace image and bypass its
    # integrity, privacy, and turn-scoping controls.
    try:
        candidate.resolve(strict=False).relative_to(
            Path(raw_root).expanduser().resolve(strict=False)
        )
    except ValueError:
        pass
    else:
        return True
    try:
        candidate.resolve(strict=False).relative_to(workspace_root.resolve())
    except ValueError:
        return True
    return False


__all__ = [
    "HOST_SCREEN_REDACTION_MARKER",
    "IDE_HOST_IMAGE_BYTE_LENGTH_ENV",
    "IDE_HOST_IMAGE_ENV_KEYS",
    "IDE_HOST_IMAGE_ROOT_ENV",
    "IDE_HOST_IMAGE_SHA256_ENV",
    "HostImageBinding",
    "HostImageCoordinateTransform",
    "HostImageValidationError",
    "HostScreenRedactor",
    "ValidatedHostImage",
    "active_host_image_coordinate_instruction",
    "configured_ide_host_image_root",
    "is_configured_host_image_candidate",
    "parse_host_image_binding_descriptor",
    "read_validated_host_image",
    "resolve_ide_host_image_root",
    "validate_ide_host_image",
    "use_ide_host_image_binding",
    "without_ide_host_image_env",
]
