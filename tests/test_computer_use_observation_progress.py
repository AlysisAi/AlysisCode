from __future__ import annotations

import json

import pytest
from test_computer_use_observation_argument_recovery import _call, _run

from alysis_code.agent.computer_use_observation_progress import ComputerUseObservationProgress
from alysis_code.llm.openai_compat import LLMResponse

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def test_refusal_read_allowance_survives_views_focus_waits_and_more_refusals():
    progress = ComputerUseObservationProgress()
    refused = {
        "error": "stale_snapshot",
        "interaction_feedback": {
            "dispatch": "not_dispatched",
            "authority": "valid_for_reobserve",
            "effect": "unknown",
        },
    }
    progress.action_refused(refused)
    for index in range(4):
        assert progress.admit_recovery_read()
        progress.observe(state(index, index), {"include_image": index % 2 == 0}, is_wait=index == 3)
        progress.action_progress(
            "focus",
            {"interaction_feedback": {"dispatch": "dispatched", "effect": "observed_change"}},
        )
        progress.action_refused(refused)
    assert not progress.admit_recovery_read()
    progress.action_progress(
        "press", {"interaction_feedback": {"dispatch": "dispatched", "effect": "unknown"}}
    )
    assert not progress.admit_recovery_read()
    progress.action_progress(
        "press", {"interaction_feedback": {"dispatch": "dispatched", "effect": "observed_change"}}
    )
    assert not progress.admit_recovery_read(), (
        "Core or a receipt-shaped dictionary cannot refill the allowance"
    )
    progress.action_progress(
        "press",
        {
            "interaction_feedback": {
                "dispatch": "dispatched",
                "authority": "valid_for_reobserve",
                "effect": "observed_change",
            }
        },
        validated_receipt=True,
    )
    assert progress.admit_recovery_read()


def test_normal_reads_without_a_refusal_do_not_spend_recovery_allowance():
    progress = ComputerUseObservationProgress()
    assert all(progress.admit_recovery_read() for _ in range(40))


def state(index: int, view: int = 0):
    return {
        "snapshot_id": f"snapshot-{index}",
        "target": {"bundle_id": "fixture.app", "window_id": "same-window"},
        "elements": [{"id": f"element-{index}", "role": "AXTextField", "value": f"PRIVATE_{view}"}],
        "coverage": {"scope": "window" if view == 0 else "subtree", "complete": True},
    }


@pytest.mark.parametrize("views", [1, 2])
def test_fresh_ids_do_not_hide_repeated_native_views(views):
    progress = ComputerUseObservationProgress()
    results = [progress.observe(state(i, i % views), {}) for i in range(16)]
    assert results == [None] * 7 + ["correct"] + [None] * 7 + ["stop"]
    assert "PRIVATE" not in repr(progress.__dict__)


def test_new_observed_content_and_distinct_assertions_are_not_a_read_cycle():
    progress = ComputerUseObservationProgress()
    assert all(progress.observe(state(i, i), {}) is None for i in range(24))
    progress = ComputerUseObservationProgress()
    assert all(
        progress.observe(state(i), {"assertions": [{"name": f"fact-{i}"}]}) is None
        for i in range(24)
    )


@pytest.mark.parametrize("kind", ["image", "browser", "empty"])
def test_reads_without_comparable_native_evidence_do_not_trigger_stall(kind):
    progress = ComputerUseObservationProgress()
    for i in range(24):
        result, arguments = state(i), {}
        if kind == "image":
            arguments["include_image"] = True
        elif kind == "browser":
            result["source"] = "chrome-dom"
        else:
            result["elements"] = []
        assert progress.observe(result, arguments) is None


def test_action_progress_starts_a_new_read_sequence():
    progress = ComputerUseObservationProgress()
    for i in range(8):
        result = progress.observe(state(i), {})
    assert result == "correct"
    progress.action_progress()
    for i in range(8):
        result = progress.observe(state(i), {})
    assert result == "correct"


