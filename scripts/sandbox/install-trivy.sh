#!/usr/bin/env bash
# Install the same reviewed scanner once per job; transient release downloads
# must not bypass either archive verification or the blocking vulnerability scan.
set -euo pipefail

case "$(uname -m)" in
  x86_64)
    archive=trivy_0.74.0_Linux-64bit.tar.gz
    asset_id=514339431
    digest=2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a
    ;;
  aarch64|arm64)
    archive=trivy_0.74.0_Linux-ARM64.tar.gz
    asset_id=514339429
    digest=b94ce1976bbf3c15b514b605ee88be7c6d94a29be2302847ff01cb794d47aad5
    ;;
  *) echo "Unsupported Trivy runner architecture" >&2; exit 1 ;;
esac

install_dir="$(mktemp -d "${RUNNER_TEMP:?}/alysis-trivy.XXXXXX")"
if ! curl --fail --location --retry 3 --retry-all-errors --connect-timeout 30 \
  --max-time 180 --output "${install_dir}/${archive}" \
  "https://github.com/aquasecurity/trivy/releases/download/v0.74.0/${archive}"; then
  curl --fail --location --retry 3 --retry-all-errors --connect-timeout 30 \
    --max-time 180 --header 'Accept: application/octet-stream' \
    --output "${install_dir}/${archive}" \
    "https://api.github.com/repos/aquasecurity/trivy/releases/assets/${asset_id}"
fi
printf '%s  %s\n' "${digest}" "${install_dir}/${archive}" | sha256sum --check --strict
tar -xzf "${install_dir}/${archive}" -C "${install_dir}" trivy
"${install_dir}/trivy" --version
printf '%s\n' "${install_dir}" >> "${GITHUB_PATH:?}"
