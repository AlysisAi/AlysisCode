"""Opt-in paired APP fixture: real protocol stdout, metadata/facts in private files.

Launch only via the offline pytest wrapper with -p no:terminal -s. The ordinary
suite skips this interactive fixture. Every provider/Git/account port is inert.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from tests.test_custom_plugins import root as root
from tests.test_plugin_skill_admission import setup as setup


@pytest.fixture
def tmp_path(tmp_path_factory):
    # Keep this inert path outside the generic credential-pattern redactor.
    # Production framing and the APP's exact workspace guard remain unchanged.
    return tmp_path_factory.mktemp("pair")


@pytest.mark.skipif(
    not os.environ.get("ALYSIS_P2_METADATA_FILE"), reason="opt-in paired stdio fixture"
)
def test_paired_stdio_fixture(setup):
    bridge, workspace, catalog, selected, calls = setup
    # Long enough for the real APP route, still bounded by the wrapper and broker.
    bridge._host_action_timeout_seconds = 5
    metadata = Path(os.environ["ALYSIS_P2_METADATA_FILE"])
    facts = Path(os.environ["ALYSIS_P2_FACTS_FILE"])
    temporary = metadata.with_suffix(".writing")
    temporary.write_text(
        json.dumps(dict(workspace=str(workspace), catalog=catalog, selection=selected)),
        encoding="utf-8",
    )
    temporary.replace(metadata)
    observed_identifiers = set()

    class ObservedInput:
        def readline(self, limit):
            line = sys.stdin.readline(limit)
            try:
                request = json.loads(line)
                if request.get("method") in {"chat.send", "run.start"}:
                    identifier = request["params"]["explicit_plugin_skill"]["admission_id"]
                    if isinstance(identifier, str):
                        observed_identifiers.add(identifier)
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
            return line  # Original bytes still traverse the production decoder.

    bridge.stdin, bridge.stdout = ObservedInput(), sys.stdout
    try:
        with redirect_stdout(sys.stderr):
            bridge.run()
    finally:
        wire = json.dumps(calls)
        facts.write_text(
            json.dumps(
                dict(
                    modelCalls=len(calls),
                    admissionIdentifiersObserved=len(observed_identifiers),
                    admissionIdentifierInModel=any(i in wire for i in observed_identifiers),
                    admissionActionInModel="plugin.skill.admit" in wire,
                    guidanceInModel="Read the notes." in wire,
                    jobs=[
                        dict(
                            status=j.status,
                            verifiedSuccess=(j.result or {}).get("verified_success"),
                            reason=(j.result or {}).get("error_details", {}).get("reason"),
                        )
                        for j in bridge._jobs.values()
                    ],
                )
            ),
            encoding="utf-8",
        )
