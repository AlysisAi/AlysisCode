"""P2 projection through real raw JSONL and the installed execution verifier."""

import hashlib
import io
import json

import pytest

from alysis_code.ide.stdio_bridge import StdioBridge
from alysis_code.mcp.connector_host import verified_custom_plugin
from tests.test_custom_plugins import _descriptor, _save, root  # noqa: F401

EPOCH = "00000000-0000-4000-8000-000000000001"
TEXT = 'Read the notes.\nKeep emoji 😀 and quotes "exact".\n'


def params(root):  # noqa: F811
    meta, _ = verified_custom_plugin(root, "custom.tools")
    return dict(
        version=1,
        workspace=str(root),
        epoch=EPOCH,
        lifecycle_revision=3,
        bound_plugins=[
            dict(
                plugin_id="custom.tools",
                descriptor_sha256=meta["manifest_sha256"],
                lifecycle_revision=2,
            )
        ],
    )


def selection(result):
    row = result["skills"][0]
    return dict(
        kind="plugin_skill",
        epoch=result["epoch"],
        **{
            k: row[k]
            for k in (
                "selection_id",
                "plugin_id",
                "skill_name",
                "descriptor_sha256",
                "text_sha256",
                "lifecycle_revision",
            )
        },
    )


def rpc(bridge, method, value, ident="p2"):
    bridge.process_line(
        json.dumps(dict(protocol_version="1", id=ident, method=method, params=value))
    )
    return [
        json.loads(s)
        for s in bridge.stdout.getvalue().splitlines()
        if json.loads(s).get("id") == ident
    ][-1]


@pytest.fixture
def installed(root):  # noqa: F811
    _save(
        root,
        _descriptor(
            auth="none",
            extra='\n[[components.plugin_skill]]\nname="notes"\ntitle="Notes"\ndescription="Read notes."\nuses_tools=["search"]\ntext='
            + json.dumps(TEXT, ensure_ascii=False)
            + "\n",
        ),
    )
    return root


@pytest.mark.parametrize(
    "method",
    [
        "session.create",
        "chat.send",
        "run.start",
        "ext.list",
        "ext.custom.save",
        "skill.list",
        "config.get",
    ],
)
def test_projection_role_denies_every_runtime_and_management_route(installed, method):
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        assert rpc(bridge, method, {})["error"]["code"] == "method_not_found"
        assert bridge._sessions == {}
    finally:
        bridge.close()


def test_runtime_default_denies_projection(installed):
    bridge = StdioBridge(stdout=io.StringIO())
    try:
        assert (
            rpc(bridge, "plugin.skills.list", params(installed))["error"]["code"]
            == "method_not_found"
        )
    finally:
        bridge.close()


def test_real_custom_list_read_hash_and_shared_guidance(installed):
    from alysis_code.extensions.plugin_skills import _guidance, active_plugin_skills

    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        cap = rpc(bridge, "getCapabilities", {})["result"]
        assert cap["features"]["plugin_skill_projection_v1"] == dict(
            supported=True, version=1, role="plugin_projection"
        )
        assert set(cap["methods"]) == {
            "initialize",
            "health",
            "getCapabilities",
            "bridge.shutdown",
            "plugin.skills.list",
            "plugin.skills.read",
        }
        value = params(installed)
        reply = rpc(bridge, "plugin.skills.list", value)
        assert reply["ok"], reply
        listed = reply["result"]
        row = listed["skills"][0]
        assert listed["dropped"] == []
        assert row["qualified_name"] == "custom.tools:notes"
        assert row["short_alias"] == "custom_tools:notes"
        assert row["text_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
        assert row["publisher_tier"] == "unverified" and row["read_only"] is True
        detail = rpc(bridge, "plugin.skills.read", {**value, "selection": selection(listed)})[
            "result"
        ]
        [skill] = active_plugin_skills(installed, connector_mcp=[dict(plugin_id="custom.tools")])
        assert detail["guidance"] == _guidance(skill)
        assert detail["row"] == row
    finally:
        bridge.close()


def error_reason(reply):
    assert reply["ok"] is False, reply
    assert reply["error"]["code"] == "plugin_skill_unavailable", reply
    return reply["error"]["details"]["reason"]


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "missing",
        "bool_revision",
        "upper_epoch",
        "duplicate_plugin",
        "over_revision",
        "bad_pin",
        "bad_plugin",
        "bad_version",
    ],
)
def test_closed_projection_requests(installed, mutation):
    value = params(installed)
    if mutation == "unknown":
        value["extra"] = True
    elif mutation == "missing":
        del value["epoch"]
    elif mutation == "bool_revision":
        value["lifecycle_revision"] = True
    elif mutation == "upper_epoch":
        value["epoch"] = "AAAAAAAA-0000-4000-8000-000000000001"
    elif mutation == "duplicate_plugin":
        value["bound_plugins"] *= 2
    elif mutation == "over_revision":
        value["bound_plugins"][0]["lifecycle_revision"] = 4
    elif mutation == "bad_pin":
        value["bound_plugins"][0]["descriptor_sha256"] = "A" * 64
    elif mutation == "bad_plugin":
        value["bound_plugins"][0]["plugin_id"] = "custom.bad-name"
    elif mutation == "bad_version":
        value["version"] = True
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        assert error_reason(rpc(bridge, "plugin.skills.list", value)) == "invalid_request"
    finally:
        bridge.close()


