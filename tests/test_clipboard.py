from __future__ import annotations

import base64
import subprocess
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageGrab

from alysis_code.clipboard import (
    ClipboardError,
    copy_text_to_clipboard,
    paste_clipboard_image,
)


@pytest.mark.parametrize(
    ("available", "expected_command"),
    [
        ("pbcopy", ["pbcopy"]),
        ("clip.exe", ["clip.exe"]),
        ("wl-copy", ["wl-copy", "--type", "text/plain;charset=utf-8"]),
        ("xclip", ["xclip", "-selection", "clipboard", "-in"]),
        ("xsel", ["xsel", "--clipboard", "--input"]),
    ],
)
def test_copy_text_to_clipboard_uses_available_platform_writer(
    available: str,
    expected_command: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bytes]] = []

    monkeypatch.setattr(
        "alysis_code.clipboard.shutil.which",
        lambda command: command if command == available else None,
    )

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("alysis_code.clipboard.subprocess.run", fake_run)

    copy_text_to_clipboard("hello κόσμε")

    assert calls == [(expected_command, "hello κόσμε".encode())]


def test_copy_text_to_clipboard_uses_unicode_safe_powershell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "alysis_code.clipboard.shutil.which",
        lambda command: command if command == "powershell.exe" else None,
    )

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("alysis_code.clipboard.subprocess.run", fake_run)

    copy_text_to_clipboard("κόσμε")

    assert captured["command"][0] == "powershell.exe"  # type: ignore[index]
    assert base64.b64decode(captured["input"]) == "κόσμε".encode()  # type: ignore[arg-type]


def test_copy_text_to_clipboard_errors_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("alysis_code.clipboard.shutil.which", lambda _command: None)

    with pytest.raises(ClipboardError, match="no supported clipboard command found"):
        copy_text_to_clipboard("hello")


def test_paste_clipboard_image_via_powershell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = b"\x89PNG\r\n\x1a\nfake"
    monkeypatch.setattr("alysis_code.clipboard.sys", SimpleNamespace(platform="win32"))

    def fake_which(cmd: str) -> str | None:
        if cmd == "powershell.exe":
            return "powershell.exe"
        return None

    def fake_run(args: list[str], **_kwargs) -> subprocess.CompletedProcess[bytes]:
        assert args[0] == "powershell.exe"
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=base64.b64encode(png),
            stderr=b"",
        )

    monkeypatch.setattr("alysis_code.clipboard.shutil.which", fake_which)
    monkeypatch.setattr("alysis_code.clipboard.subprocess.run", fake_run)

    path = paste_clipboard_image(root=tmp_path)
    assert path.exists()
    assert path.read_bytes() == png


