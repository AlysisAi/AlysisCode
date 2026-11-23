"""The image generation default must name a model OpenAI still serves.

OpenAI retired gpt-image-1 on 2026-10-23 (dall-e-2 and dall-e-3 on 2026-05-12):
https://platform.openai.com/docs/deprecations
"""

from alysis_code.config import ImageGenerationConfig

RETIRED = {"gpt-image-1", "dall-e-3", "dall-e-2"}


def test_default_image_model_is_served() -> None:
    config = ImageGenerationConfig(enabled=True)

    assert config.model == "gpt-image-2"
    assert config.model not in RETIRED


def test_configured_image_model_is_kept() -> None:
    assert ImageGenerationConfig(enabled=True, model="gpt-image-2.5").model == "gpt-image-2.5"
