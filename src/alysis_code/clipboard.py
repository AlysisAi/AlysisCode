from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path


class ClipboardError(RuntimeError):
    pass


def _run_clipboard_writer(command: list[str], payload: bytes) -> str | None:
    try:
        proc = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if proc.returncode == 0:
        return None
    detail = _decode_text(proc.stderr) if proc.stderr else ""
    return detail or f"exit code {proc.returncode}"


def copy_text_to_clipboard(text: str) -> None:
    """Copy text to the host clipboard without shell interpolation.

    The ordered backends cover macOS, Windows/WSL, Wayland, and X11. PowerShell
    receives base64-encoded UTF-8 so non-ASCII selections survive the WSL/Windows
    process boundary without depending on the active console code page.
    """
    value = str(text or "")
    if not value:
        raise ClipboardError("Cannot copy empty text.")

    utf8_payload = value.encode("utf-8")
    candidates: list[tuple[str, list[str], bytes]] = [
        ("pbcopy", ["pbcopy"], utf8_payload),
        (
            "powershell.exe",
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$b=[Console]::In.ReadToEnd(); "
                    "$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b)); "
                    "Set-Clipboard -Value $t"
                ),
            ],
            base64.b64encode(utf8_payload),
        ),
        ("clip.exe", ["clip.exe"], utf8_payload),
        ("wl-copy", ["wl-copy", "--type", "text/plain;charset=utf-8"], utf8_payload),
        ("xclip", ["xclip", "-selection", "clipboard", "-in"], utf8_payload),
        ("xsel", ["xsel", "--clipboard", "--input"], utf8_payload),
    ]
    errors: list[str] = []
    for executable, command, payload in candidates:
        if shutil.which(executable) is None:
            continue
        error = _run_clipboard_writer(command, payload)
        if error is None:
            return
        errors.append(f"{executable}: {error}")
    detail = "; ".join(errors) if errors else "no supported clipboard command found"
    raise ClipboardError(f"Could not copy text to the system clipboard: {detail}")


def _default_output_path(root: Path) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return root / ".alysis_images" / f"clipboard_{ts}_{uuid.uuid4().hex[:6]}.png"


def _decode_text(blob: bytes) -> str:
    try:
        return blob.decode("utf-8").strip()
    except UnicodeDecodeError:
        return blob.decode("utf-16-le", errors="ignore").strip()


def _read_png_macos() -> bytes:
    # Pillow uses macOS's built-in osascript to read the native image clipboard.
    from PIL import Image, ImageGrab

    try:
        image = ImageGrab.grabclipboard()
        if not isinstance(image, Image.Image):
            raise ClipboardError("macOS clipboard does not contain image data.")
        with image, BytesIO() as output:
            image.save(output, format="PNG")
            return output.getvalue()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ClipboardError(f"Could not read macOS clipboard image: {exc}") from exc


def _read_png_wl_paste() -> bytes:
    if shutil.which("wl-paste") is None:
        raise ClipboardError("wl-paste is not installed.")
    proc = subprocess.run(
        ["wl-paste", "--type", "image/png", "--no-newline"],
        capture_output=True,
        check=False,
        timeout=20,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise ClipboardError("wl-paste clipboard does not contain PNG image data.")
    return proc.stdout


def _read_png_xclip() -> bytes:
    if shutil.which("xclip") is None:
        raise ClipboardError("xclip is not installed.")
    proc = subprocess.run(
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
        capture_output=True,
        check=False,
        timeout=20,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise ClipboardError("xclip clipboard does not contain PNG image data.")
    return proc.stdout


def _read_png_powershell() -> bytes:
    if shutil.which("powershell.exe") is None:
        raise ClipboardError("powershell.exe is not available.")
    script = (
        "$img = Get-Clipboard -Format Image -ErrorAction SilentlyContinue; "
        "if ($null -eq $img) { exit 3 }; "
        "$ms = New-Object System.IO.MemoryStream; "
        "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); "
        "[Console]::Out.Write([Convert]::ToBase64String($ms.ToArray()))"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        check=False,
        timeout=20,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise ClipboardError("PowerShell clipboard does not contain image data.")
    encoded = _decode_text(proc.stdout)
    if not encoded:
        raise ClipboardError("PowerShell clipboard returned empty image data.")
    try:
        return base64.b64decode(encoded, validate=True)
    except Exception as e:  # noqa: BLE001
        raise ClipboardError("PowerShell clipboard returned invalid image data.") from e


def _read_clipboard_png() -> bytes:
    if sys.platform == "darwin":
        readers = (_read_png_macos,)
        hint = (
            "Copy an image to the macOS clipboard (for example, Control+Shift+Command+4), "
            "then try again, or attach a saved image with /image <path>. "
        )
    else:
        readers = (_read_png_wl_paste, _read_png_xclip, _read_png_powershell)
        hint = "Try installing wl-paste or xclip, or ensure Windows clipboard has an image. "
    errors: list[str] = []
    for reader in readers:
        try:
            return reader()
        except ClipboardError as e:
            errors.append(str(e))
    detail = "; ".join(errors)
    raise ClipboardError(f"Could not read an image from clipboard. {hint}Details: {detail}")


def paste_clipboard_image(*, root: Path, output_path: str | None = None) -> Path:
    root = root.resolve()
    if output_path:
        p = Path(output_path).expanduser()
        dest = p if p.is_absolute() else root / p
    else:
        dest = _default_output_path(root)
    data = _read_clipboard_png()
    if not data:
        raise ClipboardError("Clipboard image data is empty.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest.resolve()