def test_paste_clipboard_image_errors_when_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("alysis_code.clipboard.sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr("alysis_code.clipboard.shutil.which", lambda _cmd: None)

    with pytest.raises(ClipboardError, match="Could not read an image from clipboard"):
        paste_clipboard_image(root=tmp_path)


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_paste_clipboard_image_on_macos(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("alysis_code.clipboard.sys", SimpleNamespace(platform="darwin"))
    color = (12, 34, 56, 78) if mode == "RGBA" else (12, 34, 56)
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: Image.new(mode, (3, 2), color))

    def unexpected_command(_cmd: str) -> None:
        pytest.fail("macOS must not try Linux or Windows clipboard commands")

    monkeypatch.setattr("alysis_code.clipboard.shutil.which", unexpected_command)

    path = paste_clipboard_image(root=tmp_path, output_path="images/pasted.png")

    assert path == (tmp_path / "images" / "pasted.png").resolve()
    with Image.open(path) as saved:
        assert saved.format == "PNG"
        assert saved.mode == mode
        assert saved.size == (3, 2)
        assert saved.getpixel((0, 0)) == color


@pytest.mark.parametrize("clipboard_contents", [None, ["copied-file.png"]])
def test_paste_clipboard_image_on_macos_requires_image_data(
    clipboard_contents: list[str] | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("alysis_code.clipboard.sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: clipboard_contents)

    with pytest.raises(ClipboardError, match="macOS clipboard does not contain image data") as exc:
        paste_clipboard_image(root=tmp_path)

    assert "wl-paste" not in str(exc.value)
    assert "xclip" not in str(exc.value)
    assert "Windows" not in str(exc.value)
    assert "/image <path>" in str(exc.value)
    assert not (tmp_path / ".alysis_images").exists()


@pytest.mark.parametrize(
    "error",
    [
        OSError("osascript unavailable"),
        ValueError("malformed image"),
        subprocess.SubprocessError("failed"),
    ],
)
def test_paste_clipboard_image_on_macos_reports_backend_errors(
    error: Exception,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("alysis_code.clipboard.sys", SimpleNamespace(platform="darwin"))

    def failing_grab():
        raise error

    monkeypatch.setattr(ImageGrab, "grabclipboard", failing_grab)

    with pytest.raises(ClipboardError, match="Could not read an image from clipboard") as exc:
        paste_clipboard_image(root=tmp_path)

    assert str(error) in str(exc.value)
    assert "macOS" in str(exc.value)
    assert not (tmp_path / ".alysis_images").exists()


@pytest.mark.parametrize("backend", ["wl-paste", "xclip"])
def test_paste_clipboard_image_on_linux(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("alysis_code.clipboard.sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        "alysis_code.clipboard.shutil.which",
        lambda command: command if command == backend else None,
    )
    png = b"\x89PNG\r\n\x1a\nfake"

    def fake_run(args: list[str], **_kwargs) -> subprocess.CompletedProcess[bytes]:
        assert args[0] == backend
        return subprocess.CompletedProcess(args, 0, stdout=png, stderr=b"")

    monkeypatch.setattr("alysis_code.clipboard.subprocess.run", fake_run)

    assert paste_clipboard_image(root=tmp_path).read_bytes() == png


def test_macos_clipboard_through_real_pillow_backend(tmp_path, monkeypatch):
    """Only fake the OS response; run Pillow's AppleScript parser and PNG decoder."""
    monkeypatch.setattr("alysis_code.clipboard.sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(ImageGrab, "sys", SimpleNamespace(platform="darwin"))
    with Image.new("RGBA", (7, 5), (12, 34, 56, 78)) as source, BytesIO() as buffer:
        source.save(buffer, format="PNG")
        payload = buffer.getvalue()

    def fake_run(args, **kwargs):
        assert args == ["osascript", "-e", "get the clipboard as \u00abclass PNGf\u00bb"]
        assert kwargs["capture_output"] is True
        output = f"\u00abdata PNGf{payload.hex().upper()}\u00bb\n".encode()
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(ImageGrab, "subprocess", SimpleNamespace(run=fake_run))

    path = paste_clipboard_image(root=tmp_path)

    assert path.parent == tmp_path / ".alysis_images"
    with Image.open(path) as saved:
        assert saved.format == "PNG"
        assert saved.size == (7, 5)
        assert saved.getpixel((6, 4)) == (12, 34, 56, 78)


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, b""),
        (0, b""),
        (0, "\u00abdata PNGfXYZ\u00bb\n".encode()),
        (0, "\u00abdata PNGf1234\u00bb\n".encode()),
    ],
)
def test_macos_clipboard_rejects_failed_or_corrupt_os_output(
    returncode, stdout, tmp_path, monkeypatch
):
    monkeypatch.setattr("alysis_code.clipboard.sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(ImageGrab, "sys", SimpleNamespace(platform="darwin"))

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=b"read failed")

    monkeypatch.setattr(ImageGrab, "subprocess", SimpleNamespace(run=fake_run))

    with pytest.raises(ClipboardError, match="Could not read an image from clipboard"):
        paste_clipboard_image(root=tmp_path)

    assert not (tmp_path / ".alysis_images").exists()
