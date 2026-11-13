"""Private Chromium home with the qualified Linux image's system CA trust.

NSS does not automatically use Debian's OpenSSL CA bundle. Import its public
CA certificates into a new per-browser database; never disable TLS validation
or accept a certificate supplied by a page/model. No keys or host profile copy.
"""

from __future__ import annotations

import hashlib
import os
import ssl
import stat
import subprocess
from dataclasses import replace

from ..ide.managed_browser import DefaultBrowserProcessLauncher


def prepare_system_trust(home):
    bundle = "/etc/ssl/certs/ca-certificates.crt"
    fd = os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
            or info.st_size > 2 * 1024 * 1024
        ):
            raise ValueError("System CA bundle is not a bounded administrator-owned file.")
        with os.fdopen(os.dup(fd), "rb") as stream:
            pem = stream.read(2 * 1024 * 1024 + 1).decode("ascii")
        if len(pem) > 2 * 1024 * 1024:
            raise ValueError("System CA bundle exceeds its allowance.")
    finally:
        os.close(fd)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=pem)
    certificates = context.get_ca_certs(binary_form=True)
    if not 1 <= len(certificates) <= 1024:
        raise ValueError("System CA collection is unavailable or too large.")
    database = home / ".pki" / "nssdb"
    database.mkdir(mode=0o700, parents=True)

    def run(args, payload=None):
        result = subprocess.run(
            ["/usr/bin/certutil", *args, "-d", "sql:" + str(database)],
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Private browser system trust initialization failed.")

    run(["-N", "--empty-password"])
    for certificate in certificates:
        run(
            ["-A", "-t", "C,,", "-n", "system-" + hashlib.sha256(certificate).hexdigest()],
            certificate,
        )


class PrivateBrowserLauncher(DefaultBrowserProcessLauncher):
    def __init__(self, home):
        self.home = home

    def launch(self, spec):
        return super().launch(
            replace(spec, environment={**spec.environment, "HOME": str(self.home)})
        )
