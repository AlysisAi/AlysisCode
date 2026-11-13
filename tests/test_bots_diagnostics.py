"""Local owls diagnostics keep where something failed, never what it said (AL, 2026-11-07)."""

import json

import pytest

from alysis_code.bots import diagnostics
from alysis_code.bots.compute import ComputeCreationRefused
from alysis_code.bots.local_resources import storage_refusal


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "_path", None)
    diagnostics.configure(tmp_path)
    return tmp_path


def entries(folder):
    return [json.loads(line) for line in (folder / diagnostics.FILE).read_text().splitlines()]


def raise_and_record(error):
    try:
        raise error
    except Exception as caught:
        diagnostics.record("computer_start_failed", caught)


def test_nothing_is_written_until_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "_path", None)
    diagnostics.record("service_started", cores=4)
    assert not (tmp_path / diagnostics.FILE).exists()


def test_errors_keep_type_and_location_but_never_their_text(folder):
    raise_and_record(RuntimeError("sk-live-secret at C:/Users/someone/private.txt"))
    entry = entries(folder)[0]
    assert entry["event"] == "computer_start_failed" and entry["error"] == "RuntimeError"
    assert "secret" not in json.dumps(entry) and "someone" not in json.dumps(entry)


def test_refusals_keep_their_person_facing_words_and_code(folder):
    raise_and_record(
        ComputeCreationRefused(
            storage_refusal(7 * 1024**3, 5 * 1024**3), code="insufficient_local_storage"
        )
    )
    entry = entries(folder)[0]
    assert entry["code"] == "insufficient_local_storage"
    assert entry["message"].startswith("Not enough free disk space")


def test_only_short_plain_facts_are_kept(folder):
    diagnostics.record("service_started", cores=16, engine="0.14.2", note="x" * 500, things=["a"])
    entry = entries(folder)[0]
    assert entry["cores"] == 16 and entry["engine"] == "0.14.2"
    assert "note" not in entry and "things" not in entry


def test_the_file_stays_small(folder, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_BYTES", 4096)
    monkeypatch.setattr(diagnostics, "KEEP_LINES", 10)
    for index in range(300):
        diagnostics.record("task_failed", step=index)
    assert (folder / diagnostics.FILE).stat().st_size < 8192
    assert entries(folder)[-1]["step"] == 299
