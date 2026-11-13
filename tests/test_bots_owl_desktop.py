"""The owl computers' bar (AL, 2026-11-07; like Grok Bot's, 2026-11-13): launchers for installed
apps, the open windows and the clock, quoted safely, on the desktop and on the browser screen."""

from pathlib import PurePosixPath

from alysis_code.bots.browser_linux import write_browser_bar
from alysis_code.bots.desktop_worker import DOCK_APPS, write_dock


def settings_of(text):
    return dict(line.split(" = ", 1) for line in text.splitlines() if " = " in line)


def test_dock_lists_installed_apps_in_order(tmp_path):
    settings = write_dock(tmp_path, available=lambda path: True)
    text = settings.read_text()
    values = settings_of(text)
    assert values["panel_position"] == "bottom center horizontal"
    assert values["panel_items"] == "LTC"  # launchers, open windows, clock
    assert values["panel_size"] == "100% 40"
    assert values["strut_policy"] == "follow_size"  # maximised windows end above the bar
    launchers = [
        line.split(" = ")[1] for line in text.splitlines() if line.startswith("launcher_item_app")
    ]
    assert [p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in launchers] == [
        "files.desktop",
        "libreoffice.desktop",
        "terminal.desktop",
    ]
    files = (tmp_path / "files.desktop").read_text()
    assert "Exec=/usr/bin/pcmanfm /workspace" in files and "Name=Files" in files
    terminal = (tmp_path / "terminal.desktop").read_text()
    assert 'Exec=/usr/bin/xterm -fa "DejaVu Sans Mono" -fs 11' in terminal


def test_the_bar_never_hides_or_closes_a_window_and_shows_a_clock_in_digits(tmp_path):
    values = settings_of(write_dock(tmp_path, available=lambda path: True).read_text())
    # Closing the browser's last window would end the owl's browser.
    assert values["mouse_left"] == "activate"
    assert values["mouse_middle"] == values["mouse_right"] == "none"
    # Digits and the zone name only: no month or day names in any one language, and no claim
    # that the computer's time is the person's.
    clock = values["time1_format"]
    assert "%a" not in clock and "%b" not in clock and "%Z" in clock


def test_missing_apps_get_no_launcher(tmp_path):
    settings = write_dock(tmp_path, available=lambda path: path.endswith("xterm"))
    assert settings.read_text().count("launcher_item_app") == 1
    assert not (tmp_path / "files.desktop").exists()
    assert len(DOCK_APPS) == 3


def test_browser_bar_button_asks_the_controller_and_starts_no_browser(tmp_path):
    # The computer's own private folder (Linux), wherever the test runs.
    request = PurePosixPath("/tmp/alysis-bot-computer-x/bar/open-browser")
    settings = write_browser_bar(tmp_path / "bar", request, available=lambda path: True)
    text = settings.read_text()
    assert settings_of(text)["panel_items"] == "LTC"
    launchers = [line for line in text.splitlines() if line.startswith("launcher_item_app")]
    assert len(launchers) == 1
    launcher = (tmp_path / "bar" / "browser.desktop").read_text()
    assert "Name=Browser" in launcher and "Icon=chromium" in launcher
    # Only a request file: a browser started from the screen would skip the network guard.
    exec_line = next(line for line in launcher.splitlines() if line.startswith("Exec="))
    assert exec_line.startswith("Exec=/usr/bin/touch ") and str(request) in exec_line
    assert "chrom" not in exec_line.lower()
    fallback = write_browser_bar(tmp_path / "other", request, available=lambda path: False)
    assert "Icon=web-browser" in (fallback.parent / "browser.desktop").read_text()
