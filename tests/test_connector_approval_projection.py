from copy import deepcopy
from pathlib import Path

import pytest

from alysis_code.ide.approval_binding import (
    build_approval_binding,
    normalize_connector_approval_context,
)
from alysis_code.surface import ApprovalRequest


def binding(root: Path, context=None, *, access="write", target="@fixture: owner/repository"):
    metadata = {"host_action": "mcp.github.call", "access": access, "target": target}
    if context is not None:
        metadata["connector_context"] = context
    return build_approval_binding(
        ApprovalRequest("mcp", "Fixture", "Fixture", command="mcp.github.call", metadata=metadata),
        project_root=root,
        runtime_fingerprint="fixture",
        mode="review",
        operation_id="operation",
        job_id="job",
        tool_call_id="call",
    )


@pytest.mark.parametrize("access", ["read", "write"])
def test_host_display_retains_real_effect_and_target_without_inventing_identity(tmp_path, access):
    result = binding(tmp_path, access=access)
    assert result.action["access"] == access
    assert result.action["target"] == "@fixture: owner/repository"
    assert "connector_context" not in result.action


@pytest.mark.parametrize(
    "field", ["connector", "account", "repository", "service", "tool", "resources"]
)
def test_scope_fields_are_bound_by_the_exact_display_fingerprint(tmp_path, field):
    context = {
        "connector": "GitHub",
        "account": "fixture",
        "repository": "owner/repository",
        "service": "Fixture",
        "tool": "create_or_update_file",
        "resources": ["index.html"],
    }
    original = binding(tmp_path, context)
    changed = deepcopy(context)
    changed[field] = (
        ["another.html"]
        if field == "resources"
        else "Google"
        if field == "connector"
        else "Changed"
    )
    assert binding(tmp_path, changed).action_fingerprint != original.action_fingerprint
    assert original.action["connector_context"] == context


@pytest.mark.parametrize(
    "invalid",
    [
        {"connector": "GitHub", "arguments": {"content": "sensitive"}},
        {"connector": "GitHub", "account": {}},
        {"connector": "Google", "resources": [{"password": "sensitive"}]},
        {"connector": "Google", "resources": ["a"] * 101},
        {"connector": []},
    ],
)
def test_raw_nested_or_unbounded_context_is_rejected(invalid):
    with pytest.raises(ValueError):
        normalize_connector_approval_context(invalid)


def test_context_redaction_precedes_display_hash_without_mutating_source(tmp_path):
    context = {
        "connector": "GitHub",
        "tool": "update",
        "resources": ["password=fixture-private-value"],
    }
    result = binding(tmp_path, context)
    assert "fixture-private-value" not in str(result.renderer_payload())
    assert context["resources"] == ["password=fixture-private-value"]
