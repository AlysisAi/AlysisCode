"""Composer attachments: path detection, chip registry, and the live TUI wiring."""

from __future__ import annotations

import os
import subprocess
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageGrab

from alysis_code.cli_impl.tui import attachments as attach
from alysis_code.cli_impl.tui.state import TuiState

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _png(path, size: int = 64):
    path.write_bytes(PNG_HEADER + b"0" * size)
    return path


# ---------------------------------------------------------------- normalizing


def test_normalize_strips_the_quoting_terminals_add():
    assert attach.normalize_dropped_path(r'"C:\Users\me\a b.png"') == r"C:\Users\me\a b.png"
    assert attach.normalize_dropped_path("'/home/me/a b.png'") == "/home/me/a b.png"


def test_normalize_unescapes_posix_drops_but_leaves_windows_separators():
    assert attach.normalize_dropped_path(r"/home/me/a\ b.png") == "/home/me/a b.png"
    # A backslash is the separator on Windows, never an escape.
    assert attach.normalize_dropped_path(r"C:\Users\me\build") == r"C:\Users\me\build"


def test_normalize_decodes_file_uris_on_both_platforms():
    assert attach.normalize_dropped_path("file:///home/me/a%20b.png") == "/home/me/a b.png"
    assert attach.normalize_dropped_path("file:///C:/Users/me/a.png") == "C:/Users/me/a.png"


def test_normalize_rejects_empty_input():
    assert attach.normalize_dropped_path("   ") is None


# ----------------------------------------------------------------- detection


def test_detects_a_single_unquoted_path_containing_spaces(tmp_path):
    shot = _png(tmp_path / "my shot.png")

    assert attach.detect_dropped_paths(str(shot)) == [shot.resolve()]


def test_detects_several_quoted_paths_from_one_drop(tmp_path):
    shot = _png(tmp_path / "shot.png")
    notes = tmp_path / "notes.txt"
    notes.write_text("hi", encoding="utf-8")

    assert attach.detect_dropped_paths(f'"{shot}" "{notes}"') == [shot.resolve(), notes.resolve()]


def test_detects_a_multi_line_drop(tmp_path):
    shot = _png(tmp_path / "shot.png")
    notes = tmp_path / "notes.txt"
    notes.write_text("hi", encoding="utf-8")

    assert attach.detect_dropped_paths(f"{shot}\n{notes}\n") == [shot.resolve(), notes.resolve()]


def test_prose_mentioning_a_real_path_stays_an_ordinary_paste(tmp_path):
    shot = _png(tmp_path / "shot.png")

    assert attach.detect_dropped_paths(f"look at {shot} please") == []


