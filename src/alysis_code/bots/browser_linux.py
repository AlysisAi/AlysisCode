"""Private X11/Chromium executor for the opt-in local Linux browser lab.

Run only inside the qualified disposable Linux sandbox, never on the API host.
The container/VM is the isolation boundary; private X displays prevent accidental
cross-bot UI targeting but are not security boundaries between hostile processes.
"""

from __future__ import annotations

import os
import select
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..ide.browser_profiles import BrowserProfileStore
from ..ide.cdp_websocket_transport import WebSocketCdpTransportFactory
from ..ide.managed_browser import BrowserCancelledError, ManagedBrowserConfig, ManagedBrowserService
from .browser import BrowserStartupCleanupError
from .computer import LinuxScreenCapture
from .desktop_worker import TINT2_SETTINGS, WALLPAPER, desktop_exec
from .files import MAX_FILE_BYTES
from .human_input import HumanInput

# Fits the computer link (4 MB per reply) with room for JSON framing.
OWL_PAGE_SNAPSHOT_BYTES = 2 * 1024 * 1024

# The browser screen looks like the owl's desktop (AL, 2026-11-13: "like Grok Bot's"): wallpaper
# and a bar with a Browser button, the open windows and the clock. The button asks this
# controller to open a window of the owl's own managed browser; nothing on the screen can start
# a browser of its own, which would skip the browser's network guard.
CHROMIUM_ICON = "/usr/share/icons/hicolor/48x48/apps/chromium.png"


def write_browser_bar(directory: Path, request: Path, available=os.path.exists) -> Path:
    """Write the bar's Browser launcher and tint2 settings; return the settings file."""
    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / "browser.desktop"
    launcher.write_text(
        "[Desktop Entry]\nType=Application\nName=Browser\n"
        f"Icon={'chromium' if available(CHROMIUM_ICON) else 'web-browser'}\n"
        f"Exec={desktop_exec(['/usr/bin/touch', str(request)])}\nTerminal=false\n"
    )
    settings = directory / "tint2rc"
    settings.write_text(TINT2_SETTINGS + f"launcher_item_app = {launcher}\n")
    return settings


