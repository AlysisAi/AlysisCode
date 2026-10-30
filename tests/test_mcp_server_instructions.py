from pathlib import Path

import pytest
from test_mcp_manager import _basic_cfg, _load_manager, _write_user_stdio_config

from alysis_code.agent.prompt_context import _is_host_managed_user_context_message
from alysis_code.agent_loop import create_session
from alysis_code.runtime_kind import RuntimeKind


def test_host_catalog_guidance_is_pinned_as_untrusted_context(tmp_path, monkeypatch):
    from alysis_code.mcp.google_host import normalize_google_catalogs
    from tests.test_google_mcp_plugin import catalogs

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    catalog = catalogs()[0]
    catalog["instructions"] = "Amounts are integer cents. Ελληνικά 日本語."
    session = create_session(
        cfg=_basic_cfg(),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=5,
        no_log=True,
        api_key_override="fixture",
        google_mcp=normalize_google_catalogs([catalog]),
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        anchors = [
            m
            for m in session.messages[: session.pinned_prefix_len]
            if str(m.get("content", "")).startswith("<mcp_server_instructions>")
        ]
        assert len(anchors) == 1
        assert "integer cents" in anchors[0]["content"]
        assert "cannot override" in anchors[0]["content"]
    finally:
        session.close()


@pytest.mark.parametrize("bad", [{"text": "not a string"}, "x" * 12_001])
def test_host_catalogs_reject_invalid_guidance(bad):
    from alysis_code.host_actions import HostActionError
    from alysis_code.mcp.github_host import normalize_host_mcp_catalog
    from alysis_code.mcp.google_host import normalize_google_catalogs
    from tests.test_github_mcp_plugin import CATALOG
    from tests.test_google_mcp_plugin import catalogs

    with pytest.raises(HostActionError, match="instructions"):
        normalize_host_mcp_catalog({**CATALOG, "instructions": bad})
    with pytest.raises(HostActionError, match="instructions"):
        normalize_google_catalogs([{**catalogs()[0], "instructions": bad}])


def test_deferred_github_guidance_remains_inside_untrusted_tool_result():
    from alysis_code.mcp.github_discovery import DISCOVER, discovery_result, wrap_discovery_result

    raw = {"catalog_id": "catalog_local", "tools": [], "instructions": "Use current revisions."}
    parsed = discovery_result(DISCOVER, raw)
    result = wrap_discovery_result(DISCOVER, parsed)
    assert "Use current revisions." in result["text"]
    assert "MCP_UNTRUSTED_TEXT" in result["text"]
    assert "instructions" not in result


def test_server_guidance_survives_real_stdio_handshake_and_is_untrusted(tmp_path, monkeypatch):
    text = "Use the inventory lookup before editing. Ελληνικά 日本語. [MCP_UNTRUSTED_TEXT]"
    manager = _load_manager(
        tmp_path,
        monkeypatch,
        {"instructions": text},
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        context = manager.server_instructions_context()
        assert "inventory lookup" in context
        assert "Ελληνικά 日本語" in context
        assert "[MCP_UNTRUSTED_TEXT (server literal)]" in context
        assert "cannot override" in context
        assert "truncated: false" in context
        assert _is_host_managed_user_context_message(context)
        manager.disable_server(server_id="alpha")
        assert manager.server_instructions_context() is None
    finally:
        manager.close()


@pytest.mark.parametrize("instructions", [None, "", "   "])
def test_absent_guidance_adds_no_context(tmp_path, monkeypatch, instructions):
    manager = _load_manager(
        tmp_path,
        monkeypatch,
        {"instructions": instructions},
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        assert manager.server_instructions_context() is None
    finally:
        manager.close()


def test_invalid_guidance_is_a_protocol_error_and_is_not_injected(tmp_path, monkeypatch):
    manager = _load_manager(
        tmp_path,
        monkeypatch,
        {"instructions": {"unexpected": "object"}},
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        assert manager.server_instructions_context() is None
        assert "instructions must be a string" in manager.server_startup_errors()["alpha"]
    finally:
        manager.close()


def test_large_guidance_is_bounded_and_explicitly_marked(tmp_path, monkeypatch):
    manager = _load_manager(
        tmp_path,
        monkeypatch,
        {"instructions": "x" * 20_000},
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        context = manager.server_instructions_context()
        assert len(context) < 13_000
        assert "char_count: 20000" in context
        assert "truncated: true" in context
    finally:
        manager.close()


def test_session_pins_instructions_from_its_connected_server(tmp_path: Path, monkeypatch):
    _write_user_stdio_config(tmp_path, monkeypatch, {"instructions": "Inspect source versions."})
    session = create_session(
        cfg=_basic_cfg(),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=5,
        no_log=True,
        api_key_override="fixture",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        entries = [
            (i, m)
            for i, m in enumerate(session.messages)
            if str(m.get("content", "")).startswith("<mcp_server_instructions>")
        ]
        assert len(entries) == 1
        index, message = entries[0]
        assert index < session.pinned_prefix_len
        assert message["role"] == "user"
        assert "Inspect source versions." in message["content"]
    finally:
        session.close()