def test_new_semantic_cycle_receives_its_own_correction():
    progress = ComputerUseObservationProgress()
    for view in (0, 1):
        results = [progress.observe(state(i, view), {}) for i in range(8)]
        assert results == [None] * 7 + ["correct"]
    assert [progress.observe(state(i, 1), {}) for i in range(8)] == [None] * 7 + ["stop"]


def test_explicit_wait_preserves_async_work_and_breaks_a_read_cycle():
    progress = ComputerUseObservationProgress()
    for i in range(7):
        assert progress.observe(state(i), {}) is None
    for i in range(24):
        assert progress.observe(state(i), {"duration_ms": 1000}, is_wait=True) is None
    assert [progress.observe(state(i), {}) for i in range(8)] == [None] * 7 + ["correct"]


def test_actual_turn_can_wait_for_slow_native_result(tmp_path):
    calls = []

    def host(name, arguments):
        calls.append(name)
        return state(len(calls), 1 if len(calls) == 20 else 0)

    _, surface, client, _ = _run(
        tmp_path,
        [_call("computer_wait", {"duration_ms": 1000}, i) for i in range(20)]
        + [LLMResponse(content="The delayed state appeared.", raw={}, tool_calls=[])],
        host,
        max_steps=22,
    )
    assert len(calls) == 20 and client.calls >= 20
    assert all(code != "computer_use_observation_stalled" for code, *_ in surface.closed_errors)


def test_actual_turn_bounds_ignored_cycle_correction_without_any_input(tmp_path):
    calls = []

    def host(name, arguments):
        calls.append(name)
        return state(len(calls), len(calls) % 2)

    code, surface, client, durable = _run(
        tmp_path, [_call("computer_state", {}, i) for i in range(20)], host, max_steps=25
    )
    assert code == 1 and client.calls == 16
    assert calls == ["computer_state"] * 16
    assert "repeat the same observed views" in json.dumps(client.call_records[8]["messages"])
    assert surface.closed_errors[0][0] == "computer_use_observation_stalled"
    assert "PRIVATE" not in durable
    assert "requested result remains unverified" in durable


def test_app_switch_cycle_uses_post_state_and_does_not_count_as_progress():
    progress = ComputerUseObservationProgress()
    results = []
    for i in range(12):
        post = state(i, i % 2)
        post["target"]["bundle_id"] = f"fixture.app.{i % 2}"
        results.append(progress.action_progress("activate_application", {"state": post}))
    assert results == [None] * 5 + ["switch_correct"] + [None] * 5 + ["switch_stop"]
    assert "PRIVATE" not in repr(progress.__dict__)


def test_real_work_or_new_app_state_breaks_switch_cycle():
    progress = ComputerUseObservationProgress()
    for i in range(24):
        assert progress.action_progress("activate_application", {"state": state(i, i)}) is None
    progress = ComputerUseObservationProgress()
    for i in range(24):
        assert progress.action_progress("activate_application", {"state": state(i)}) is None
        progress.action_progress("type", {})


def test_actual_turn_stops_switch_cycle_after_one_correction(tmp_path):
    calls = []

    def host(name, arguments):
        calls.append(name)
        post = state(len(calls), len(calls) % 2)
        post["target"]["bundle_id"] = f"fixture.app.{len(calls) % 2}"
        if name == "computer_action":
            return {"receipt": {"action": "activate_application", "performed": True}, "state": post}
        return post

    responses = [_call("computer_state", {}, 0)] + [
        _call(
            "computer_action",
            {"action": "activate_application", "application": f"Fixture {i % 2}"},
            i + 1,
        )
        for i in range(20)
    ]
    code, surface, client, durable = _run(tmp_path, responses, host, max_steps=25)
    assert code == 1 and calls.count("computer_action") == 12
    assert "Switching applications alone" in json.dumps(client.call_records[7]["messages"])
    assert surface.closed_errors[0][0] == "computer_use_observation_stalled"
    assert "PRIVATE" not in durable
