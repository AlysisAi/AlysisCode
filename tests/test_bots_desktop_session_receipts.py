"""Fault/authority checks for session receipts; no actual Linux/model claims."""

import pytest
from test_bots_desktop import lab as lab
from test_bots_desktop import task, token

from alysis_code.bots.browser import BrowserImageResult
from alysis_code.bots.desktop_linux import DesktopError


def receipt(manager, name="computer_run"):
    value = manager.call(
        task(),
        name,
        {"command": "fixture"} if name == "computer_run" else {},
        token(),
        approved=True,
        image_input=True,
    )
    return value.observation if isinstance(value, BrowserImageResult) else value


def test_failed_first_operation_does_not_conceal_new_session_identity(lab):
    manager, views, instances = lab
    original = receipt(manager)
    instances[0].closed = True
    factory = manager.factory

    def failed_factory(*args, **kwargs):
        executor = factory(*args, **kwargs)
        call = executor.call
        attempts = []

        def once(owner, operation, arguments, **kwargs):
            attempts.append(operation)
            if len(attempts) == 1:
                raise DesktopError("Synthetic safe operation rejection")
            return call(owner, operation, arguments, **kwargs)

        executor.call = once
        executor.attempts = attempts
        return executor

    manager.factory = failed_factory
    with pytest.raises(DesktopError, match="safe operation rejection"):
        receipt(manager)
    assert len(instances) == 2 and instances[1].attempts == ["run"]
    current = receipt(manager, "computer_observe")
    assert current["computer_session_id"] != original["computer_session_id"]
    assert current["computer_session_id"] == views.frame("owner", "bot")["session_id"]
    # Allocation happened during the rejected operation. False here does not
    # imply continuity with the previously observed session; compare its ID.
    assert current["computer_created"] is False
    assert instances[1].attempts == ["run", "observe"]


@pytest.mark.parametrize("name", ["computer_run", "computer_observe"])
def test_worker_data_cannot_spoof_manager_session_identity(lab, name):
    manager, views, instances = lab
    factory = manager.factory

    def forged_factory(*args, **kwargs):
        executor = factory(*args, **kwargs)
        call = executor.call

        def forged(*args, **kwargs):
            result = call(*args, **kwargs)
            return {**result, "computer_session_id": "forged", "computer_created": False}

        executor.call = forged
        return executor

    manager.factory = forged_factory
    result = receipt(manager, name)
    assert result["computer_created"] is True
    assert result["computer_session_id"] != "forged"
    assert result["computer_session_id"] == views.frame("owner", "bot")["session_id"]
