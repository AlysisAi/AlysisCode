"""The approval reviewer runs at low reasoning effort where the model offers it.

A review is a short, bounded judgement and a person is waiting on it. Left to
the provider, the chat model's default effort can be its maximum.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_approval_review import _FakeClient, _request, _reviewer

from alysis_code.agent.approval_review import REVIEW_REASONING_EFFORT
from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client


@pytest.fixture
def review(tmp_path, monkeypatch):
    """A reviewer whose capability lookup and client construction are observable."""
    state: dict[str, Any] = {"offered": ["low", "high", "max"], "built": [], "fail": None}

    def capabilities(cfg, **_):
        if isinstance(state["offered"], Exception):
            raise state["offered"]
        state.setdefault("asked", []).append(cfg.model)
        return {"reasoning_efforts": [{"id": item} for item in state["offered"]]}

    def build(**kwargs):
        if state["fail"] is not None:
            raise state["fail"]
        client = _FakeClient([{"decision": "allow", "reason": "dedicated"}] * 5)
        client.model = kwargs["model"]
        state["built"].append((kwargs, client))
        return client

    monkeypatch.setattr("alysis_code.model_reasoning.model_reasoning_capabilities", capabilities)
    monkeypatch.setattr("alysis_code.llm.factory.make_llm_client", build)
    reviewer, main_client, _store = _reviewer(
        tmp_path, [{"decision": "allow", "reason": "chat"}] * 5
    )

    def run():
        return reviewer.review(_request(), workspace_root=tmp_path)

    return state, main_client, run


def test_review_uses_its_own_low_effort_client(review) -> None:
    state, main_client, run = review
    first, second = run(), run()
    assert first.outcome == second.outcome == "allow"
    assert len(state["built"]) == 1  # built once, then reused
    kwargs, dedicated = state["built"][0]
    assert kwargs["model"] == main_client.model == "chat-model"
    assert kwargs["fixed_reasoning_effort"] == REVIEW_REASONING_EFFORT == "low"
    assert kwargs["temperature"] == 0.0
    assert len(dedicated.calls) == 2 and not main_client.calls
    assert state["asked"] == ["chat-model"]  # the capability lookup is cached too


@pytest.mark.parametrize("offered", [[], ["high", "max"], ["enabled"], RuntimeError("offline")])
def test_model_without_a_low_effort_keeps_the_chat_client(review, offered) -> None:
    state, main_client, run = review
    state["offered"] = offered
    assert run().outcome == "allow"
    assert not state["built"] and len(main_client.calls) == 1


def test_failed_setup_never_costs_the_review(review) -> None:
    state, main_client, run = review
    state["fail"] = RuntimeError("could not build the client")
    assert run().outcome == "allow" and run().outcome == "allow"
    assert not state["built"] and len(main_client.calls) == 2


def test_fixed_effort_is_not_replaced_by_the_chat_choice(monkeypatch) -> None:
    # The app passes the chat's own effort to the whole engine process.
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", "max")
    cfg = AppConfig(model="glm-5.3-flash", base_url="https://example.test/v1")
    cfg.llm_reasoning_effort = "high"
    client = make_llm_client(
        cfg=cfg, api_key="fixture", model="glm-5.3-flash", fixed_reasoning_effort="low"
    )
    assert client.reasoning_effort == "low"
    assert client.enable_thinking is True