class LinuxBrowserComputer:
    def __init__(
        self,
        bot_id: str,
        cancel,
        *,
        preview_urls=lambda: (),
        profile_store: BrowserProfileStore | None = None,
        account_id: str | None = None,
        system_trust: bool = False,
    ):
        self.owner = bot_id
        self.preview_urls = preview_urls
        self._directory = tempfile.TemporaryDirectory(prefix="alysis-bot-computer-")
        self._processes = []
        self.browser = None
        self.session_id = None
        self._closed = False
        self._cleaned = False
        self._profile_lease = None
        # The bar and its watcher are cosmetic: never part of alive() and never fatal.
        self._decorations = []
        self._stop_watching = threading.Event()
        self.lifetime = "Temporary browser. Idle shutdown clears tabs and browser storage."
        root = Path(self._directory.name)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(root)}
        read_fd, write_fd = os.pipe()
        try:
            from .browser_trust import PrivateBrowserLauncher, prepare_system_trust

            if system_trust:
                prepare_system_trust(root)
            if profile_store is not None:
                if not account_id:
                    raise ValueError("Persistent browser storage requires its owning account.")
                self._profile_lease = profile_store.acquire(account_id, bot_id)
                self.lifetime = (
                    "Browser storage survives verified clean shutdown. Reopening starts a blank "
                    "page; tabs and unsaved page state are not restored. Unclean shutdown blocks "
                    "reuse until its process group is proved stopped. Recovered browser data may be incomplete."
                    + (
                        " This browser recovered after an unclean shutdown; verify the website state and never replay a prior submission."
                        if self._profile_lease.recovered
                        else ""
                    )
                )
            # Xvfb chooses an unused display atomically; no global DISPLAY lookup.
            xserver = subprocess.Popen(
                [
                    "/usr/bin/Xvfb",
                    "-displayfd",
                    str(write_fd),
                    "-screen",
                    "0",
                    "1280x800x24",
                    "-nolisten",
                    "tcp",
                ],
                pass_fds=(write_fd,),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._processes.append(xserver)
            os.close(write_fd)
            write_fd = -1
            deadline = time.monotonic() + 10
            data = b""
            while b"\n" not in data:
                if cancel():
                    raise BrowserCancelledError("Computer startup cancelled.")
                if time.monotonic() >= deadline or xserver.poll() is not None:
                    raise ValueError("Private Linux display could not start.")
                if select.select([read_fd], [], [], 0.05)[0]:
                    part = os.read(read_fd, 32)
                    if not part:
                        raise ValueError("Private Linux display closed during startup.")
                    data += part
            number = data.strip().decode("ascii")
            if not number.isdigit() or len(number) > 5:
                raise ValueError("Invalid private Linux display.")
            self.display = ":" + number
            self._processes.append(
                subprocess.Popen(
                    ["/usr/bin/openbox", "--sm-disable"],
                    env={**env, "DISPLAY": self.display},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            self._open_request = root / "bar" / "open-browser"
            # Before the browser starts, so its maximised window ends above the bar.
            self._decorate({**env, "DISPLAY": self.display}, root / "bar")
            self.browser = ManagedBrowserService(
                launcher=PrivateBrowserLauncher(root),
                config=ManagedBrowserConfig(
                    data_root=root / "browser",
                    linux_display=self.display,
                    max_sessions_total=1,
                    max_sessions_per_owner=1,
                    # Real pages measured 0.4-1.4 MB (search results, articles, news).
                    # Below this, a page is text-only and the owl cannot act on it.
                    max_snapshot_bytes=OWL_PAGE_SNAPSHOT_BYTES,
                ),
                transport_factory=WebSocketCdpTransportFactory(max_upload_bytes=MAX_FILE_BYTES),
            )
            session = self.browser.start(
                bot_id,
                allowed_preview_urls_provider=preview_urls,
                profile_lease=self._profile_lease,
                cancel=cancel,
            )
            self.session_id = session.session_id
            self.capture = LinuxScreenCapture(self.display)
            self.human_input = HumanInput(self.display)
            threading.Thread(
                target=self._watch_open_requests, name="alysis-browser-bar", daemon=True
            ).start()
        except BaseException:
            try:
                self.close()
            except Exception:
                # Keep a failed-start process tree owned and retryable rather
                # than losing it when the constructor cannot return normally.
                raise BrowserStartupCleanupError(self) from None
            raise
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def _decorate(self, env, directory: Path) -> None:
        quiet = {
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        try:
            if os.path.exists("/usr/bin/hsetroot"):
                wallpaper = ["/usr/bin/hsetroot", "-add", WALLPAPER[0], "-add", WALLPAPER[1]]
                subprocess.run([*wallpaper, "-gradient", "160"], timeout=5, **quiet)
            elif os.path.exists("/usr/bin/xsetroot"):
                subprocess.run(["/usr/bin/xsetroot", "-solid", "#d6d9de"], timeout=5, **quiet)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not os.path.exists("/usr/bin/tint2"):
            return
        # A panel mapped while Openbox is still starting can stay unmapped (desktop_worker.py).
        deadline = time.monotonic() + 3
        while os.path.exists("/usr/bin/xdotool") and time.monotonic() < deadline:
            try:
                ready = subprocess.run(
                    ["/usr/bin/xdotool", "get_desktop"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if ready.returncode == 0 and ready.stdout.strip().isdigit():
                break
            time.sleep(0.05)
        try:
            settings = write_browser_bar(directory, self._open_request)
            self._decorations.append(
                subprocess.Popen(["/usr/bin/tint2", "-c", str(settings)], **quiet)
            )
        except OSError:
            pass

    def _watch_open_requests(self) -> None:
        """Open a browser window each time the bar's Browser button is pressed."""
        while not self._stop_watching.wait(0.25):
            try:
                self._open_request.unlink()
            except OSError:
                continue
            try:
                self.browser.open_window(self.owner, self.session_id)
            except Exception:
                # A stopped browser makes the computer not alive; it is replaced as usual.
                pass

    def alive(self) -> bool:
        return (
            not self._closed
            and all(p.poll() is None for p in self._processes)
            and self.browser.status(self.owner, self.session_id).state == "running"
        )

    def close(self) -> None:
        if self._cleaned:
            return
        self._closed = True
        failed = False
        stop = getattr(self, "_stop_watching", None)
        if stop is not None:
            stop.set()
        if hasattr(self, "human_input"):
            try:
                self.human_input.close()
            except Exception:
                failed = True
        try:
            if self.browser:
                self.browser.close_all()
        except Exception:
            failed = True
        finally:
            for process in [*getattr(self, "_decorations", ()), *reversed(self._processes)]:
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                except Exception:
                    failed = True
            if not failed:
                if getattr(self, "_profile_lease", None) is not None:
                    self._profile_lease.release()
                self._directory.cleanup()
                self._cleaned = True
        if failed:
            raise RuntimeError("Private computer cleanup did not finish.")
