"""Each frozen negotiation leaf must refuse before real queue/model admission."""

import pytest

from tests.test_custom_plugins import root as root
from tests.test_plugin_skill_admission import create, send
from tests.test_plugin_skill_admission import setup as setup


@pytest.mark.parametrize(
    "field,bad",
    [
        ("supported", False),
        ("protocol_version", "0"),
        ("approval_protocol", 0),
        ("approval_protocol", True),
        ("approval_authority", "host"),
        ("request_event", "wrong"),
        ("cancellation_event", "wrong"),
        ("response_method", "wrong"),
        ("actions", []),
    ],
)
@pytest.mark.parametrize("missing", [False, True])
def test_each_feature_requirement_before_enqueue(setup, monkeypatch, field, bad, missing):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    cap = bridge._runtime_capabilities_payload()
    feature = cap["features"]["host_actions"]
    if missing:
        del feature[field]
    else:
        feature[field] = bad
    monkeypatch.setattr(bridge, "_runtime_capabilities_payload", lambda: cap)
    reply = send(setup, sid)
    assert reply["error"]["details"] == {"reason": "unsupported"}
    assert not bridge._jobs and calls == []


@pytest.mark.parametrize(
    "collection,member",
    [
        ("methods", "host.action.respond"),
        ("methods", "host.action.dispatched"),
        ("events", "host_action_requested"),
        ("events", "host_action_cancelled"),
    ],
)
def test_each_advertised_method_and_event_before_enqueue(setup, monkeypatch, collection, member):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    cap = bridge._runtime_capabilities_payload()
    cap[collection].remove(member)
    monkeypatch.setattr(bridge, "_runtime_capabilities_payload", lambda: cap)
    reply = send(setup, sid)
    assert reply["error"]["details"] == {"reason": "unsupported"}
    assert not bridge._jobs and calls == []
