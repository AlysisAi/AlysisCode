from __future__ import annotations

import base64
import builtins
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from alysis_code.config import AppConfig, ConfigError, set_config_value
from alysis_code.tools.image_generation import (
    ImageGenerationError,
    _bounded_response,
    generate_images,
    plan_image_output_paths,
)


def _png_bytes(*, size: tuple[int, int] = (7, 5)) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", size, (12, 34, 56, 128)).save(output, format="PNG")
    return output.getvalue()


def _enabled_config(**overrides: object) -> AppConfig:
    settings: dict[str, object] = {
        "enabled": True,
        "model": "gpt-image-test",
        "base_url": "https://images.example.test/v1",
    }
    settings.update(overrides)
    return AppConfig(model="text-model", image_generation=settings)


def test_plan_image_output_paths_is_workspace_bounded_and_deterministic(
    tmp_path: Path,
) -> None:
    planned = plan_image_output_paths(
        root=tmp_path,
        output_path="src/assets/hero.webp",
        count=3,
    )

    assert [relative for _path, relative in planned] == [
        "src/assets/hero-1.webp",
        "src/assets/hero-2.webp",
        "src/assets/hero-3.webp",
    ]
    assert all(path.is_relative_to(tmp_path) for path, _relative in planned)

    with pytest.raises(ImageGenerationError, match="escapes the workspace"):
        plan_image_output_paths(root=tmp_path, output_path="../hero.png", count=1)
    with pytest.raises(ImageGenerationError, match="must end in"):
        plan_image_output_paths(root=tmp_path, output_path="hero.svg", count=1)
    with pytest.raises(ImageGenerationError, match="integer from 1 to 4"):
        plan_image_output_paths(root=tmp_path, output_path="hero.png", count=True)


def test_generate_images_writes_validated_new_assets_and_returns_metadata(
    tmp_path: Path,
) -> None:
    raw_png = _png_bytes(size=(11, 9))
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(
            200,
            headers={"x-request-id": "image-request-123"},
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(raw_png).decode("ascii"),
                        "revised_prompt": "A refined production prompt",
                    }
                ],
                "usage": {"total_tokens": 42},
            },
        )

    result = generate_images(
        root=tmp_path,
        cfg=_enabled_config(),
        fallback_api_key="sk-test-image-credential-123456",
        prompt="A quiet geometric product illustration; no words or logos.",
        output_path="web/assets/empty-state.png",
        size="1024x1024",
        quality="high",
        background="transparent",
        transport=httpx.MockTransport(handler),
    )

    assert seen["url"] == "https://images.example.test/v1/images/generations"
    assert seen["authorization"] == "Bearer sk-test-image-credential-123456"
    assert result["status"] == "succeeded"
    assert result["output_paths"] == ["web/assets/empty-state.png"]
    assert result["provider_request_id"] == "image-request-123"
    assert result["provider_usage"] == {"total_tokens": 42}
    assert result["images"][0]["width"] == 11
    assert result["images"][0]["height"] == 9
    assert result["images"][0]["format"] == "png"
    assert result["images"][0]["has_alpha"] is True
    assert len(result["images"][0]["sha256"]) == 64
    assert (tmp_path / "web/assets/empty-state.png").is_file()
    with Image.open(tmp_path / "web/assets/empty-state.png") as generated:
        assert generated.size == (11, 9)
        assert generated.format == "PNG"


def test_generate_images_requires_decoder_before_provider_request(tmp_path, monkeypatch) -> None:
    original_import = builtins.__import__
    requests = []

    def without_decoder(name, *args, **kwargs):
        if name == "PIL":
            raise ImportError("image decoder unavailable")
        return original_import(name, *args, **kwargs)

    def handler(request):
        requests.append(request)
        return httpx.Response(500)

    monkeypatch.setattr(builtins, "__import__", without_decoder)
    with pytest.raises(ImportError, match="image decoder unavailable"):
        generate_images(
            root=tmp_path,
            cfg=_enabled_config(),
            fallback_api_key="fixture-only",
            prompt="A synthetic test image",
            output_path="image.png",
            transport=httpx.MockTransport(handler),
        )
    assert requests == []
    assert not (tmp_path / "image.png").exists()


