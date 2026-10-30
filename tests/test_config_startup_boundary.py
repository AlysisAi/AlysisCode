"""Configuration must be usable before a provider runtime exists."""

import os
import subprocess
import sys
from pathlib import Path


def test_configuration_defaults_do_not_initialize_provider_requests(tmp_path: Path):
    code = """
import sys
from alysis_code.config import AppConfig, clone_cfg, load_config
from alysis_code.llm import provider_defaults
cfg = load_config()
copy = clone_cfg(cfg)
assert copy.provider_concurrency_caps == provider_defaults.DEFAULT_PROVIDER_CONCURRENCY_CAPS
assert copy.provider_concurrency_caps is not provider_defaults.DEFAULT_PROVIDER_CONCURRENCY_CAPS
assert copy.provider_retry_max_retries == provider_defaults.DEFAULT_PROVIDER_RETRY_MAX_RETRIES
assert copy.provider_retry_base_delay_seconds == provider_defaults.DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS
assert copy.provider_retry_max_delay_seconds == provider_defaults.DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS
assert 'alysis_code.llm.provider_limits' not in sys.modules
assert 'alysis_code.llm.http_cancellation' not in sys.modules
assert 'httpx' not in sys.modules
from alysis_code.failure_category import exit_code_for_failure
from alysis_code.run_outcome import INFRASTRUCTURE_FAILURE_EXIT_CODE
assert exit_code_for_failure(RuntimeError('sandbox unavailable')) == INFRASTRUCTURE_FAILURE_EXIT_CODE
assert 'httpx' not in sys.modules
from alysis_code.llm import provider_limits
for name in dir(provider_defaults):
    if name.startswith('DEFAULT_PROVIDER_'):
        assert getattr(provider_limits, name) is getattr(provider_defaults, name)
settings = provider_limits.resolve_provider_retry_settings(copy)
assert settings.max_retries == copy.provider_retry_max_retries
assert settings.base_delay_seconds == copy.provider_retry_base_delay_seconds
assert settings.max_delay_seconds == copy.provider_retry_max_delay_seconds
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            ALYSIS_CONFIG_DIR=str(tmp_path / "config"),
            ALYSIS_DATA_DIR=str(tmp_path / "data"),
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
