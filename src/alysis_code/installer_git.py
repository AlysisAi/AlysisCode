"""Credential/config isolation shared by plugin and skill Git installers."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .command_text import command_text


def run_installer_git(args: list[str], *, cwd: Path | None) -> subprocess.CompletedProcess[str]:
    # Installer source fetching must not consult the person's Git credentials,
    # config, templates or hooks. Retain only executable/platform necessities
    # and explicit CA/proxy settings; never inherit TLS-verification bypasses.
    inherited_keys = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
    env = {key: os.environ[key] for key in inherited_keys if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="alysis-installer-git-") as isolated_home:
        env.update(
            HOME=isolated_home,
            USERPROFILE=isolated_home,
            XDG_CONFIG_HOME=isolated_home,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_TEMPLATE_DIR=isolated_home,
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="https",
            GIT_CONFIG_COUNT="3",
            GIT_CONFIG_KEY_0="credential.helper",
            GIT_CONFIG_VALUE_0="",
            GIT_CONFIG_KEY_1="core.hooksPath",
            GIT_CONFIG_VALUE_1=isolated_home,
            GIT_CONFIG_KEY_2="http.sslVerify",
            GIT_CONFIG_VALUE_2="true",
        )
        return subprocess.run(
            args,
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=env,
            stdin=subprocess.DEVNULL,
            shell=False,
            check=True,
            timeout=120,
            capture_output=True,
            **command_text(),
        )
