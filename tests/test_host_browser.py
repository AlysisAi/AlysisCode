from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code import host_browser


def test_native_browser_uses_python_webbrowser(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(host_browser, "is_wsl", lambda: False)
    monkeypatch.setattr(
        host_browser.webbrowser,
        "open",
        lambda url: opened.append(url) or True,
    )

    assert host_browser.open_url("https://example.test/login") is True
    assert opened == ["https://example.test/login"]


def test_wsl_browser_prefers_wslview(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(host_browser, "is_wsl", lambda: True)
    monkeypatch.setattr(
        host_browser,
        "_wsl_browser_commands",
        lambda url: (("/usr/bin/wslview", url), ("powershell.exe", url)),
    )
    monkeypatch.setattr(
        host_browser.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(tuple(command)) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        host_browser.webbrowser,
        "open",
        lambda _url: (_ for _ in ()).throw(AssertionError("unexpected fallback")),
    )

    assert host_browser.open_url("https://example.test/login?a=1&b=2") is True
    assert commands == [("/usr/bin/wslview", "https://example.test/login?a=1&b=2")]


def test_wsl_browser_falls_through_launchers_then_python(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []
    opened: list[str] = []
    monkeypatch.setattr(host_browser, "is_wsl", lambda: True)
    monkeypatch.setattr(
        host_browser,
        "_wsl_browser_commands",
        lambda url: (("wslview", url), ("powershell.exe", url)),
    )
    monkeypatch.setattr(
        host_browser.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(tuple(command)) or SimpleNamespace(returncode=1),
    )
    monkeypatch.setattr(
        host_browser.webbrowser,
        "open",
        lambda url: opened.append(url) or True,
    )

    assert host_browser.open_url("https://example.test/login") is True
    assert commands == [
        ("wslview", "https://example.test/login"),
        ("powershell.exe", "https://example.test/login"),
    ]
    assert opened == ["https://example.test/login"]


def test_wsl_browser_commands_keep_url_as_one_argument(monkeypatch) -> None:
    monkeypatch.setattr(host_browser.shutil, "which", lambda name: f"/bin/{name}")

    url = "https://example.test/login?state=a&next=https%3A%2F%2Fexample.test"
    commands = host_browser._wsl_browser_commands(url)

    assert commands[0] == ("/bin/wslview", url)
    assert commands[1][-1] == url
    assert commands[2] == ("/bin/rundll32.exe", "url.dll,FileProtocolHandler", url)


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("frozen", [False, True])
def test_quiet_browser_contains_real_launcher_output(
    tmp_path, monkeypatch, capfd, exit_code, frozen
):
    launched = tmp_path / "received-url.txt"
    launcher = tmp_path / "noisy-browser.py"
    launcher.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(launched)!r}).write_text(sys.argv[1], encoding='utf-8')\n"
        "print('gio: noisy launcher stdout', flush=True)\n"
        "print('gio: noisy launcher stderr', file=sys.stderr, flush=True)\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    # Keep every fallback inside a fake browser module: Python's real browser
    # registry can otherwise try the user's browser after the fixture exits 1.
    (tmp_path / "webbrowser.py").write_text(
        "import subprocess, sys\n"
        "def open(url, new=0):\n"
        f"    return subprocess.call([sys.executable, {str(launcher)!r}, url]) == 0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(host_browser, "is_wsl", lambda: False)
    monkeypatch.setattr(host_browser.sys, "frozen", frozen, raising=False)
    if frozen:
        entry = Path(__file__).resolve().parents[1] / "scripts/release/managed_cli_entry.py"
        run = host_browser.subprocess.run

        def run_managed_entry(command, **kwargs):
            # Execute the actual packaged entry point with the arguments it
            # receives in a frozen build. -S also proves the helper needs no CLI
            # dependencies or startup; the fake browser is the only extra module.
            assert command[0] == host_browser.sys.executable
            assert "-c" not in command
            return run([command[0], "-S", str(entry), *command[1:]], **kwargs)

        monkeypatch.setattr(host_browser.subprocess, "run", run_managed_entry)
    url = "https://example.test/draft?title=feedback&body=one%20two%23three"

    assert host_browser.open_url(url, quiet=True) is (exit_code == 0)
    assert launched.read_text(encoding="utf-8") == url
    assert capfd.readouterr() == ("", "")


def test_quiet_wsl_fallback_never_uses_in_process_webbrowser(monkeypatch):
    monkeypatch.setattr(host_browser, "is_wsl", lambda: True)
    monkeypatch.setattr(host_browser, "_wsl_browser_commands", lambda url: ())
    monkeypatch.setattr(
        host_browser.webbrowser, "open", lambda *a, **kw: pytest.fail("unsafe terminal fallback")
    )

    def timeout(command, **kwargs):
        for stream in ("stdin", "stdout", "stderr"):
            assert kwargs[stream] == host_browser.subprocess.DEVNULL
        raise host_browser.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(host_browser.subprocess, "run", timeout)
    assert host_browser.open_url("https://example.test/draft", quiet=True) is False
