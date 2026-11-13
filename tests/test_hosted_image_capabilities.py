"""Hosted image routing must match every model currently offered by the plans."""

import pytest

from alysis_code.model_reasoning import hosted_model_capabilities
from alysis_code.model_registry import catalog_model_capabilities


@pytest.mark.parametrize(
    "model,mode",
    [
        ("deepseek-flash", "native"),
        ("glm-5.3-flash", "native"),
        ("gpt-6-luna", "native"),
        ("claude-haiku-5-5", "native"),
        ("deepseek-v4.1-flash", "native"),
        ("minimax-m3.1-flash", "native"),
        ("reflection-beam", "assisted"),
        ("nemotron-3.5-super-vl", "assisted"),
        ("gpt-6.1-terra", "native"),
        ("kimi-k3", "native"),
        ("deepseek-v4-pro", "assisted"),
        ("mistral-large-4", "native"),
        ("glm-5.3", "assisted"),
    ],
)
def test_offered_models_publish_honest_image_routing(model, mode):
    capabilities = hosted_model_capabilities(model)
    assert capabilities["supports_vision"] is True
    assert capabilities["input_modalities"] == ["text", "image"]
    assert capabilities["image_input"] == mode
    assert capabilities.get("image_helper") == ("gpt-6-luna" if mode == "assisted" else None)


def test_assistance_does_not_claim_upstream_native_vision():
    assert (
        catalog_model_capabilities("reflection/Beam-1.0-501B-A23B", "together")["supports_vision"]
        is False
    )
