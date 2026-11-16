from __future__ import annotations

import pytest

from alysis_code.web_search_policy import normalize_web_search_policy


def test_web_search_policy_is_an_access_switch_only() -> None:
    assert normalize_web_search_policy("") == "auto"
    assert normalize_web_search_policy(" AUTO ") == "auto"
    assert normalize_web_search_policy("off") == "off"


def test_legacy_always_policy_migrates_to_model_led_auto() -> None:
    assert normalize_web_search_policy("always") == "auto"


def test_normalize_web_search_policy_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="web_search_policy"):
        normalize_web_search_policy("sometimes")
