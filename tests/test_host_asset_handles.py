from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from alysis_code.assets import local_tools
from alysis_code.config import AppConfig
from alysis_code.host_file_handles import host_file_action
from alysis_code.host_file_policy import HostFilePolicy, HostFilePolicyError


def image_bytes():
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(data, format="PNG")
    return data.getvalue()


def test_asset_pixels_are_decoded_from_admitted_bytes_not_a_reopened_path(tmp_path, monkeypatch):
    source = tmp_path / "ordinary.png"
    source.write_bytes(image_bytes())
    original = Image.open
    seen = []

    def checked(value, *args, **kwargs):
        assert not isinstance(value, (str, Path))
        seen.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(Image, "open", checked)
    viewer = local_tools.LocalAssetViewer(root=tmp_path, cfg=AppConfig())
    with host_file_action(HostFilePolicy((), ()), readonly=True):
        result = viewer._view({"path": source.name})
    assert result["source_dimensions"] == [8, 8]
    assert seen and result["visual_delivery"] == "pending_context_append"


def test_asset_private_hardlink_insertion_never_reaches_decoder(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    secret = private / "secret.png"
    secret.write_bytes(image_bytes())
    source = tmp_path / "ordinary.png"
    source.write_bytes(image_bytes())
    original = local_tools.open_host_file_read
    swapped = False

    def swap(path):
        nonlocal swapped
        if not swapped:
            swapped = True
            source.unlink()
            source.hardlink_to(secret)
        return original(path)

    monkeypatch.setattr(local_tools, "open_host_file_read", swap)
    viewer = local_tools.LocalAssetViewer(root=tmp_path, cfg=AppConfig())
    with host_file_action(HostFilePolicy((private,), ()), readonly=True):
        with pytest.raises(HostFilePolicyError):
            viewer._view({"path": source.name})
    assert not viewer._pending


def test_posix_video_decoder_receives_validated_descriptor(tmp_path, monkeypatch):
    source = tmp_path / "ordinary.mp4"
    source.write_bytes(b"synthetic bounded video")
    seen = []

    class Posix:
        name = "posix"

        def __getattr__(self, name):
            return getattr(os, name)

    def decode(argv, **kwargs):
        (fd,) = kwargs["pass_fds"]
        assert os.read(fd, 4096) == b"synthetic bounded video"
        assert argv[argv.index("-i") + 1].endswith(f"/fd/{fd}")
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, image_bytes(), b"pts_time:0.5 s:8x8")

    monkeypatch.setattr(local_tools, "os", Posix())
    monkeypatch.setattr(local_tools.shutil, "which", lambda _: "synthetic-ffmpeg")
    monkeypatch.setattr(local_tools.subprocess, "run", decode)
    viewer = local_tools.LocalAssetViewer(root=tmp_path, cfg=AppConfig())
    with host_file_action(HostFilePolicy((), ()), readonly=True):
        result = viewer._view({"path": source.name, "timestamp_s": 0.5})
    assert seen and result["frame_timestamp_s"] == 0.5
