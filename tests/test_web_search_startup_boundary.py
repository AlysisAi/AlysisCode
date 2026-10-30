"""Readiness checks must not initialize provider execution backends."""

import os
import subprocess
import sys
from pathlib import Path


def test_readiness_preserves_policy_without_loading_provider_execution(tmp_path: Path):
    code = """
import inspect, sys, typing
from alysis_code.config import AppConfig
from alysis_code.tools import web_search_runtime as metadata
from alysis_code.tools.capability_readiness import WebSearchCapabilityProbe
disabled = AppConfig(web_search_mode='off')
status = metadata.resolve_web_search_runtime_status(cfg=disabled, api_key=None)
assert status.registration_ready is False
assert metadata.resolve_web_search_runtime(cfg=disabled, api_key=None) is None
assert WebSearchCapabilityProbe().status(cfg=disabled, api_key=None, exposed=False)['state'] == 'unavailable'
configured = AppConfig(web_search_mode='native', web_search_adapter='openai_responses',
                       web_search_base_url='https://api.openai.com/v1', web_search_model='example')
status = metadata.resolve_web_search_runtime_status(cfg=configured, api_key='synthetic-no-request')
assert status.registration_ready is True
for name in ('alysis_code.llm.openai_responses', 'alysis_code.tools.web_search',
             'alysis_code.tools.web_search_provider_adapters', 'alysis_code.tools.web_search_dashscope',
             'alysis_code.tools.web_search_tavily', 'httpx'):
    assert name not in sys.modules, name
from alysis_code.tools import web_search as implementation
from alysis_code.llm.openai_responses import OpenAIResponsesClient
for name in ('WebSearchError', 'WebSearchRuntimeConfig', 'WebSearchRuntimeStatus',
             'resolve_web_search_runtime', 'resolve_web_search_runtime_status'):
    assert getattr(metadata, name) is getattr(implementation, name), name
# The public execution API still has its actual concrete default and annotations.
assert inspect.signature(implementation.web_search).parameters['client_factory'].default is OpenAIResponsesClient
assert typing.get_type_hints(implementation.web_search)['return'] == dict[str, typing.Any]
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


def test_registration_dispatch_preserves_execution_injection(monkeypatch):
    from alysis_code.agent import tools_assembly
    from alysis_code.tools import web_search as implementation

    expected = {"answer": "synthetic"}
    received = []

    def execute(**kwargs):
        received.append(kwargs)
        return expected

    monkeypatch.setattr(implementation, "web_search", execute)
    options = dict(query="synthetic", cfg=None, api_key="synthetic-no-request")
    assert tools_assembly.web_search(**options) is expected
    assert received == [options]
