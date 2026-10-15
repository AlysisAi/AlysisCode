"""Real IDE + real AgentSession, generated loopback route only; no provider mock."""

import os

from alysis_code.agent.session import create_session
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.task_budget import provider_auth_network_allowed

endpoint = os.environ["ALYSIS_BUDGET_FIXTURE_ENDPOINT"]
assert endpoint.startswith("http://127.0.0.1:")
cfg = AppConfig(
    model="gpt-4.1",
    base_url=endpoint,
    routing_mode="code_only",
    provider_retry_base_delay_seconds=0.01,
    provider_retry_max_delay_seconds=0.01,
)
stdio_bridge.load_config = lambda: cfg


def actual_session(**kwargs):
    kwargs["api_key_override"] = "generated-test-key"
    assert not provider_auth_network_allowed()
    session = create_session(**kwargs)
    assert not session.model_registry._provider_auth_metadata_allowed
    return session


raise SystemExit(stdio_bridge.StdioBridge(create_session_fn=actual_session).run())
