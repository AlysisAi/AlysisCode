"""Frozen section T producer/schema checks; synthetic data and clocks only."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import parse_request_line

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/computer_use_attribution_v1.json").read_text()
)
CONTEXT = FIXTURE["bases"]["turnContext"]


def api():
    return importlib.import_module("alysis_code.computer_use_attribution")


def test_independent_attribution_capability():
    caps = capabilities_payload()
    assert caps["features"]["computer_use_attribution_v1"] == {"supported": True, "version": 1}
    assert "computer_use_attribution" in caps["events"]


def test_request_opt_in_is_separate_from_business_params():
    context = FIXTURE["bases"]["startupContext"]
    request = parse_request_line(
        json.dumps(
            {
                "protocol_version": "1",
                "id": "q1",
                "method": "session.create",
                "params": {},
                "computer_use_attribution": {"version": 1, "context": context},
            }
        )
    )
    assert request.params == {}
    assert request.computer_use_attribution == context


def test_collector_reserves_final_summary_after_detail_saturation():
    a = api()
    clock = [0.0]
    c = a.Collector(CONTEXT, clock=lambda: clock[0])
    for _ in range(65):
        span = c.begin("model_call")
        clock[0] += 5
        c.attempt(span)
        c.end(span, category="start", response_class="start_only")
    assert c.finish("returned") == FIXTURE["bases"]["detailSaturatedSummary"]
    assert len(c.records) == 128


@pytest.fixture(autouse=True)
def isolated_encoder_owner(monkeypatch):
    monkeypatch.setattr(api(), "_encoder_prior_unowned", False)
