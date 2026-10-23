#!/usr/bin/env bash
set -euo pipefail
uv_executable="$(command -v uv)"
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --env UV_CACHE_DIR=/tmp/uv-cache \
  --env UV_PROJECT_ENVIRONMENT=/tmp/alysis-venv \
  --env UV_PYTHON=/tmp/alysis-python/python/bin/python3 \
  --env UV_PYTHON_DOWNLOADS=never \
  --env OUTPUT_NAME="$OUTPUT_NAME" \
  --volume "$PWD:/workspace" \
  --volume "$uv_executable:/usr/local/bin/uv:ro" \
  --workdir /workspace \
  "$MANYLINUX_IMAGE" \
  bash -euxo pipefail -c '
    # Bootstrap with the image interpreter, then package only the patched,
    # hash-pinned distribution that includes the required shared library.
    /opt/python/cp311-cp311/bin/python scripts/release/install_managed_cli_python.py \
      --target "${OUTPUT_NAME#alysis-}" --destination /tmp/alysis-python \
      --receipt "managed-cli-out/${OUTPUT_NAME#alysis-}.python-runtime.json"
    uv sync --frozen --no-editable --extra managed-build
    uv run --frozen --no-sync python -m PyInstaller \
      --noconfirm \
      --clean \
      --onefile \
      --name alysis \
      --collect-all alysis_code \
      scripts/release/managed_cli_entry.py
    mkdir -p managed-cli-out
    mv dist/alysis "managed-cli-out/${OUTPUT_NAME}"
  '
