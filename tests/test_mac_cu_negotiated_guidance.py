"""Mac CU guidance follows what the session actually negotiated (audit D5/D6).

With Mac W7 discovery that negotiated application lifecycle v1, the
computer_start entry is byte-identical to plugins-finish (ENGINE 9949e6b5).
With interaction v2, lifecycle v1 and window selection v1 negotiated, the
protected workflow is the plugins-finish SKILL with no restrictive note.
Anything not negotiated keeps the restrictive text. No schema is widened.
"""

from __future__ import annotations

import hashlib
import io
from importlib import resources
from types import SimpleNamespace

import pytest

from alysis_code import computer_use_platform_policy as policy
from alysis_code.agent.browser_start import BrowserStartHandoff, build_computer_start_tools
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.protocol import ProtocolRequest
from alysis_code.skills.computer_use import (
    computer_use_route_availability,
    computer_use_workflow_instructions,
    ordinary_entry_guidance,
)

# sha256 of the stripped legacy entry section at ENGINE 9949e6b5 (plugins-finish).
PF_LEGACY_ENTRY_SHA256 = "7a7738241b854f63c41739e7a51ddb30bc7dc331a55725c351c0b877a50583b3"
# sha256 of the restrictive legacy entry section at ENGINE 304ba715.
MAIN_LEGACY_ENTRY_SHA256 = "48c27f6352d8237ed05fa038bad76c573055a45a81307fbcc69deed02fdf1be7"
# sha256 of bundled computer-use/SKILL.md at ENGINE 9949e6b5.
PF_SKILL_SHA256 = "60be3356fdeda454351513febc196a6e3b8884436702ff689cb9d46b808a97d7"

PF_QUIT = "For an app-close goal prefer quit_application."
PF_INPUT = "native v2 key/type or visual pointer input_mode background needs that action advertised in background_actions"
STALE = (
    "Direct quit_application and window-reopen routes are not offered in legacy Mac mode",
    "Native type, key, scroll, drag and pointer-only click are unavailable",
    "Current availability is the legacy Mac v1 route only",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def mac(monkeypatch):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="darwin"))


def _inventory(lifecycle: bool) -> dict:
    return {
        "version": 1,
        **({"application_lifecycle_version": 1} if lifecycle else {}),
        "targets": [
            {
                "target_id": "6f1d1d38-3a7e-4c35-9a59-7a0d0f0d0f01",
                "name": "TextEdit",
                "route": "native",
            }
        ],
    }


def test_entry_with_negotiated_lifecycle_is_exactly_plugins_finish():
    text = ordinary_entry_guidance(resolved=False, application_lifecycle_v1=True)
    assert _sha(text) == PF_LEGACY_ENTRY_SHA256
    assert PF_QUIT in text and PF_INPUT in text
    assert not any(stale in text for stale in STALE)
    tools = build_computer_start_tools(lambda _s: BrowserStartHandoff("bound"), _inventory(True))
    start = tools["computer_start"]
    assert _sha(start.description) == PF_LEGACY_ENTRY_SHA256
    enum = start.parameters["properties"]["app_steps"]["items"]["properties"]["actions"]["items"][
        "enum"
    ]
    assert {"type", "key", "scroll", "drag", "quit_application"} <= set(enum)
    assert "history_back" not in enum


def test_entry_without_negotiated_lifecycle_offers_input_but_not_quit():
    text = ordinary_entry_guidance(resolved=False, application_lifecycle_v1=False)
    assert PF_INPUT in text
    assert "quit_application is not offered for this request" in text
    assert PF_QUIT not in text and "or quit_application uses true" not in text
    start = build_computer_start_tools(lambda _s: BrowserStartHandoff("bound"), _inventory(False))[
        "computer_start"
    ]
    assert start.description == text
    enum = start.parameters["properties"]["app_steps"]["items"]["properties"]["actions"]["items"][
        "enum"
    ]
    assert "quit_application" not in enum and {"type", "key", "scroll", "drag"} <= set(enum)
    # Default is fail-closed.
    assert ordinary_entry_guidance(resolved=False) == text


def test_entry_without_mac_w7_keeps_the_restrictive_text(monkeypatch):
    monkeypatch.setattr(
        "alysis_code.skills.computer_use.mac_w7_computer_use_available", lambda: False
    )
    for lifecycle in (False, True):
        text = ordinary_entry_guidance(resolved=False, application_lifecycle_v1=lifecycle)
        assert _sha(text) == MAIN_LEGACY_ENTRY_SHA256
        assert all(stale in text for stale in STALE[:2]) and PF_QUIT not in text


def test_entry_fragments_fail_closed_when_packaged_text_drifts(monkeypatch):
    real = (
        resources.files("alysis_code.skills")
        .joinpath("bundled", "computer-use", "references", "ordinary-entry.md")
        .read_text(encoding="utf-8")
    )
    drifted = real.replace("<!-- entry:native-input:unavailable -->", "")

    class Resource:
        def joinpath(self, *parts):
            return self

        def read_text(self, **kwargs):
            return drifted

    monkeypatch.setattr(
        "alysis_code.skills.computer_use.resources.files", lambda package: Resource()
    )
    monkeypatch.setattr(
        "alysis_code.skills.computer_use.mac_w7_computer_use_available", lambda: False
    )
    with pytest.raises(ValueError):
        ordinary_entry_guidance(resolved=False)


