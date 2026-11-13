"""Stop during a file transfer to an owl's own computer is a cancellation, not a failure."""

import importlib

import pytest

from alysis_code.cancellation import CooperativeCancellationError


@pytest.mark.parametrize(
    "module,provider",
    [("local_hcs", "WindowsVmComputers"), ("local_vm", "LocalVmComputers")],
)
def test_stopping_during_an_upload_cancels_instead_of_failing(module, provider):
    cls = getattr(importlib.import_module(f"alysis_code.bots.{module}"), provider)
    computers = object.__new__(cls)  # the transfer check runs before any computer is touched
    with pytest.raises(CooperativeCancellationError, match="cancelled"):
        computers.upload(None, "/workspace/notes.txt", [b"hello"], size=5, cancel=lambda: True)
