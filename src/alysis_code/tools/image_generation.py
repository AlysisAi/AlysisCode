from __future__ import annotations

import base64
import binascii
import hashlib
import io
import ipaddress
import os
import socket
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..branding import env_get
from ..config import AppConfig
from ..error_text import sanitize_error_summary
from ..host_file_handles import guard_host_mutation, mkdir_host_path, rename_host_file_no_replace
from ..profiles import get_active_profile, resolve_effective_base_url

_OUTPUT_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_SIZES = frozenset({"auto", "1024x1024", "1536x1024", "1024x1536"})
_QUALITIES = frozenset({"auto", "low", "medium", "high"})
_BACKGROUNDS = frozenset({"auto", "opaque", "transparent"})


class ImageGenerationError(RuntimeError):
    pass


def _load_image_decoder() -> tuple[Any, type[OSError]]:
    from PIL import Image, UnidentifiedImageError

    return Image, UnidentifiedImageError


def plan_image_output_paths(
    *,
    root: Path,
    output_path: str,
    count: int,
) -> tuple[tuple[Path, str], ...]:
    root = root.resolve()
    raw = str(output_path or "").strip()
    if not raw:
        raise ImageGenerationError("output_path is required")
    requested = Path(raw)
    if requested.is_absolute():
        raise ImageGenerationError("output_path must be relative to the workspace root")
    if requested.suffix.lower() not in _OUTPUT_SUFFIXES:
        raise ImageGenerationError("output_path must end in .png, .jpg, .jpeg, or .webp")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 4:
        raise ImageGenerationError("count must be an integer from 1 to 4")

    candidates: list[Path] = []
    if count == 1:
        candidates.append(requested)
    else:
        candidates.extend(
            requested.with_name(f"{requested.stem}-{index}{requested.suffix}")
            for index in range(1, count + 1)
        )

    planned: list[tuple[Path, str]] = []
    for candidate in candidates:
        resolved = (root / candidate).resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ImageGenerationError("output_path escapes the workspace root") from exc
        planned.append((resolved, relative.as_posix()))
    return tuple(planned)


