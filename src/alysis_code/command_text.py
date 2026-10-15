"""Text decoding for the pipes of child processes.

Why this module exists
----------------------
``subprocess`` in text mode decodes a child's output with one fixed codec. Without an explicit
``encoding`` that codec is the locale's, which on Windows is the ANSI code page (cp1252,
cp1253, ...). Children rarely write in it: Python children write UTF-8 (the desktop app sets
``PYTHONIOENCODING=utf-8``), git writes UTF-8, and console programs such as cmd's ``dir`` or
Windows PowerShell write the console's code page (the OEM code page: cp437, cp737, ...). The
first byte the codec cannot decode raises ``UnicodeDecodeError`` in subprocess's reader, and
the caller receives no output at all.

The rule
--------
Every text-mode pipe is opened with ``**command_text()``: UTF-8, with the
``alysis-command-output`` error handler registered below.

* Valid UTF-8 decodes as UTF-8.
* When a line holds bytes that are not valid UTF-8, the rest of that line, from the first such
  byte, is decoded with :func:`legacy_output_encoding`: the console's output code page on
  Windows (the OEM code page when the process has no console), the locale's encoding elsewhere.
  Bytes that codec cannot decode either become U+FFFD.
* Text written to a child's stdin is UTF-8; a lone surrogate becomes U+FFFD.

Decoding never raises, and the same bytes always give the same text on the same machine. A line
is the unit because a line is almost always written by one program in one encoding.
``tests/test_command_text_usage.py`` fails when a subprocess call chooses text mode, an encoding
or an error handler any other way.
"""

from __future__ import annotations

import codecs
import locale
import sys
from typing import Any

COMMAND_OUTPUT_ERRORS = "alysis-command-output"
COMMAND_OUTPUT_ENCODING = "utf-8"


def _codec_name(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def _windows_console_code_page() -> int:
    """The code page console programs started by this process write in, or 0 if unknown."""

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # GetConsoleOutputCP is 0 when the process has no console; children then get a new
        # console, whose output code page is the OEM code page.
        return int(kernel32.GetConsoleOutputCP()) or int(kernel32.GetOEMCP())
    except Exception:  # noqa: BLE001 - any failure falls back to the locale below
        return 0


def legacy_output_encoding(platform: str | None = None) -> str:
    """The codec for output bytes that are not valid UTF-8.

    Read on every use rather than cached: a child running ``chcp`` changes the console's code
    page for the children after it.
    """

    target = sys.platform if platform is None else platform
    if target == "win32":
        code_page = _windows_console_code_page()
        name = _codec_name(f"cp{code_page}") if code_page else None
        if name:
            return name
    return _codec_name(locale.getpreferredencoding(False)) or COMMAND_OUTPUT_ENCODING


def _command_output_error_handler(error: UnicodeError) -> tuple[str, int]:
    if isinstance(error, UnicodeDecodeError):
        data = bytes(error.object)
        newline = data.find(b"\n", error.start)
        end = len(data) if newline < 0 else max(newline, error.end)
        segment = data[error.start : end]
        return segment.decode(legacy_output_encoding(), "replace"), end
    if isinstance(error, UnicodeEncodeError):
        # Encoders accept only ASCII text or bytes from a handler, so hand back U+FFFD encoded.
        replacement = "\ufffd".encode(error.encoding, "replace")
        return replacement * (error.end - error.start), error.end
    raise error


codecs.register_error(COMMAND_OUTPUT_ERRORS, _command_output_error_handler)


def command_text(enabled: bool = True) -> dict[str, Any]:
    """Keyword arguments that open a child's pipes in text mode under the rule above.

    ``enabled=False`` returns no arguments, so the pipes stay binary. Use it as
    ``subprocess.run(args, capture_output=True, **command_text())``.
    """

    if not enabled:
        return {}
    return {"text": True, "encoding": COMMAND_OUTPUT_ENCODING, "errors": COMMAND_OUTPUT_ERRORS}


def decode_command_output(data: bytes | bytearray | memoryview | str | None) -> str:
    """Decode captured output bytes under the same rule (no newline translation)."""

    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return bytes(data).decode(COMMAND_OUTPUT_ENCODING, COMMAND_OUTPUT_ERRORS)