def test_duplicate_keys_and_actual_request_byte_bound(installed):
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        line = json.dumps(
            dict(
                protocol_version="1",
                id="dupe",
                method="plugin.skills.list",
                params=params(installed),
            )
        )
        bridge.process_line(line.replace('"version": 1', '"version": 1, "version": 1'))
        assert (
            error_reason(json.loads(bridge.stdout.getvalue().splitlines()[-1])) == "invalid_request"
        )
        bridge.process_line(line.replace('"dupe"', json.dumps("x" * 65536)))
        assert (
            error_reason(json.loads(bridge.stdout.getvalue().splitlines()[-1])) == "invalid_request"
        )
    finally:
        bridge.close()


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("disabled", "disabled"),
        ("removed", "not_installed"),
        ("pin", "pin_mismatch"),
        ("review", "needs_review"),
        ("bytes", "pin_mismatch"),
        ("secret", "unavailable"),
    ],
)
def test_actual_execution_state_drives_projection_and_read(installed, monkeypatch, kind, reason):
    from alysis_code.extensions.manifest import resolve_manifest_path
    from alysis_code.extensions.paths import installed_plugin_root
    from alysis_code.extensions.state import load_global_state, save_global_state

    value = params(installed)
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        selected = selection(rpc(bridge, "plugin.skills.list", value)["result"])
        state = load_global_state()
        if kind == "disabled":
            state.enabled = []
            state.installed["custom.tools"].enabled = False
        elif kind == "removed":
            del state.installed["custom.tools"]
        elif kind == "pin":
            value["bound_plugins"][0]["descriptor_sha256"] = "b" * 64
        elif kind == "review":
            state.installed["custom.tools"].trust = None
        elif kind == "bytes":
            path = resolve_manifest_path(
                installed_plugin_root("custom.tools", project=False, repo_root=installed)
            )
            path.write_bytes(path.read_bytes() + b"\n# drift\n")
        elif kind == "secret":
            monkeypatch.setenv("P2_SECRET", "Read the notes.")
        if kind in {"disabled", "removed", "review"}:
            save_global_state(state)
        result = rpc(bridge, "plugin.skills.list", value)["result"]
        assert result["skills"] == []
        assert result["dropped"] == [dict(plugin_id="custom.tools", reason=reason)]
        assert (
            error_reason(rpc(bridge, "plugin.skills.read", {**value, "selection": selected}))
            == reason
        )
    finally:
        bridge.close()


@pytest.mark.parametrize(
    "field,changed,reason",
    [
        ("text_sha256", "b" * 64, "text_changed"),
        ("descriptor_sha256", "b" * 64, "pin_mismatch"),
        ("lifecycle_revision", 1, "stale"),
        ("skill_name", "missing", "unknown_skill"),
        ("epoch", "00000000-0000-4000-8000-000000000002", "stale"),
    ],
)
def test_read_never_retargets(installed, field, changed, reason):
    from alysis_code.ide.plugin_skill_projection import selection_id

    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        value = params(installed)
        picked = selection(rpc(bridge, "plugin.skills.list", value)["result"])
        picked[field] = changed
        picked["selection_id"] = selection_id(
            picked["epoch"],
            picked["plugin_id"],
            picked["skill_name"],
            picked["descriptor_sha256"],
            picked["text_sha256"],
            picked["lifecycle_revision"],
        )
        assert (
            error_reason(rpc(bridge, "plugin.skills.read", {**value, "selection": picked}))
            == reason
        )
    finally:
        bridge.close()


