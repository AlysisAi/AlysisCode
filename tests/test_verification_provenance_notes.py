"""Command provenance notes must reflect source metadata, not authorship guesses."""

from __future__ import annotations

from dataclasses import replace

import pytest

from alysis_code.agent.verification import _verification_evidence_note
from alysis_code.agent.verification_evidence import (
    VerificationEvidence,
    VerificationEvidenceCategory,
    classify_verification_evidence,
)
from alysis_code.verification_contract import build_verification_command_spec

_COMMAND = "python3 -m unittest discover -s tests -v"
_OTHER = "python3 -m unittest discover -s integration -v"
_UNCONFIRMED = "Verification command provenance is unconfirmed."


def _evidence(command=_COMMAND, *, known=None):
    return classify_verification_evidence(
        command,
        known_verification_commands=[_COMMAND] if known is None else known,
        authoritative=True,
        exit_code=0,
        real_execution=True,
        output="Ran 1 test in 0.001s\n\nOK\n",
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("config.verify_commands", "EXPLICIT_USER_COMMAND"),
        ("cli.verify_cmd", "EXPLICIT_USER_COMMAND"),
        ("environment.authoritative_verification_commands", "HOST_AUTHORITATIVE"),
        ("repo_scan.likely_test_commands", "INFERRED_HEURISTIC"),
        ("verification_fallback.detected_runner", "INFERRED_HEURISTIC"),
        ("repository.declared_check", "PREEXISTING_REPO_NATIVE"),
    ],
)
def test_typed_source_overrides_legacy_repo_native_label(source, expected):
    spec = build_verification_command_spec(_COMMAND, source=source, contract_type="repo_native")
    result = {
        "verification_contract_type": "repo_native",
        "verification_command_specs": [spec.as_payload()],
    }
    assert _verification_evidence_note(_evidence(), result=result) == (
        f"Matched command provenance: {expected}."
    )


def test_batch_reports_all_matched_sources_without_borrowing_unrelated_provenance():
    first = build_verification_command_spec(
        _COMMAND, source="config.verify_commands", contract_type="repo_native"
    )
    second = build_verification_command_spec(
        _OTHER, source="repo_scan.likely_test_commands", contract_type="repo_native"
    )
    unrelated = build_verification_command_spec(
        "pytest -q", source="repository.declared_check", contract_type="repo_native"
    )
    evidence = replace(_evidence(), covered_verification_commands=(_COMMAND, _OTHER))
    result = {
        "verification_command_specs": [
            first.as_payload(),
            second.as_payload(),
            unrelated.as_payload(),
        ]
    }
    assert _verification_evidence_note(evidence, result=result) == (
        "Matched command provenance: EXPLICIT_USER_COMMAND, INFERRED_HEURISTIC."
    )


@pytest.mark.parametrize(
    "specs",
    [
        None,
        [],
        [None],
        [{"original_text": _COMMAND}],
        [{"original_text": _COMMAND, "provenance": "unknown_future_source"}],
        [{"original_text": _OTHER, "provenance": "PREEXISTING_REPO_NATIVE"}],
    ],
)
def test_missing_invalid_or_unrelated_specs_do_not_confirm_legacy_label(specs):
    assert (
        _verification_evidence_note(
            _evidence(),
            result={
                "verification_contract_type": "repo_native",
                "verification_command_specs": specs,
            },
        )
        == _UNCONFIRMED
    )


def test_partial_batch_metadata_cannot_label_every_matched_command():
    evidence = replace(_evidence(), covered_verification_commands=(_COMMAND, _OTHER))
    assert (
        _verification_evidence_note(
            evidence,
            result={
                "verification_command_specs": [
                    {"original_text": _COMMAND, "provenance": "PREEXISTING_REPO_NATIVE"}
                ]
            },
        )
        == _UNCONFIRMED
    )


@pytest.mark.parametrize("category", list(VerificationEvidenceCategory))
def test_evidence_category_alone_is_not_source_or_authorship_proof(category):
    evidence = VerificationEvidence(category=category, normalized_command=_COMMAND)
    assert _verification_evidence_note(evidence) == (
        "" if category == VerificationEvidenceCategory.NOT_VERIFICATION else _UNCONFIRMED
    )


def test_different_supplemental_selection_does_not_inherit_contract_source():
    result = {
        "verification_command_specs": [
            {"original_text": _COMMAND, "provenance": "PREEXISTING_REPO_NATIVE"}
        ]
    }
    note = _verification_evidence_note(_evidence(_OTHER), result=result)
    assert note == (
        "Additional verification execution; its selection does not establish "
        "coverage of the resolved verification command."
    )


def test_supplemental_task_check_does_not_assert_authorship_from_contract_presence():
    evidence = VerificationEvidence(
        category=VerificationEvidenceCategory.TASK_ACCEPTANCE,
        normalized_command="python3 checks.py",
        reason="supplemental_only_contract_exists",
        real_execution=True,
        supplemental_only=True,
    )
    assert _verification_evidence_note(evidence) == (
        "Additional task-check execution; it does not establish coverage "
        "of the resolved verification command."
    )
