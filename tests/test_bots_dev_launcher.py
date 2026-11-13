"""Development tests cannot share the user preview's database or default port."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "bots_dev", Path(__file__).parents[1] / "scripts/bots-dev/dev.py"
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_preview_and_tests_use_distinct_persistent_state():
    assert launcher.development_target(False, None, None) == ("alysis-bots-dev", 7080)
    first = launcher.development_target(True, None, None)
    second = launcher.development_target(True, None, None)
    assert first[0] != second[0]
    assert first[0].startswith("alysis-bots-test-")
    assert first[1] == second[1] == 7081
    # A named test may be reopened for inspection, but can never alias preview state.
    assert launcher.development_target(False, "alysis-bots-dev", None) == (
        "alysis-bots-test-alysis-bots-dev",
        7081,
    )
    assert launcher.development_target(True, "coding", 7082) == ("alysis-bots-test-coding", 7082)


@pytest.mark.parametrize("name", ["../preview", "", "Upper", "a b", "a" * 41])
def test_invalid_test_namespace_rejected(name):
    if name == "":
        # Empty optional input still selects an isolated generated namespace.
        assert launcher.development_target(True, name, None)[0].startswith("alysis-bots-test-")
    else:
        with pytest.raises(ValueError):
            launcher.development_target(True, name, None)


@pytest.mark.parametrize("port", [0, 80, 65536])
def test_invalid_port_rejected(port):
    with pytest.raises(ValueError):
        launcher.development_target(True, None, port)