def test_projection_starts_no_runtime_services_and_role_is_read_only(installed, monkeypatch):
    from alysis_code.ide import stdio_bridge

    def deny(*args, **kwargs):
        raise AssertionError("Runtime service constructed")

    monkeypatch.setattr(stdio_bridge, "DurablePromptQueue", deny)
    monkeypatch.setattr(stdio_bridge, "DurableForgeRequestLedger", deny)
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        with pytest.raises(AttributeError):
            bridge.role = "runtime"
        for method in ["initialize", "health"]:
            result = rpc(bridge, method, {})["result"]
            assert result["capabilities"]["features"] == {
                "plugin_skill_projection_v1": dict(
                    supported=True, version=1, role="plugin_projection"
                )
            }
        assert rpc(bridge, "bridge.shutdown", {})["ok"]
    finally:
        bridge.close()


def test_alias_collides_with_ordinary_alias(installed, monkeypatch):
    from types import SimpleNamespace

    from alysis_code.ide import plugin_skill_projection as p

    monkeypatch.setattr(
        p,
        "discover_skills",
        lambda **_: SimpleNamespace(
            ordered=[SimpleNamespace(lookup_keys=lambda: ("custom_tools:notes",))]
        ),
    )
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        assert (
            rpc(bridge, "plugin.skills.list", params(installed))["result"]["skills"][0][
                "short_alias"
            ]
            is None
        )
    finally:
        bridge.close()


def test_shared_literal_hash_vectors():
    from pathlib import Path

    from alysis_code.ide import plugin_skill_projection as p

    vectors = json.loads(
        (Path(__file__).parent / "fixtures/plugin_skill_projection/v1.json").read_text()
    )["vectors"]
    for v in vectors:
        assert p.text_sha256(v["text"]) == v["text_sha256"]
        assert (
            p.selection_id(
                v["epoch"],
                v["plugin_id"],
                v["skill_name"],
                v["descriptor_sha256"],
                v["text_sha256"],
                v["lifecycle_revision"],
            )
            == v["selection_id"]
        )
    with pytest.raises(p.PluginSkillUnavailable):
        p.text_sha256("\ud800")


def test_all_runtime_capabilities_are_denied_in_projection(installed):
    from alysis_code.ide.health import SUPPORTED_METHODS
    from alysis_code.ide.plugin_skill_projection import METHODS

    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        for method in set(SUPPORTED_METHODS) - set(METHODS):
            assert rpc(bridge, method, {})["error"]["code"] == "method_not_found"
    finally:
        bridge.close()


def test_actual_framed_read_bound_never_truncates(installed, monkeypatch):
    from alysis_code.ide import plugin_skill_projection as p
    from alysis_code.ide.protocol import dumps_message, response_message

    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        value = params(installed)
        listed = rpc(bridge, "plugin.skills.list", value)["result"]
        picked = selection(listed)
        rows = listed["skills"]
        key = rows[0]["qualified_name"]
        guidance = "😀" * 5000
        monkeypatch.setattr(p, "build_rows", lambda *_: (rows, [], {key: guidance}))
        # ASCII escaping in the existing serializer is larger than compact UTF8.
        assert len(guidance.encode()) < 32768
        assert len(dumps_message(response_message("p2", {"guidance": guidance})).encode()) > 32768
        assert (
            error_reason(rpc(bridge, "plugin.skills.read", {**value, "selection": picked}))
            == "too_large"
        )
    finally:
        bridge.close()


def test_row_count_and_list_envelope_bounds(installed, monkeypatch):
    from alysis_code.ide import plugin_skill_projection as p

    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        monkeypatch.setattr(p, "build_rows", lambda *_: ([{"x": "😀" * 30000}], [], {}))
        assert error_reason(rpc(bridge, "plugin.skills.list", params(installed))) == "too_large"
    finally:
        bridge.close()


def test_unbound_projection_has_no_catalog_fallback(installed):
    value = params(installed)
    value["bound_plugins"] = []
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        result = rpc(bridge, "plugin.skills.list", value)["result"]
        assert result["skills"] == result["dropped"] == []
    finally:
        bridge.close()


def test_cli_projection_role_is_launch_only(monkeypatch):
    from typer.testing import CliRunner

    from alysis_code.cli_impl.commands import ide_bridge as command

    calls = []
    monkeypatch.setattr(command, "run_stdio_bridge", lambda **kwargs: calls.append(kwargs) or 0)
    runner = CliRunner()
    assert (
        runner.invoke(command.ide_bridge_app, ["--stdio", "--role", "plugin-projection"]).exit_code
        == 0
    )
    assert calls == [{"role": "plugin_projection"}]
    calls.clear()
    assert runner.invoke(command.ide_bridge_app, ["--stdio"]).exit_code == 0
    assert calls == [{}]
    calls.clear()
    assert runner.invoke(command.ide_bridge_app, ["--stdio", "--role", "other"]).exit_code != 0
    assert calls == []
