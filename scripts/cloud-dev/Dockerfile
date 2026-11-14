# syntax=docker/dockerfile:1.7
# Development desktop, separate from the production Wolfi coding sandbox.
FROM python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/alysis/.venv/bin:$PATH

# Debian supplies native Chromium on both arm64 and amd64 (no Snap or emulation).
RUN apt-get update && apt-get install -y --no-install-recommends \
    bubblewrap ca-certificates chromium chromium-sandbox dbus-x11 libnss3-tools \
    fonts-dejavu-core fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji \
    git libreoffice-calc libreoffice-impress libreoffice-writer novnc openbox procps ripgrep \
    websockify x11-utils x11vnc xclip xdotool xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 alysis \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash alysis \
    && install -d -o 1000 -g 1000 /var/lib/alysis /var/lib/alysis/workspace

COPY --from=ghcr.io/astral-sh/uv@sha256:2381d6aa60c326b71fd40023f921a0a3b8f91b14d5db6b90402e65a635053709 /uv /usr/local/bin/uv
WORKDIR /opt/alysis
COPY pyproject.toml uv.lock README.md hatch_build.py ./
COPY src ./src
COPY schemas/alysis-host-v2 ./schemas/alysis-host-v2
RUN uv sync --locked --no-dev --no-editable --extra server --extra documents && uv cache clean
COPY scripts/cloud-dev/entrypoint.sh scripts/cloud-dev/check.py /opt/cloud-dev/

USER 1000:1000
WORKDIR /var/lib/alysis/workspace
CMD ["bash", "/opt/cloud-dev/entrypoint.sh"]