def generate_images(
    *,
    root: Path,
    cfg: AppConfig,
    fallback_api_key: str | None,
    prompt: str,
    output_path: str,
    count: int = 1,
    size: str = "auto",
    quality: str = "auto",
    background: str = "auto",
    timeout_s: float | None = None,
    transport: httpx.BaseTransport | None = None,
    publish: Callable[[Path, bytes], None] | None = None,
) -> dict[str, Any]:
    settings = cfg.image_generation
    if not settings.enabled:
        raise ImageGenerationError(
            "Image generation is disabled. Enable image_generation.enabled first."
        )
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise ImageGenerationError("prompt is required")
    if len(clean_prompt) > 32_000:
        raise ImageGenerationError("prompt exceeds the 32,000 character limit")
    if count > settings.max_images_per_call:
        raise ImageGenerationError(
            f"count exceeds configured max_images_per_call={settings.max_images_per_call}"
        )

    normalized_size = _enum_value(size, allowed=_SIZES, field="size")
    normalized_quality = _enum_value(quality, allowed=_QUALITIES, field="quality")
    normalized_background = _enum_value(
        background,
        allowed=_BACKGROUNDS,
        field="background",
    )
    planned = plan_image_output_paths(root=root, output_path=output_path, count=count)
    if normalized_background == "transparent" and any(
        target.suffix.lower() in {".jpg", ".jpeg"} for target, _ in planned
    ):
        raise ImageGenerationError("transparent background requires PNG or WebP output")
    existing = [relative for target, relative in planned if target.exists()]
    if existing:
        raise ImageGenerationError(
            "Refusing to overwrite existing image file(s): " + ", ".join(existing)
        )

    # Text-only startup does not need image codecs. An enabled image request
    # must still discover missing codecs before contacting the provider.
    _load_image_decoder()
    base_url, api_key, extra_headers = _resolve_runtime(
        cfg=cfg,
        fallback_api_key=fallback_api_key,
    )
    endpoint = _generation_endpoint(base_url)
    payload: dict[str, Any] = {
        "model": settings.model,
        "prompt": clean_prompt,
        "n": count,
    }
    if normalized_size != "auto":
        payload["size"] = normalized_size
    if normalized_quality != "auto":
        payload["quality"] = normalized_quality
    if normalized_background != "auto":
        payload["background"] = normalized_background
    if "gpt-image" in settings.model.casefold():
        payload["output_format"] = _provider_output_format(planned[0][0].suffix)

    safe_extra_headers = {
        str(name): str(value)
        for name, value in extra_headers.items()
        if str(name).strip().casefold()
        not in {"authorization", "content-length", "content-type", "host"}
    }
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "alysis-code/image-generation",
        **safe_extra_headers,
    }
    effective_timeout = float(timeout_s or settings.timeout_s)
    max_response_bytes = min(
        count * (((settings.max_image_bytes + 2) // 3) * 4) + 1_000_000,
        160_000_000,
    )
    started = time.perf_counter()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(effective_timeout),
            follow_redirects=False,
            transport=transport,
        ) as client:
            with client.stream("POST", endpoint, headers=headers, json=payload) as streamed:
                response = _bounded_response(
                    streamed,
                    max_bytes=max_response_bytes,
                    label="Image provider response",
                )
    except (httpx.HTTPError, OSError) as exc:
        raise ImageGenerationError(
            "Image provider request failed: " + sanitize_error_summary(str(exc))
        ) from exc
    response_payload = _response_json(response)
    if response.status_code < 200 or response.status_code >= 300:
        detail = _provider_error_detail(response_payload, response.text)
        raise ImageGenerationError(f"Image provider returned HTTP {response.status_code}: {detail}")

    data = response_payload.get("data")
    if not isinstance(data, list) or len(data) < count:
        raise ImageGenerationError(
            f"Image provider returned {len(data) if isinstance(data, list) else 0} "
            f"image(s); expected {count}"
        )

    endpoint_host = str(urlsplit(endpoint).hostname or "").casefold()
    prepared: list[tuple[Path, str, bytes, dict[str, Any]]] = []
    revised_prompts: list[str] = []
    for index, ((target, relative), item) in enumerate(zip(planned, data, strict=False), start=1):
        if not isinstance(item, dict):
            raise ImageGenerationError(f"Image provider result {index} is not an object")
        raw_bytes = _image_bytes_from_result(
            item=item,
            max_bytes=settings.max_image_bytes,
            endpoint_host=endpoint_host,
            timeout_s=effective_timeout,
            transport=transport,
        )
        normalized_bytes, metadata = _normalize_and_validate_image(
            raw_bytes=raw_bytes,
            output_suffix=target.suffix,
            max_bytes=settings.max_image_bytes,
            max_pixels=settings.max_pixels,
        )
        prepared.append((target, relative, normalized_bytes, metadata))
        revised_prompt = str(item.get("revised_prompt") or "").strip()
        if revised_prompt:
            revised_prompts.append(revised_prompt)

    committed: list[Path] = []
    try:
        for target, _relative, image_bytes, _metadata in prepared:
            (publish or _atomic_create_bytes)(target, image_bytes)
            committed.append(target)
    except Exception as exc:
        # A published pathname may already have been replaced by another
        # process. Host rollback would follow mutable ancestors outside the
        # publication sandbox. Preserve successful outputs and report them so
        # recovery never deletes an object we no longer own.
        if committed:
            retained = ", ".join(path.relative_to(root.resolve()).as_posix() for path in committed)
            raise ImageGenerationError(
                "Image publication failed; already-created outputs were retained: " + retained
            ) from exc
        raise

    images: list[dict[str, Any]] = []
    for _target, relative, image_bytes, metadata in prepared:
        images.append(
            {
                "path": relative,
                "bytes": len(image_bytes),
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
                **metadata,
            }
        )
    raw_usage = response_payload.get("usage")
    return {
        "status": "succeeded",
        "model": settings.model,
        "count": len(images),
        "images": images,
        "output_paths": [item["path"] for item in images],
        "size": normalized_size,
        "quality": normalized_quality,
        "background": normalized_background,
        "prompt_sha256": hashlib.sha256(clean_prompt.encode("utf-8")).hexdigest(),
        "revised_prompts": revised_prompts,
        "provider_request_id": str(response.headers.get("x-request-id") or "") or None,
        "provider_usage": raw_usage if isinstance(raw_usage, dict) else None,
        "elapsed_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "technical_validation": "decoded, dimension-bounded, format-normalized, atomically written",
    }


def _resolve_runtime(
    *,
    cfg: AppConfig,
    fallback_api_key: str | None,
) -> tuple[str, str, dict[str, str]]:
    settings = cfg.image_generation
    explicit_base_url = str(settings.base_url or "").strip()
    if explicit_base_url:
        base_url = explicit_base_url
        extra_headers: dict[str, str] = {}
    else:
        profile = get_active_profile(cfg)
        base_url = resolve_effective_base_url(cfg=cfg, profile=profile)
        extra_headers = dict(profile.extra_headers)

    configured_env = str(settings.api_key_env or "").strip()
    if configured_env:
        api_key = str(env_get(configured_env) or "").strip()
        if not api_key:
            raise ImageGenerationError(
                f"Image API key environment variable {configured_env!r} is not set"
            )
    else:
        api_key = str(env_get("ALYSIS_IMAGE_API_KEY") or fallback_api_key or "").strip()
    if not api_key:
        raise ImageGenerationError(
            "No image API key is available. Set ALYSIS_IMAGE_API_KEY or "
            "image_generation.api_key_env."
        )
    return base_url, api_key, extra_headers


def _generation_endpoint(base_url: str) -> str:
    clean = str(base_url or "").strip().rstrip("/")
    parts = urlsplit(clean)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ImageGenerationError("Image generation base URL must be HTTP(S)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ImageGenerationError("Image generation base URL cannot contain credentials or query")
    if parts.path.rstrip("/").endswith("/images/generations"):
        return clean
    return clean + "/images/generations"


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ImageGenerationError(
            f"Image provider returned invalid JSON (HTTP {response.status_code})"
        ) from exc
    if not isinstance(payload, dict):
        raise ImageGenerationError("Image provider response must be a JSON object")
    return payload


def _bounded_response(
    response: httpx.Response,
    *,
    max_bytes: int,
    label: str,
) -> httpx.Response:
    content_length = str(response.headers.get("content-length") or "").strip()
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = 0
        if declared_length > max_bytes:
            raise ImageGenerationError(f"{label} exceeds the configured byte limit")
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > max_bytes:
            raise ImageGenerationError(f"{label} exceeds the configured byte limit")
        body.extend(chunk)
    return httpx.Response(
        status_code=response.status_code,
        headers=response.headers,
        content=bytes(body),
        request=response.request,
        extensions=dict(response.extensions),
    )


def _provider_error_detail(payload: dict[str, Any], fallback: str) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        candidate = error.get("message") or error.get("detail") or error.get("code")
    else:
        candidate = error
    return sanitize_error_summary(str(candidate or fallback or "provider error"))


def _image_bytes_from_result(
    *,
    item: dict[str, Any],
    max_bytes: int,
    endpoint_host: str,
    timeout_s: float,
    transport: httpx.BaseTransport | None,
) -> bytes:
    encoded = item.get("b64_json")
    if isinstance(encoded, str) and encoded.strip():
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageGenerationError("Image provider returned invalid base64 image data") from exc
        if len(payload) > max_bytes:
            raise ImageGenerationError("Generated image exceeds configured max_image_bytes")
        return payload

    url = str(item.get("url") or "").strip()
    if not url:
        raise ImageGenerationError("Image provider result has neither b64_json nor url")
    return _download_image(
        url=url,
        endpoint_host=endpoint_host,
        max_bytes=max_bytes,
        timeout_s=timeout_s,
        transport=transport,
    )


def _download_image(
    *,
    url: str,
    endpoint_host: str,
    max_bytes: int,
    timeout_s: float,
    transport: httpx.BaseTransport | None,
) -> bytes:
    parts = urlsplit(url)
    host = str(parts.hostname or "").casefold()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise ImageGenerationError("Provider image URL must be credential-free HTTPS")
    if host != endpoint_host and not _public_network_host(host):
        raise ImageGenerationError("Provider image URL resolves to a non-public network address")
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            transport=transport,
        ) as client:
            with client.stream("GET", url, headers={"Accept": "image/*"}) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise ImageGenerationError(
                        f"Provider image download returned HTTP {response.status_code}"
                    )
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise ImageGenerationError(
                        "Generated image download exceeds configured max_image_bytes"
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageGenerationError(
                            "Generated image download exceeds configured max_image_bytes"
                        )
                    chunks.append(chunk)
    except ImageGenerationError:
        raise
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise ImageGenerationError(
            "Generated image download failed: " + sanitize_error_summary(str(exc))
        ) from exc
    return b"".join(chunks)


def _public_network_host(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return literal.is_global
    try:
        addresses = {
            ipaddress.ip_address(sockaddr[0])
            for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
                host,
                443,
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError):
        return False
    return bool(addresses) and all(address.is_global for address in addresses)


def _normalize_and_validate_image(
    *,
    raw_bytes: bytes,
    output_suffix: str,
    max_bytes: int,
    max_pixels: int,
) -> tuple[bytes, dict[str, Any]]:
    Image, UnidentifiedImageError = _load_image_decoder()
    try:
        with Image.open(io.BytesIO(raw_bytes)) as opened:
            width, height = opened.size
            if width < 1 or height < 1 or width * height > max_pixels:
                raise ImageGenerationError(
                    f"Generated image dimensions {width}x{height} exceed configured max_pixels"
                )
            opened.load()
            image = opened.copy()
    except ImageGenerationError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageGenerationError("Provider returned invalid or unsupported image bytes") from exc

    desired_format = _pillow_output_format(output_suffix)
    output = io.BytesIO()
    if desired_format == "JPEG":
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            image = background.convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.save(output, format="JPEG", quality=95, optimize=True)
    elif desired_format == "WEBP":
        image.save(output, format="WEBP", quality=95, method=6)
    else:
        image.save(output, format="PNG", optimize=True)
    normalized = output.getvalue()
    if len(normalized) > max_bytes:
        raise ImageGenerationError("Normalized image exceeds configured max_image_bytes")
    has_alpha = image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info)
    return normalized, {
        "width": width,
        "height": height,
        "format": desired_format.lower(),
        "mode": image.mode,
        "has_alpha": bool(has_alpha),
    }


def _atomic_create_bytes(path: Path, payload: bytes) -> None:
    guard_host_mutation(path)
    mkdir_host_path(path.parent)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if os.name == "nt":
                rename_host_file_no_replace(temp_path, path)
            else:
                os.link(temp_path, path)
        except FileExistsError as exc:
            raise ImageGenerationError(
                f"Refusing to overwrite existing image file: {path.name}"
            ) from exc
        except OSError:
            if os.name == "nt":
                # A denied guarded rename must not reopen a rejected pathname.
                raise
            # Filesystems without hard-link support still get an exclusive,
            # fully-written target. The fallback never overwrites an existing path.
            try:
                target_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError as exc:
                raise ImageGenerationError(
                    f"Refusing to overwrite existing image file: {path.name}"
                ) from exc
            try:
                with os.fdopen(target_fd, "wb") as target:
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
            except Exception:
                with suppress(OSError):
                    path.unlink()
                raise
    finally:
        with suppress(OSError):
            temp_path.unlink()


def _enum_value(value: str, *, allowed: frozenset[str], field: str) -> str:
    normalized = str(value or "auto").strip().lower() or "auto"
    if normalized not in allowed:
        raise ImageGenerationError(f"{field} must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _provider_output_format(suffix: str) -> str:
    return "jpeg" if suffix.lower() in {".jpg", ".jpeg"} else suffix.lower().lstrip(".")


def _pillow_output_format(suffix: str) -> str:
    return {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
        ".png": "PNG",
    }[suffix.lower()]