def test_a_bare_relative_word_is_never_an_attachment(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    assert attach.detect_dropped_paths("src") == []


def test_a_missing_path_stays_an_ordinary_paste(tmp_path):
    assert attach.detect_dropped_paths(str(tmp_path / "gone.png")) == []


def test_one_unresolvable_path_rejects_the_whole_drop(tmp_path):
    shot = _png(tmp_path / "shot.png")

    assert attach.detect_dropped_paths(f'"{shot}" "{tmp_path / "gone.txt"}"') == []


# --------------------------------------------------------------- WSL drops


def test_windows_drive_paths_translate_to_their_wsl_mount():
    translated = attach.translate_windows_path(r"C:\Users\me\shot.png")

    assert translated == "/mnt/c/Users/me/shot.png"


def test_a_custom_automount_root_is_honoured():
    translated = attach.translate_windows_path(r"D:\data\a.pdf", mount_root="/windows")

    assert translated == "/windows/d/data/a.pdf"


def test_explorer_wsl_unc_paths_drop_back_to_their_distro_path():
    for prefix in (r"\\wsl$\Ubuntu", r"\\wsl.localhost\Ubuntu-24.04"):
        assert attach.translate_windows_path(prefix + r"\home\me\a.png") == "/home/me/a.png"


def test_a_posix_path_is_not_a_windows_path():
    assert attach.translate_windows_path("/home/me/a.png") is None
    assert attach.translate_windows_path("") is None


def test_a_dropped_windows_path_resolves_through_the_wsl_mount(tmp_path, monkeypatch):
    shot = _png(tmp_path / "shot.png")
    # Pretend tmp_path is what "C:\drop" maps to inside the distro.
    monkeypatch.setattr(attach, "running_under_wsl", lambda: True)
    monkeypatch.setattr(
        attach,
        "translate_windows_path",
        lambda text, mount_root=None: os.fspath(shot) if text.lower().startswith("c:") else None,
    )

    assert attach.detect_dropped_paths(r"C:\drop\shot.png") == [shot.resolve()]


# ------------------------------------------------------- drops typed as keys


def test_a_completed_quoted_run_is_recognised_as_a_drop():
    assert attach.trailing_drop_run(r'ask about "C:\Users\me\a.png"') == (r'"C:\Users\me\a.png"')


def test_a_file_uri_is_recognised_as_a_drop():
    assert attach.trailing_drop_run("look file:///home/me/a.png") == "file:///home/me/a.png"


def test_a_half_typed_path_is_not_a_drop_yet():
    # No closing quote: still being written, so the composer leaves it alone.
    assert attach.trailing_drop_run(r'"C:\Users\me') is None
    # A quoted word with no separator is just a quoted word.
    assert attach.trailing_drop_run('he said "hello"') is None
    # A trailing space means the run is already finished and handled.
    assert attach.trailing_drop_run(r'"C:\a\b.png" ') is None
    assert attach.trailing_drop_run("") is None


# ------------------------------------------------------------- classification


def test_classification_splits_images_files_and_folders(tmp_path):
    shot = _png(tmp_path / "shot.png")
    notes = tmp_path / "notes.txt"
    notes.write_text("hi", encoding="utf-8")

    assert attach.classify_attachment(shot) == "image"
    assert attach.classify_attachment(notes) == "file"
    assert attach.classify_attachment(tmp_path) == "folder"


def test_an_oversized_image_is_refused_at_attach_time(tmp_path):
    shot = _png(tmp_path / "huge.png", size=4096)

    with pytest.raises(attach.AttachmentError) as excinfo:
        attach.classify_attachment(shot, max_image_bytes=128)

    assert "huge.png" in str(excinfo.value)


# ---------------------------------------------------------------- the registry


def _registry_with(tmp_path):
    shot = _png(tmp_path / "shot.png")
    notes = tmp_path / "notes.txt"
    notes.write_text("hi", encoding="utf-8")
    registry = attach.AttachmentRegistry()
    return registry, registry.add_path(shot), registry.add_path(notes), shot, notes


def test_chips_are_numbered_per_kind(tmp_path):
    registry, image, file_, _shot, _notes = _registry_with(tmp_path)
    second_image = registry.add_path(_png(tmp_path / "other.png"))

    assert image.token == "[Image #1]"
    assert file_.token == "[File #1]"
    assert second_image.token == "[Image #2]"


def test_expand_keeps_image_chips_and_resolves_file_chips(tmp_path):
    registry, image, file_, shot, notes = _registry_with(tmp_path)
    text = f"look at {image.token} and {file_.token}"

    expanded = registry.expand(text)

    assert image.token in expanded
    assert os.fspath(notes.resolve()) in expanded
    assert registry.image_paths(text) == [os.fspath(shot.resolve())]


def test_a_path_with_spaces_is_quoted_into_the_instruction(tmp_path):
    notes = tmp_path / "my notes.txt"
    notes.write_text("hi", encoding="utf-8")
    registry = attach.AttachmentRegistry()
    chip = registry.add_path(notes)

    assert registry.expand(chip.token) == f'"{os.fspath(notes.resolve())}"'


def test_a_deleted_chip_is_forgotten_and_a_look_alike_is_not_adopted(tmp_path):
    registry, image, file_, _shot, _notes = _registry_with(tmp_path)

    registry.retain_tokens(image.token)

    assert sorted(registry.snapshot()) == [image.token]
    assert registry.image_paths("[Image #9]") == []
    # A token the registry has forgotten is sent as the literal text it is.
    assert registry.expand(file_.token) == file_.token


def test_restore_re_adopts_chips_without_renumbering(tmp_path):
    registry, image, _file, _shot, _notes = _registry_with(tmp_path)
    registry.retain_tokens("")
    assert registry.snapshot() == {}

    registry.restore([image])

    assert registry.image_paths(image.token) == [os.fspath(image.path)]
    assert registry.add_path(_png(tmp_path / "next.png")).token == "[Image #2]"


def test_the_hint_names_one_attachment_and_counts_many(tmp_path):
    registry, image, file_, _shot, _notes = _registry_with(tmp_path)

    assert registry.hint(image.token).startswith("image #1")
    assert "shot.png" in registry.hint(image.token)
    assert registry.hint(f"{image.token} {file_.token}").startswith("2 attachments")
    assert registry.hint("nothing attached") == ""


def test_relative_paths_resolve_against_the_session_workdir(tmp_path):
    shot = _png(tmp_path / "shot.png")
    registry = attach.AttachmentRegistry()

    chip = registry.add_path("shot.png", root=tmp_path)

    assert chip.path == shot.resolve()


def test_a_quoted_path_typed_at_slash_image_is_unquoted(tmp_path):
    shot = _png(tmp_path / "my shot.png")
    registry = attach.AttachmentRegistry()

    chip = registry.add_path(f'"{shot}"')

    assert chip.path == shot.resolve()


def test_a_missing_path_is_refused_with_its_name(tmp_path):
    registry = attach.AttachmentRegistry()

    with pytest.raises(attach.AttachmentError):
        registry.add_path(tmp_path / "gone.png")


# ------------------------------------------------------------ the live composer


def _bracketed_paste(text: str) -> str:
    return f"\x1b[200~{text}\x1b[201~"


def _run_headless_attachment_submission(monkeypatch, keys: str, *, workspace_root=None):
    """Drive the real TUI over a pipe and record what each turn received."""

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import app as app_module

    delivered: list[tuple[str, list[str]]] = []

    class RecordingSession:
        def __init__(self, surface):
            self.surface = surface

        def run_turn(self, text, *, cancellation_token=None, image_paths=None, **_kwargs):
            delivered.append((text, list(image_paths or [])))
            return 0

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        _result, transcript = app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=RecordingSession,
            background_turns=False,
            workspace_root=workspace_root,
        )
    user_echoes = [text for role, text in transcript if role == "user"]
    return delivered, user_echoes, transcript


