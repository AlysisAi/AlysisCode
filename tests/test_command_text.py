"""Behaviour of the shared decoding rule for child-process pipes (alysis_code.command_text)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from alysis_code import command_text as ct
from alysis_code.command_text import command_text, decode_command_output

GREEK = "Γεια σου κόσμε"


@pytest.fixture
def legacy(monkeypatch: pytest.MonkeyPatch):
    def use(codec: str) -> None:
        monkeypatch.setattr(ct, "legacy_output_encoding", lambda platform=None: codec)

    return use


def test_utf8_output_decodes_as_utf8(legacy) -> None:
    legacy("cp1253")
    assert decode_command_output(GREEK.encode("utf-8")) == GREEK


@pytest.mark.parametrize("codec", ["cp737", "cp1253", "cp437", "cp1252"])
def test_a_line_in_the_console_code_page_decodes_with_that_code_page(legacy, codec: str) -> None:
    legacy(codec)
    text = GREEK if codec in {"cp737", "cp1253"} else "Café déjà vu"
    assert decode_command_output(text.encode(codec)) == text


def test_each_line_is_decided_on_its_own(legacy) -> None:
    legacy("cp737")
    data = b"\n".join(
        [GREEK.encode("utf-8"), GREEK.encode("cp737"), b"plain ascii", "ναι".encode()]
    )
    assert decode_command_output(data).split("\n") == [GREEK, GREEK, "plain ascii", "ναι"]


def test_ascii_before_the_first_bad_byte_keeps_its_utf8_reading(legacy) -> None:
    legacy("cp1253")
    data = "naïve ".encode() + "λάθος".encode("cp1253")
    assert decode_command_output(data) == "naïve λάθος"


def test_decoding_never_raises(legacy) -> None:
    legacy("cp932")  # a multi-byte code page, where single bytes are often invalid
    every_byte = bytes(range(256)) * 3
    decoded = decode_command_output(every_byte)
    assert isinstance(decoded, str) and decoded


def test_writing_a_lone_surrogate_to_stdin_does_not_raise() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        input="a\udcffb",
        capture_output=True,
        check=False,
        **command_text(),
    )
    assert result.returncode == 0
    assert result.stdout == "a�b"


def test_disabled_text_mode_adds_no_arguments() -> None:
    assert command_text(False) == {}
    assert command_text() == {"text": True, "encoding": "utf-8", "errors": "alysis-command-output"}


def _child(payload: bytes) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({payload!r})"]


def test_a_child_writing_utf8_and_code_page_lines_is_read_in_full(legacy) -> None:
    legacy("cp737")
    payload = GREEK.encode("utf-8") + b"\r\n" + GREEK.encode("cp737") + b"\r\nend\r\n"
    result = subprocess.run(_child(payload), capture_output=True, check=False, **command_text())
    # Text mode keeps translating newlines exactly as before.
    assert result.stdout == f"{GREEK}\n{GREEK}\nend\n"


def test_a_streamed_child_is_read_line_by_line_across_reads(legacy) -> None:
    legacy("cp1253")
    # Each long line spans more than one 8 KiB read, so reads end inside both kinds of line.
    code = (
        "import sys; g = " + repr(GREEK) + "; sys.stdout.buffer.write("
        "g.encode('cp1253') * 700 + b'\\n' + g.encode('utf-8') * 700 + b'\\ntail\\n')"
    )
    with subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, **command_text()
    ) as process:
        assert process.stdout is not None
        received = [line.rstrip("\n") for line in process.stdout]
    assert received == [GREEK * 700, GREEK * 700, "tail"]


def test_legacy_encoding_outside_windows_is_the_locale_codec(monkeypatch) -> None:
    monkeypatch.setattr(ct.locale, "getpreferredencoding", lambda do_setlocale=True: "ISO-8859-7")
    assert ct.legacy_output_encoding("linux") == "iso8859-7"


def test_legacy_encoding_on_windows_is_the_console_code_page(monkeypatch) -> None:
    monkeypatch.setattr(ct, "_windows_console_code_page", lambda: 737)
    assert ct.legacy_output_encoding("win32") == "cp737"
    monkeypatch.setattr(ct, "_windows_console_code_page", lambda: 0)
    monkeypatch.setattr(ct.locale, "getpreferredencoding", lambda do_setlocale=True: "cp1253")
    assert ct.legacy_output_encoding("win32") == "cp1253"


@pytest.mark.skipif(sys.platform != "win32", reason="reads the real Windows console code page")
def test_the_real_windows_code_page_is_used() -> None:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    expected = kernel32.GetConsoleOutputCP() or kernel32.GetOEMCP()
    assert ct.legacy_output_encoding() == ct.codecs.lookup(f"cp{expected}").name
