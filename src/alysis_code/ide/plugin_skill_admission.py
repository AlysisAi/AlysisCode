"""P2 turn-only exact selection and bounded ACK readiness. No tool authority."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from ..execution_deadline import DeadlineExhausted
from ..extensions import plugin_skills
from . import plugin_skill_projection as projection

ACTION = "plugin.skill.admit"
FIELDS = {"plugin_id", "skill_name", "descriptor_sha256", "text_sha256", "admission_id"}


@dataclass
class Readiness:
    request: object
    session_id: str
    prompt_id: str = ""
    ready: bool = False
    failed: bool = False
    event: threading.Event = field(default_factory=threading.Event)


def selection(value):
    projection.closed(value, FIELDS)
    if (
        not projection.plugin_id(value["plugin_id"])
        or not projection.matches(value["skill_name"], r"[a-z][a-z0-9-]{0,47}")
        or not projection.matches(value["descriptor_sha256"], projection.HEX)
        or not projection.matches(value["text_sha256"], projection.HEX)
        or not projection.matches(value["admission_id"], r"[0-9a-f]{32}")
    ):
        projection.fail()
    return dict(value)


def result(value):
    if type(value) is not dict or type(value.get("admit")) is not bool:
        projection.fail("unavailable")
    if value["admit"]:
        if set(value) != {"admit"}:
            projection.fail("unavailable")
    elif (
        set(value) != {"admit", "reason"}
        or type(value.get("reason")) is not str
        or value["reason"] not in projection.REASONS
    ):
        projection.fail("unavailable")
    return dict(value)


def scoped_duplicates(line, field):
    """Re-decode only the selected P2 object; other protocol objects stay unchanged."""

    class Pairs(list):
        pass

    raw = json.loads(line, object_pairs_hook=Pairs)
    params = dict(raw).get("params")
    target = dict(params).get(field)

    def walk(value):
        if isinstance(value, Pairs):
            seen = set()
            for key, child in value:
                if key in seen:
                    projection.fail()
                seen.add(key)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(target)


def negotiate(session, capabilities):
    feature = capabilities.get("features", {}).get("host_actions", {})
    if (
        feature.get("supported") is not True
        or feature.get("protocol_version") != "1"
        or type(feature.get("approval_protocol")) is not int
        or feature["approval_protocol"] != 1
        or feature.get("approval_authority") != "engine"
        or feature.get("request_event") != "host_action_requested"
        or feature.get("cancellation_event") != "host_action_cancelled"
        or feature.get("response_method") != "host.action.respond"
        or not {"host.action.respond", "host.action.dispatched"}
        <= set(capabilities.get("methods", []))
        or not {"host_action_requested", "host_action_cancelled"}
        <= set(capabilities.get("events", []))
        or ACTION not in feature.get("actions", [])
        or session.host_approval_protocol != 1
        or ACTION not in session.host_actions
        or not session.workspace_trusted
        or not session.host_capability_fingerprint
    ):
        projection.fail("unsupported")


def _resolve(session, selected, *, expected=None):
    if session.sensitive_media or session.computer_use_host is not None:
        projection.fail("protected_session")
    candidates = [
        s
        for s in getattr(session.agent_session, "plugin_skills", ())
        if s.plugin_id == selected["plugin_id"]
        and s.name.split(":", 1)[1] == selected["skill_name"]
    ]
    if len(candidates) != 1:
        bound = any(
            s.plugin_id == selected["plugin_id"]
            for s in getattr(session.agent_session, "plugin_skills", ())
        )
        projection.fail("unknown_skill" if bound else "not_bound")
    skill = candidates[0]
    if expected is not None and skill is not expected:
        projection.fail("stale")
    if not plugin_skills.plugin_skill_still_active(session.root, skill):
        projection.fail(projection.refusal_reason(session.root, skill.plugin_id))
    projection.verified(session.root, skill.plugin_id, selected["descriptor_sha256"])
    if projection.text_sha256(skill.text) != selected["text_sha256"]:
        projection.fail("text_changed")
    return skill


def check_deadline(session):
    budget = session.task_budget_authority
    if budget is not None:
        budget.check()
    deadline = getattr(session.agent_session, "execution_deadline", None)
    if deadline is not None and deadline.is_exhausted():
        raise DeadlineExhausted("run deadline exhausted during plugin skill admission")


def resolve(session, selected, *, expected=None):
    try:
        return _resolve(session, selected, expected=expected)
    except projection.PluginSkillUnavailable:
        raise
    except Exception:
        projection.fail("unavailable")