def test_skill_is_plugins_finish_and_static_skill_read_has_no_stale_caveat():
    raw = (
        resources.files("alysis_code.skills")
        .joinpath("bundled", "computer-use", "SKILL.md")
        .read_bytes()
    )
    assert hashlib.sha256(raw).hexdigest() == PF_SKILL_SHA256
    assert not any(stale in computer_use_workflow_instructions() for stale in STALE)


@pytest.mark.parametrize(
    "v2,lifecycle,window,expected",
    [
        (True, True, True, ()),
        (True, False, True, ("did not negotiate application lifecycle v1",)),
        (True, True, False, ("did not negotiate window selection v1",)),
        (
            True,
            False,
            False,
            ("did not negotiate application lifecycle v1", "did not negotiate window selection v1"),
        ),
        (False, False, False, ("negotiated only the legacy v1 interaction route",)),
    ],
)
def test_route_note_follows_negotiated_versions(v2, lifecycle, window, expected):
    note = computer_use_route_availability(
        interaction_v2=v2, application_lifecycle_v1=lifecycle, window_selection_v1=window
    )
    if not expected:
        assert note == ""
    for phrase in expected:
        assert phrase in note
    if not v2:
        assert "quit_application" in note and "input_mode" in note


def test_route_note_is_absent_off_mac(monkeypatch):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="win32"))
    assert (
        computer_use_route_availability(
            interaction_v2=False, application_lifecycle_v1=False, window_selection_v1=False
        )
        == ""
    )


def _bridge_prompt(tmp_path, monkeypatch, **versions):
    captured = {}
    bridge = stdio_bridge.StdioBridge(stdout=io.StringIO())
    monkeypatch.setattr(
        bridge, "_managed_browser_factory", lambda **kwargs: SimpleNamespace(close_all=lambda: None)
    )

    class CapturedPrompt(Exception):
        pass

    def capture(**kwargs):
        captured.update(kwargs)
        raise CapturedPrompt

    monkeypatch.setattr(bridge, "_create_session", capture)
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    monkeypatch.setattr(
        stdio_bridge, "_apply_computer_use_route_overrides", lambda *args, **kwargs: None
    )
    destination = stdio_bridge._computer_use_destination(
        model="test-model",
        profile="synthetic",
        auth_source="fixture",
        account_id="fixture",
        endpoint_digest="sha256:" + "a" * 64,
    )
    host = stdio_bridge.ComputerUseHostBinding(
        operation_id="synthetic-operation",
        broker_fingerprint="sha256:" + "b" * 64,
        authority_epoch=1,
        scope_digest="sha256:" + "c" * 64,
        **versions,
    )
    with pytest.raises(CapturedPrompt):
        bridge._session_create(
            ProtocolRequest(
                id="prompt",
                method="session.create",
                params={
                    "workspace": str(tmp_path),
                    "mode": "review",
                    "sensitive_media": True,
                    "computer_use_destination": destination.to_protocol(),
                    "computer_use_host": host.to_protocol(),
                },
            )
        )
    return captured


def test_bridge_prompt_with_full_w7_negotiation_is_plugins_finish_workflow(tmp_path, monkeypatch):
    captured = _bridge_prompt(
        tmp_path,
        monkeypatch,
        interaction_version=2,
        application_lifecycle_version=1,
        window_selection_version=1,
    )
    prompt = captured["trusted_system_prompt_override"]
    assert captured["computer_interaction_v2"] is True
    assert captured["computer_application_lifecycle_v1"] is True
    assert captured["computer_window_selection_v1"] is True
    assert prompt.count(computer_use_workflow_instructions()) == 1
    assert "<computer_use_route_availability>" not in prompt
    assert not any(stale in prompt for stale in STALE)


def test_bridge_prompt_without_v2_stays_restrictive(tmp_path, monkeypatch):
    captured = _bridge_prompt(tmp_path, monkeypatch)
    prompt = captured["trusted_system_prompt_override"]
    assert captured["computer_interaction_v2"] is False
    assert prompt.count(computer_use_workflow_instructions()) == 1
    assert prompt.count("<computer_use_route_availability>") == 1
    assert "negotiated only the legacy v1 interaction route" in prompt


def test_bridge_prompt_v2_without_lifecycle_withholds_only_quit(tmp_path, monkeypatch):
    captured = _bridge_prompt(
        tmp_path, monkeypatch, interaction_version=2, window_selection_version=1
    )
    prompt = captured["trusted_system_prompt_override"]
    assert "did not negotiate application lifecycle v1" in prompt
    assert "did not negotiate window selection v1" not in prompt
    assert "negotiated only the legacy v1" not in prompt
