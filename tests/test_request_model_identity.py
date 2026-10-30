from types import SimpleNamespace

import pytest

from alysis_code.agent.prompt_context import request_model_identity
from alysis_code.agent.task_artifact_binding import TOOL_PARAMETERS, binding_parameters
from alysis_code.agent.turn.core import _approval_declined_final_text


@pytest.mark.parametrize("model", ["route-a", "vendor/custom-model-42", "μοντέλο-二"])
def test_model_identity_comes_from_client_metadata_not_a_model_or_question_match(model):
    client = SimpleNamespace(
        model=model, api_key="private-key", base_url="https://private.example/secret"
    )
    text = request_model_identity(client)
    assert model in text
    assert "requested_model" in text
    assert "private" not in text
    client.model = "changed-route"
    assert "changed-route" in request_model_identity(client)
    assert model not in request_model_identity(client)


@pytest.mark.parametrize("model", [None, "", "bad\nidentifier", "x" * 513])
def test_missing_or_invalid_client_identity_is_not_invented(model):
    assert "unavailable" in request_model_identity(SimpleNamespace(model=model))


def test_host_default_requires_an_explicit_location_choice_without_mutating_shared_schema():
    assert (
        "location"
        in binding_parameters("delivered/current")["properties"]["paths"]["items"]["required"]
    )
    assert "location" not in binding_parameters(None)["properties"]["paths"]["items"]["required"]
    assert "location" not in TOOL_PARAMETERS["properties"]["paths"]["items"]["required"]


def test_denial_wording_uses_the_registered_operation_and_never_advises_escalation():
    text = _approval_declined_final_text(tool_name="desktop_terminal_open", approval_kind="shell")
    assert "open command window" in text
    assert "did not run" in text
    assert "desktop_terminal" not in text
    assert "Full access" not in text
