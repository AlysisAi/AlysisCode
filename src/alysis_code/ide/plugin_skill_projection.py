"""Closed, credential-free P2 projection. Existing execution verifiers alone admit rows."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..extensions import plugin_skills as skills
from ..extensions.plugin_skill_text import is_one_line, one_line
from ..mcp.connector_host import VERIFIED_CONNECTOR_PLUGIN_IDS, verified_connector_plugin
from ..mcp.github_host import verified_host_plugin
from ..mcp.google_host import SERVICES
from ..skills.discovery import discover_skills
from .protocol import ProtocolError, dumps_message, redact_secrets, response_message

METHODS = (
    "initialize",
    "health",
    "getCapabilities",
    "bridge.shutdown",
    "plugin.skills.list",
    "plugin.skills.read",
)
CAPABILITY = dict(supported=True, version=1, role="plugin_projection")
REASONS = frozenset(
    "not_bound not_installed disabled needs_review pin_mismatch unknown_skill text_changed stale protected_session unsupported invalid_request too_large unavailable".split()
)
MESSAGE = "This plugin skill changed or isn't available now. Choose it again."
HEX = r"[0-9a-f]{64}"
EPOCH = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
BASE_KEYS = {"version", "workspace", "epoch", "lifecycle_revision", "bound_plugins"}
SELECTION_KEYS = {
    "kind",
    "selection_id",
    "plugin_id",
    "skill_name",
    "descriptor_sha256",
    "text_sha256",
    "lifecycle_revision",
    "epoch",
}


class PluginSkillUnavailable(ProtocolError):
    def __init__(self, reason: str):
        self.reason = reason if reason in REASONS else "unavailable"
        super().__init__("plugin_skill_unavailable", MESSAGE)


def fail(reason="invalid_request"):
    raise PluginSkillUnavailable(reason)


def matches(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def integer(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def plugin_id(value):
    return (
        type(value) is str
        and len(value) <= 64
        and (
            value == "alysis.github"
            or value in {f"alysis.google_{s}" for s in SERVICES}
            or value in VERIFIED_CONNECTOR_PLUGIN_IDS
            or matches(value, r"custom\.[a-z][a-z0-9_]{0,19}")
        )
    )


def closed(value, keys):
    if type(value) is not dict or set(value) != keys:
        fail()


def text_sha256(text):
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except (UnicodeError, AttributeError):
        fail("unavailable")


def selection_id(epoch, plugin, name, descriptor, text_hash, revision):
    return text_sha256(
        "\0".join(("p2v1", epoch, plugin, name, descriptor, text_hash, str(revision)))
    )[:32]


def strict_frame(line):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail()
            result[key] = value
        return result

    try:
        if len(line.encode("utf-8")) > 64 * 1024:
            fail()
        json.loads(line, object_pairs_hook=pairs)
    except (UnicodeError, ValueError, RecursionError):
        fail()


def validate_selection(value):
    closed(value, SELECTION_KEYS)
    if (
        value["kind"] != "plugin_skill"
        or not plugin_id(value["plugin_id"])
        or not matches(value["skill_name"], r"[a-z][a-z0-9-]{0,47}")
        or not matches(value["descriptor_sha256"], HEX)
        or not matches(value["text_sha256"], HEX)
        or not matches(value["epoch"], EPOCH)
        or not matches(value["selection_id"], r"[0-9a-f]{32}")
        or not integer(value["lifecycle_revision"])
    ):
        fail()
    if value["selection_id"] != selection_id(
        value["epoch"],
        value["plugin_id"],
        value["skill_name"],
        value["descriptor_sha256"],
        value["text_sha256"],
        value["lifecycle_revision"],
    ):
        fail()


def validate_params(value, read=False):
    closed(value, BASE_KEYS | ({"selection"} if read else set()))
    if type(value["version"]) is not int or value["version"] != 1:
        fail()
    if not matches(value["epoch"], EPOCH) or not integer(value["lifecycle_revision"]):
        fail()
    if type(value["workspace"]) is not str or not value["workspace"]:
        fail()
    try:
        root = Path(value["workspace"]).resolve(strict=True)
        if not root.is_dir():
            fail()
    except (OSError, ValueError, RuntimeError):
        fail()
    bound = value["bound_plugins"]
    if type(bound) is not list or len(bound) > 64:
        fail()
    seen = set()
    for row in bound:
        closed(row, {"plugin_id", "descriptor_sha256", "lifecycle_revision"})
        if (
            not plugin_id(row["plugin_id"])
            or row["plugin_id"] in seen
            or not matches(row["descriptor_sha256"], HEX)
            or not integer(row["lifecycle_revision"])
            or row["lifecycle_revision"] > value["lifecycle_revision"]
        ):
            fail()
        seen.add(row["plugin_id"])
    if read:
        validate_selection(value["selection"])
        if value["selection"]["epoch"] != value["epoch"]:
            fail("stale")
    return root


def binding(pid):
    # The exact execution mapping, with no credential or fabricated tool catalog.
    if pid == "alysis.github":
        return skills._bound_plugins({}, None)[0]
    if pid.startswith("custom.") or pid in VERIFIED_CONNECTOR_PLUGIN_IDS:
        return skills._bound_plugins(None, None, [{"plugin_id": pid}])[0]
    return skills._bound_plugins(None, [{"service": pid.removeprefix("alysis.google_")}])[0]


def refusal_reason(root, pid):
    """Diagnosis only after execution refused; never returns an eligible object."""
    from ..extensions.activation import resolve_active_plugins
    from ..extensions.models import ProjectExtensionOverrides
    from ..extensions.state import compute_effective_enabled, load_global_state, load_project_state

    try:
        global_state = load_global_state()
        try:
            decision = resolve_active_plugins(repo_root=root, workspace_trust_prompt=None)
            project = load_project_state(root) if decision.workspace_trust_granted else None
            enabled = decision.enabled_plugin_ids
        except (RuntimeError, ValueError):
            project = None
            enabled = compute_effective_enabled(global_state, ProjectExtensionOverrides())
        records = [
            s.installed[pid]
            for s in (global_state, project)
            if s is not None and pid in s.installed
        ]
        if not records:
            return "not_installed"
        if any(r.trust != "manifest-sha256" for r in records):
            return "needs_review"
        if pid not in enabled:
            return "disabled"
        return "pin_mismatch"
    except Exception:
        return "unavailable"


def verified(root, pid, expected_pin):
    ref = binding(pid)
    manifest = skills._verified(ref, root)
    if manifest is None:
        fail(refusal_reason(root, pid))
    # Metadata from the same execution verifier; compare the exact parsed object
    # to reject drift between these reads. Neither metadata nor the classifier admits.
    evidence = (
        skills.verified_custom_plugin(root, pid)
        if ref[1] == skills._CUSTOM
        else verified_connector_plugin(root, pid)
        if ref[1] == skills._VERIFIED
        else verified_host_plugin(root, *ref)
    )
    if evidence is None or evidence[1] != manifest:
        fail("stale")
    if evidence[0]["manifest_sha256"] != expected_pin:
        fail("pin_mismatch")
    return manifest, ref


def build_rows(root, params):
    rows, dropped, guidance = [], [], {}
    try:
        ordinary = discover_skills(focus_path=root, workspace_root=root)
        names = {key for skill in ordinary.ordered for key in skill.lookup_keys()}
    except Exception:
        fail("unavailable")
    for bound in params["bound_plugins"]:
        pid = bound["plugin_id"]
        try:
            manifest, ref = verified(root, pid, bound["descriptor_sha256"])
            plugin_rows, plugin_guidance = [], {}
            for skill in skills._skills_of(manifest, ref[1], ref[2]):
                name = skill.name.split(":", 1)[1]
                digest = text_sha256(skill.text)
                custom = ref[1] == skills._CUSTOM
                row = dict(
                    selection_id=selection_id(
                        params["epoch"],
                        pid,
                        name,
                        bound["descriptor_sha256"],
                        digest,
                        bound["lifecycle_revision"],
                    ),
                    plugin_id=pid,
                    skill_name=name,
                    qualified_name=f"{pid}:{name}",
                    short_alias=skill.name,
                    plugin_name=skill.plugin_name,
                    plugin_version=skill.version,
                    descriptor_sha256=bound["descriptor_sha256"],
                    text_sha256=digest,
                    lifecycle_revision=bound["lifecycle_revision"],
                    source_kind="custom" if custom else "bundled",
                    publisher="You" if custom else skill.publisher,
                    publisher_tier="unverified"
                    if custom
                    else "alysis"
                    if skill.tier == "alysis"
                    else "community",
                    title=one_line(skill.title),
                    description=one_line(skill.description),
                    uses_tools=list(skill.uses_tools),
                    read_only=True,
                )
                for key, limit in (
                    ("plugin_name", 60),
                    ("plugin_version", 64),
                    ("publisher", 200),
                    ("title", 80),
                    ("description", 140),
                ):
                    if (
                        type(row[key]) is not str
                        or len(row[key]) > limit
                        or not is_one_line(row[key])
                    ):
                        fail("unavailable")
                if (
                    not matches(skill.name, r"[a-z][a-z0-9_]*:[a-z][a-z0-9-]*")
                    or len(skill.name) > 113
                ):
                    fail("unavailable")
                text = skills._guidance(skill)
                if redact_secrets(row) != row or redact_secrets(text) != text:
                    fail("unavailable")
                plugin_rows.append(row)
                plugin_guidance[row["qualified_name"]] = text
            # Same execution object must still be current when the projection completes.
            if skills._verified(ref, root) != manifest:
                fail("stale")
            rows.extend(plugin_rows)
            guidance.update(plugin_guidance)
        except PluginSkillUnavailable as exc:
            dropped.append(dict(plugin_id=pid, reason=exc.reason))
        except Exception:
            dropped.append(dict(plugin_id=pid, reason="unavailable"))
    aliases = [r["short_alias"].casefold() for r in rows]
    for row in rows:
        alias = row["short_alias"].casefold()
        if alias in names or aliases.count(alias) != 1:
            row["short_alias"] = None
    if len(rows) > 256:
        fail("too_large")
    return rows, dropped, guidance


def project(method, params, request_id):
    read = method == "plugin.skills.read"
    root = validate_params(params, read)
    rows, dropped, guidance = build_rows(root, params)
    base = dict(version=1, epoch=params["epoch"], lifecycle_revision=params["lifecycle_revision"])
    if not read:
        result = dict(**base, skills=rows, dropped=dropped)
    else:
        selected = params["selection"]
        pid = selected["plugin_id"]
        if pid not in {b["plugin_id"] for b in params["bound_plugins"]}:
            fail("not_bound")
        for item in dropped:
            if item["plugin_id"] == pid:
                fail(item["reason"])
        row = next(
            (r for r in rows if r["qualified_name"] == f"{pid}:{selected['skill_name']}"), None
        )
        if row is None:
            fail("unknown_skill")
        for field, reason in (
            ("descriptor_sha256", "pin_mismatch"),
            ("text_sha256", "text_changed"),
            ("lifecycle_revision", "stale"),
            ("selection_id", "stale"),
        ):
            if row[field] != selected[field]:
                fail(reason)
        result = dict(**base, row=row, guidance=guidance[row["qualified_name"]])
    if (
        len(dumps_message(response_message(request_id, result)).encode("utf-8"))
        > (32 if read else 256) * 1024
    ):
        fail("too_large")
    return result
