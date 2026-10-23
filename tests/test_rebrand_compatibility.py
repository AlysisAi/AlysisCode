"""Public command identity after retirement of legacy aliases."""

import os
import runpy
from pathlib import Path


def test_child_repetition_replay_uses_canonical_package_and_brand() -> None:
    root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(os.fspath(root / "scripts" / "qa" / "replay_child_repetition.py"))

    fingerprint = namespace["_child_tool_outcome_fingerprint"]
    parser = namespace["_parser"]()
    assert fingerprint.__module__ == "alysis_code.agent.turn.core"
    assert "Alysis Code's real repetition" in parser.description
