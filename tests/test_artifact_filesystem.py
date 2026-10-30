from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, fields, is_dataclass

import pytest

from alysis_code.artifact_filesystem import FilesystemArtifactLayout
from alysis_code.checkpoint_settings import CheckpointSettings
from alysis_code.config import AnytimeCheckpointConfig
from alysis_code.session_artifacts import SessionArtifactLayout
from alysis_code.tools.fs import SensitivePathClassification, classify_sensitive_path


def test_public_artifact_layout_keeps_its_frozen_dataclass_contract(tmp_path):
    layout = SessionArtifactLayout(tmp_path, "custom/artifacts")
    assert is_dataclass(layout)
    assert [field.name for field in fields(layout)] == ["filesystem_root", "locator_prefix"]
    assert asdict(layout) == {"filesystem_root": tmp_path, "locator_prefix": "custom/artifacts"}
    assert layout == SessionArtifactLayout(tmp_path, "custom/artifacts")
    assert hash(layout) == hash(SessionArtifactLayout(tmp_path, "custom/artifacts"))
    with pytest.raises(FrozenInstanceError):
        layout.locator_prefix = "different"


@pytest.mark.parametrize("worker_writes", [True, False])
def test_worker_and_public_layout_exchange_content_bound_handles(tmp_path, worker_writes):
    public = SessionArtifactLayout(tmp_path, "custom/artifacts")
    worker = FilesystemArtifactLayout(tmp_path, "custom/artifacts")
    artifact = tmp_path / "nested" / "disposable note.txt"
    artifact.parent.mkdir()
    artifact.write_bytes(b"verified bytes")
    writer, reader = (worker, public) if worker_writes else (public, worker)
    handle = writer.register_artifact(artifact)
    assert reader.resolve_handle(handle) == "custom/artifacts/nested/disposable note.txt"
    assert reader.resolve_locator(reader.resolve_handle(handle)).read_bytes() == b"verified bytes"
    with pytest.raises(ValueError, match="invalid path segment"):
        reader.resolve_locator("custom/artifacts/../outside")
    artifact.write_bytes(b"changed bytes")
    with pytest.raises(FileNotFoundError, match="no longer available"):
        reader.resolve_handle(handle)


def test_private_checkpoint_settings_preserve_wire_fields_and_immutability():
    payload = AnytimeCheckpointConfig(objective_command="python measure.py").model_dump(mode="json")
    settings = CheckpointSettings(**payload)
    assert settings._asdict() == payload
    with pytest.raises(AttributeError):
        settings.max_files = 1
    with pytest.raises(TypeError):
        CheckpointSettings(**payload, unexpected=True)
    missing = {key: value for key, value in payload.items() if key != "objective_direction"}
    with pytest.raises(TypeError):
        CheckpointSettings(**missing)


def test_public_sensitive_path_result_keeps_its_dataclass_contract():
    classification = classify_sensitive_path("nested/.env")
    assert isinstance(classification, SensitivePathClassification)
    assert is_dataclass(classification)
    assert asdict(classification) == {"sensitive": True, "category": "environment_file"}
    with pytest.raises(FrozenInstanceError):
        classification.sensitive = False
