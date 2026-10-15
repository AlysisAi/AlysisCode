from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.cli_impl.tui.transcript_navigation import TranscriptMatch, TranscriptNavigator


def test_unicode_multiline_offsets_and_wrap() -> None:
    navigator = TranscriptNavigator()
    text = "Straße\nκόσμε Straße\nκόσμε"
    assert navigator.find([("assistant", text)], "STRASSE\nΚΌΣΜΕ") == TranscriptMatch(0, 0, 12)
    assert navigator.next() == TranscriptMatch(0, 13, 25)
    assert navigator.next() == TranscriptMatch(0, 0, 12)
    assert navigator.previous() == TranscriptMatch(0, 13, 25)
    assert navigator.find([("assistant", "ß")], "s") == TranscriptMatch(0, 0, 1)
    assert len(navigator.matches) == 1


def test_no_match_empty_and_new_snapshot() -> None:
    navigator = TranscriptNavigator()
    navigator.find([("user", "repeat repeat"), ("assistant", "repeat")], "repeat")
    assert len(navigator.matches) == 3
    assert navigator.find([], "repeat") is None
    assert navigator.next() is None
    assert navigator.previous() is None
    assert navigator.find([("user", "text")], "") is None


def test_copy_source_markdown_without_ui_controls() -> None:
    transcript = TuiTranscript()
    assert transcript.last_response_text() == ""
    transcript.load_history(
        [
            {"role": "assistant", "content": "old reply"},
            {
                "role": "assistant",
                "content": "\x1b[31m# Heading\x1b[0m\n```py\nprint('α')\n```\n\x1b]8;;https://example.com\x07link\x1b]8;;\x07\x00",
            },
            {"role": "user", "content": "new prompt"},
        ]
    )
    assert transcript.last_response_text() == "# Heading\n```py\nprint('α')\n```\nlink"
