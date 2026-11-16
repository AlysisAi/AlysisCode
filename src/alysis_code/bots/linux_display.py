"""One owned X display for a disposable Linux computer, never a host display."""

from __future__ import annotations

import os
import select
import subprocess
import time


class PrivateLinuxDisplay:
    def __init__(self, cancel=lambda: False):
        self.processes = []
        read_fd, write_fd = os.pipe()
        env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}
        try:
            server = subprocess.Popen(
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
            self.processes.append(server)
            os.close(write_fd)
            write_fd = -1
            deadline, data = time.monotonic() + 10, b""
            while b"\n" not in data:
                if cancel() or time.monotonic() >= deadline or server.poll() is not None:
                    raise RuntimeError("Private display startup did not finish.")
                if select.select([read_fd], [], [], 0.05)[0]:
                    part = os.read(read_fd, 32)
                    if not part or len(data) + len(part) > 32:
                        raise RuntimeError("Private display startup was not confirmed.")
                    data += part
            number = data.strip().decode("ascii")
            if not number.isdigit() or len(number) > 5:
                raise RuntimeError("Invalid private display identity.")
            self.name = ":" + number
        except BaseException:
            self.close()
            raise
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def alive(self):
        return bool(self.processes) and all(p.poll() is None for p in self.processes)

    def close(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
