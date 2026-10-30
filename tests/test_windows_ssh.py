from dataclasses import replace

import pytest

from alysis_code.config import ConfigError
from alysis_code.sandbox_runner import select_shell_sandbox_backend
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.mark.parametrize(
    "backend,system", [("windows-mxc", "Windows"), ("auto", "Windows"), ("seatbelt", "Darwin")]
)
def test_exact_file_access_uses_supported_containment(backend, system):
    choice = select_shell_sandbox_backend(
        ShellSandboxSettings(backend=backend, additional_read_files=("synthetic-key",)),
        system=system,
        path_exists=lambda _: True,
    )
    assert choice.backend in {"windows-mxc", "seatbelt"}


@pytest.mark.parametrize(
    "changes,system",
    [
        ({"mode": "off"}, "Windows"),
        ({"mode": "warn"}, "Windows"),
        ({"backend": "windows-native"}, "Windows"),
        ({"backend": "docker"}, "Windows"),
        ({"clear_env": False}, "Windows"),
        ({"protect_repo_meta": False}, "Windows"),
        ({}, "Linux"),
    ],
)
def test_exact_files_never_fall_back_to_host_or_other_backend(changes, system):
    settings = replace(
        ShellSandboxSettings(
            backend="windows-mxc",
            additional_read_files=("synthetic-key",),
            unavailable_fallback="ask",
        ),
        **changes,
    )
    with pytest.raises(ConfigError, match="Exact read-file permissions"):
        select_shell_sandbox_backend(settings, system=system)