def test_dropping_an_image_shows_a_chip_and_attaches_the_file(monkeypatch, tmp_path):
    shot = _png(tmp_path / "shot.png")

    delivered, user_echoes, _transcript = _run_headless_attachment_submission(
        monkeypatch,
        _bracketed_paste(str(shot)) + "what is this?\r/exit\r",
    )

    assert user_echoes == ["[Image #1] what is this?"]
    text, image_paths = delivered[0]
    assert text == "[Image #1] what is this?"
    assert image_paths == [os.fspath(shot.resolve())]


def test_dropping_a_document_expands_to_its_path_with_no_image_attached(monkeypatch, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("hi", encoding="utf-8")

    delivered, user_echoes, _transcript = _run_headless_attachment_submission(
        monkeypatch,
        _bracketed_paste(str(notes)) + "summarize\r/exit\r",
    )

    assert user_echoes == ["[File #1] summarize"]
    text, image_paths = delivered[0]
    assert text == f"{os.fspath(notes.resolve())} summarize"
    assert image_paths == []


def test_dropping_two_files_chips_both(monkeypatch, tmp_path):
    shot = _png(tmp_path / "shot.png")
    notes = tmp_path / "notes.txt"
    notes.write_text("hi", encoding="utf-8")

    delivered, user_echoes, _transcript = _run_headless_attachment_submission(
        monkeypatch,
        _bracketed_paste(f'"{shot}" "{notes}"') + "\r/exit\r",
    )

    assert user_echoes == ["[Image #1] [File #1]"]
    _text, image_paths = delivered[0]
    assert image_paths == [os.fspath(shot.resolve())]


def test_backspace_removes_a_whole_chip_and_detaches_it(monkeypatch, tmp_path):
    shot = _png(tmp_path / "shot.png")

    delivered, user_echoes, _transcript = _run_headless_attachment_submission(
        monkeypatch,
        "keep " + _bracketed_paste(str(shot)) + "\x7f\x7fstill here\r/exit\r",
    )

    assert user_echoes == ["keep still here"]
    assert delivered[0] == ("keep still here", [])


def test_prose_containing_a_path_is_pasted_verbatim(monkeypatch, tmp_path):
    shot = _png(tmp_path / "shot.png")

    delivered, _user_echoes, _transcript = _run_headless_attachment_submission(
        monkeypatch,
        _bracketed_paste(f"open {shot} now") + "\r/exit\r",
    )

    assert delivered[0] == (f"open {shot} now", [])


def test_slash_image_with_a_path_attaches_without_starting_a_turn(monkeypatch, tmp_path):
    shot = _png(tmp_path / "shot.png")

    delivered, user_echoes, _transcript = _run_headless_attachment_submission(
        monkeypatch,
        f"/image {shot}\rdescribe it\r/exit\r",
        workspace_root=tmp_path,
    )

    assert user_echoes == ["[Image #1] describe it"]
    assert delivered[0][1] == [os.fspath(shot.resolve())]


def test_slash_image_reports_a_path_it_cannot_attach(monkeypatch, tmp_path):
    delivered, _user_echoes, transcript = _run_headless_attachment_submission(
        monkeypatch,
        f"/image {tmp_path / 'gone.png'}\r/exit\r",
        workspace_root=tmp_path,
    )

    assert delivered == []
    assert any(role == "warn" and "Could not attach" in text for role, text in transcript)


@pytest.mark.parametrize("trigger", ["\x16", "/image\r", "/paste-image\r"])
def test_macos_clipboard_image_reaches_the_turn(trigger, monkeypatch, tmp_path):
    """Exercise OS output -> Pillow -> disk -> TUI chip -> submitted image path."""
    monkeypatch.setattr("alysis_code.clipboard.sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(ImageGrab, "sys", SimpleNamespace(platform="darwin"))
    with Image.new("RGBA", (3, 2), (12, 34, 56, 78)) as source, BytesIO() as buffer:
        source.save(buffer, format="PNG")
        payload = buffer.getvalue()

    def fake_run(args, **kwargs):
        assert args[0] == "osascript"
        output = f"\u00abdata PNGf{payload.hex().upper()}\u00bb\n".encode()
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(ImageGrab, "subprocess", SimpleNamespace(run=fake_run))

    delivered, user_echoes, transcript = _run_headless_attachment_submission(
        monkeypatch,
        trigger + "describe it\rnext turn\r/exit\r",
        workspace_root=tmp_path,
    )

    assert user_echoes == ["[Image #1] describe it", "next turn"]
    assert len(delivered[0][1]) == 1
    assert delivered[1] == ("next turn", [])
    assert not any(role in {"warn", "error"} for role, _text in transcript)
    saved_paths = list((tmp_path / ".alysis_images").glob("*.png"))
    assert delivered[0][1] == [os.fspath(saved_paths[0].resolve())]
    with Image.open(saved_paths[0]) as saved:
        assert saved.size == (3, 2)
        assert saved.getpixel((0, 0)) == (12, 34, 56, 78)


def test_macos_empty_clipboard_leaves_composer_usable(monkeypatch, tmp_path):
    monkeypatch.setattr("alysis_code.clipboard.sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: None)

    delivered, _user_echoes, transcript = _run_headless_attachment_submission(
        monkeypatch,
        "\x16still usable\r/exit\r",
        workspace_root=tmp_path,
    )

    assert delivered == [("still usable", [])]
    assert any(role == "warn" and "macOS clipboard" in text for role, text in transcript)
    assert not (tmp_path / ".alysis_images").exists()


def test_a_drop_typed_as_plain_keys_still_chips(monkeypatch, tmp_path):
    """Terminals that do not bracket a drop type the quoted path in instead."""
    shot = _png(tmp_path / "shot.png")

    delivered, user_echoes, _transcript = _run_headless_attachment_submission(
        monkeypatch,
        f'"{shot}"' + "what is this?\r/exit\r",
    )

    assert user_echoes == ["[Image #1] what is this?"]
    assert delivered[0][1] == [os.fspath(shot.resolve())]


def test_a_quoted_word_typed_by_hand_stays_text(monkeypatch, tmp_path):
    delivered, user_echoes, _transcript = _run_headless_attachment_submission(
        monkeypatch,
        'he said "hello" today\r/exit\r',
    )

    assert user_echoes == ['he said "hello" today']
    assert delivered[0] == ('he said "hello" today', [])
