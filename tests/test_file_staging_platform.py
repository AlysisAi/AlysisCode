"""Availability and pre-I/O refusal for the POSIX saved-file helper."""

import os

import pytest

from alysis_code import file_staging


def test_staging_advertises_actual_host_availability():
    assert file_staging.capability_status()["state"] == (
        "ready" if os.name == "posix" else "unavailable"
    )


@pytest.mark.skipif(
    os.name == "posix", reason="Unsupported-host refusal; POSIX effects have their own contracts"
)
def test_unsupported_staging_refuses_before_opening_the_named_file(tmp_path, monkeypatch):
    source = tmp_path / "private.txt"
    source.write_text("must not be read", encoding="utf-8")

    def unexpected_open(*args, **kwargs):
        pytest.fail("Unsupported staging attempted file I/O")

    monkeypatch.setattr(file_staging.os, "open", unexpected_open)
    with pytest.raises(file_staging.StagingError) as caught:
        with file_staging._open_regular_source(source):
            pytest.fail("Unsupported staging returned an open source")
    assert caught.value.code == "unsupported_platform"