def test_partial_publication_retains_output_without_host_path_rollback(tmp_path, monkeypatch):
    raw = _png_bytes()
    calls = []

    def publish(target, payload):
        if calls:
            raise OSError("synthetic second publication failure")
        target.write_bytes(payload)
        calls.append(target)

    def forbidden_unlink(*args, **kwargs):
        raise AssertionError("published pathname must not be deleted by the host")

    monkeypatch.setattr(Path, "unlink", forbidden_unlink)
    with pytest.raises(
        ImageGenerationError, match="already-created outputs were retained: image-1.png"
    ):
        generate_images(
            root=tmp_path,
            cfg=_enabled_config(),
            fallback_api_key="synthetic",
            prompt="synthetic",
            output_path="image.png",
            count=2,
            publish=publish,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"data": [{"b64_json": base64.b64encode(raw).decode()}] * 2}
                )
            ),
        )
    assert calls[0].is_file()


def test_generate_images_never_calls_provider_when_output_exists(tmp_path: Path) -> None:
    target = tmp_path / "assets/existing.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(_png_bytes())
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    with pytest.raises(ImageGenerationError, match="Refusing to overwrite"):
        generate_images(
            root=tmp_path,
            cfg=_enabled_config(),
            fallback_api_key="test-key",
            prompt="Anything",
            output_path="assets/existing.png",
            transport=httpx.MockTransport(handler),
        )

    assert called is False


def test_generate_images_rejects_invalid_provider_bytes_without_partial_file(
    tmp_path: Path,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(b"not-an-image").decode()}]},
        )

    with pytest.raises(ImageGenerationError, match="invalid or unsupported"):
        generate_images(
            root=tmp_path,
            cfg=_enabled_config(),
            fallback_api_key="test-key",
            prompt="Anything",
            output_path="assets/broken.png",
            transport=httpx.MockTransport(handler),
        )

    assert not (tmp_path / "assets/broken.png").exists()


def test_generate_images_redacts_provider_errors(tmp_path: Path) -> None:
    leaked = "sk-provider-secret-1234567890"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"bad key {leaked}"}})

    with pytest.raises(ImageGenerationError) as raised:
        generate_images(
            root=tmp_path,
            cfg=_enabled_config(),
            fallback_api_key="test-key",
            prompt="Anything",
            output_path="assets/rejected.png",
            transport=httpx.MockTransport(handler),
        )

    assert leaked not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


def test_provider_response_body_is_bounded_before_json_parsing() -> None:
    response = httpx.Response(
        200,
        content=b"12345",
        request=httpx.Request("POST", "https://images.example.test/v1/images/generations"),
    )

    with pytest.raises(ImageGenerationError, match="configured byte limit"):
        _bounded_response(response, max_bytes=4, label="Image provider response")


def test_image_generation_configuration_is_opt_in_and_validated() -> None:
    cfg = AppConfig()
    assert cfg.image_generation.enabled is False

    cfg = set_config_value(cfg, "image_generation.enabled", "true")
    cfg = set_config_value(cfg, "image_generation.model", "provider/image-v2")
    cfg = set_config_value(cfg, "image_generation.api_key_env", "IMAGE_PROVIDER_KEY")
    cfg = set_config_value(cfg, "image_generation.timeout_s", "240")
    cfg = set_config_value(cfg, "image_generation.max_images_per_call", "2")

    assert cfg.image_generation.enabled is True
    assert cfg.image_generation.model == "provider/image-v2"
    assert cfg.image_generation.api_key_env == "IMAGE_PROVIDER_KEY"
    assert cfg.image_generation.timeout_s == 240.0
    assert cfg.image_generation.max_images_per_call == 2

    with pytest.raises(ConfigError):
        set_config_value(cfg, "image_generation.api_key_env", "NOT VALID")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "image_generation.timeout_s", "901")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "image_generation.max_images_per_call", "5")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "image_generation.max_pixels", "67108865")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "image_generation.base_url", "https://user:secret@example.test/v1")
