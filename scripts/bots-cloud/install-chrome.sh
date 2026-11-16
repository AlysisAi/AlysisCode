#!/bin/sh
# Google Chrome is required for the amd64 Cloud worker; never silently substitute Chromium.
set -eu
test "$(dpkg --print-architecture)" = amd64
python3 - <<'PY'
from pathlib import Path
from urllib.request import urlopen
with urlopen('https://dl.google.com/linux/linux_signing_key.pub', timeout=30) as response:
    key = response.read(100000)
if not key.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----'):
    raise RuntimeError('Google package signing key was not confirmed')
Path('/usr/share/keyrings/google-chrome.asc').write_bytes(key)
Path('/etc/apt/sources.list.d/google-chrome.list').write_text(
    'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.asc] '
    'https://dl.google.com/linux/chrome/deb/ stable main\n')
PY
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable
/usr/bin/google-chrome-stable --version
