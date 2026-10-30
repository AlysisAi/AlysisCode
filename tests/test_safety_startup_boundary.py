"""Local text safety must not initialize the HTTP transport."""

import os
import subprocess
import sys
from pathlib import Path


def test_local_sanitization_keeps_canonical_http_exports_lazy():
    code = """
import sys, typing
from alysis_code import safety
from alysis_code.safety.subagent_report import sanitize_subagent_report
assert callable(sanitize_subagent_report)
assert callable(safety.sanitize_mcp_tool_description)
assert set(safety.__all__) <= set(dir(safety))
assert 'alysis_code.safety.safe_http' not in sys.modules
assert 'httpx' not in sys.modules
try:
    safety.unknown_export
except AttributeError:
    pass
else:
    raise AssertionError('unknown export accepted')
assert 'httpx' not in sys.modules
from alysis_code.safety import *
from alysis_code.safety import safe_http as implementation
assert SafeHttpError is implementation.SafeHttpError
assert safe_http_request is implementation.safe_http_request
assert safety.safe_http_request is implementation.safe_http_request
assert typing.get_type_hints(safe_http_request) == typing.get_type_hints(implementation.safe_http_request)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
