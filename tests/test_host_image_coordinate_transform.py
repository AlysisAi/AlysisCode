from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.prompt_context import _image_attachment_instruction_text
from alysis_code.config import ConfigError
from alysis_code.host_images import (
    HostImageValidationError,
    active_host_image_coordinate_instruction,
    parse_host_image_binding_descriptor,
    use_ide_host_image_binding,
)


def _descriptor(root: Path) -> dict[str, Any]:
    return {
        "root": str(root.resolve()),
        "sha256": "a" * 64,
        "byte_length": 42,
        "coordinate_transform": {
            "schema_version": 1,
            "width_pixels": 800,
            "height_pixels": 600,
            "backing_scale": 2,
            "logical_bounds": {
                "x": -100,
                "y": 50,
                "width": 400,
                "height": 300,
            },
            "coordinate_space": "global_display_points",
            "pixel_origin": "top-left",
        },
    }


def test_bound_transform_generates_exact_path_free_pixel_to_point_guidance(
    tmp_path: Path,
) -> None:
    binding = parse_host_image_binding_descriptor(
        _descriptor(tmp_path),
        max_bytes=1024,
    )

    assert active_host_image_coordinate_instruction() is None
    with use_ide_host_image_binding(binding):
        guidance = active_host_image_coordinate_instruction()
        message = _image_attachment_instruction_text("Operate the visible Lab.", image_count=1)

    assert guidance is not None
    assert "800x600 pixels" in guidance
    assert "global_display_points" in guidance
    assert "(-100 + px / 2, 50 + py / 2)" in guidance
    assert str(tmp_path) not in guidance
    assert guidance in message
    assert active_host_image_coordinate_instruction() is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("coordinate_transform"),
        lambda value: value["coordinate_transform"].__setitem__("extra", True),
        lambda value: value["coordinate_transform"].__setitem__("schema_version", True),
        lambda value: value["coordinate_transform"].__setitem__("width_pixels", 805),
        lambda value: value["coordinate_transform"].__setitem__("backing_scale", float("nan")),
        lambda value: value["coordinate_transform"].__setitem__("pixel_origin", "bottom-left"),
        lambda value: value["coordinate_transform"]["logical_bounds"].__setitem__("width", -400),
    ],
)
def test_transform_parser_rejects_missing_extra_inconsistent_and_nonfinite_values(
    tmp_path: Path,
    mutate: Any,
) -> None:
    descriptor = deepcopy(_descriptor(tmp_path))
    mutate(descriptor)

    with pytest.raises(HostImageValidationError) as raised:
        parse_host_image_binding_descriptor(descriptor, max_bytes=1024)

    assert raised.value.code == "host_image_binding_invalid"
    assert str(tmp_path) not in raised.value.message


def test_prepared_image_survives_staging_deletion_only_in_its_bound_turn(tmp_path: Path) -> None:
    import hashlib

    from alysis_code.agent.prompt_context import _build_user_message
    from alysis_code.host_images import read_validated_host_image, validate_ide_host_image

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    image_path = root / "capture.png"
    raw = b"\x89PNG\r\n\x1a\nsynthetic-image"
    image_path.write_bytes(raw)
    image_path.chmod(0o600)
    descriptor = _descriptor(root)
    descriptor.update(sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw))
    binding = parse_host_image_binding_descriptor(descriptor, max_bytes=1024)
    with use_ide_host_image_binding(binding):
        image = validate_ide_host_image(
            str(image_path), max_bytes=1024, supported_mime_types={"image/png"}
        )
        prepared = (image, read_validated_host_image(image, max_bytes=1024))
    image_path.unlink()
    root.rmdir()
    with use_ide_host_image_binding(binding, prepared=prepared):
        message, log = _build_user_message(
            root=tmp_path, instruction="Observe", image_paths=[str(image_path)]
        )
        assert message["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert "base64" not in str(log)
        assert str(image_path) not in str(log)
        with pytest.raises(HostImageValidationError):
            _build_user_message(
                root=tmp_path, instruction="Observe", image_paths=[str(root / "different.png")]
            )
    with (
        use_ide_host_image_binding(binding),
        pytest.raises(ConfigError, match="Host screen image rejected"),
    ):
        _build_user_message(root=tmp_path, instruction="Observe", image_paths=[str(image_path)])
