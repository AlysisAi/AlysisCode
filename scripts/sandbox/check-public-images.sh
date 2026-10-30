#!/usr/bin/env bash
# Verify the same unauthenticated access required by a fresh Alysis installation.
set -euo pipefail

pull=false
if [[ "${1:-}" == "--pull" ]]; then
  pull=true
  shift
fi
if (( $# == 0 )); then
  echo "Usage: bash check-public-images.sh [--pull] IMAGE[:TAG|@DIGEST] ..." >&2
  exit 2
fi

# Do not log out the caller or reuse their registry credentials.
# Docker also accepts credentials through the environment.
unset DOCKER_AUTH_CONFIG
public_config="$(mktemp -d)"
trap 'rm -rf -- "${public_config}"' EXIT

# Explicit empty auth entries suppress Docker's automatic native credential-store
# detection. An absent config (or an empty auths map) can still use the OS keychain.
# Resolve registry names using Docker's reference rules; unqualified names use Hub.
auth_config='{"auths":{}}'
for image in "$@"; do
  registry="${image%%/*}"
  if [[ "${image}" != */* ]] || [[ "${registry}" != *.* && "${registry}" != *:* && "${registry}" != localhost ]]; then
    registry=docker.io
  fi
  if [[ "${registry}" == docker.io || "${registry}" == index.docker.io ]]; then
    registry=https://index.docker.io/v1/
  fi
  auth_config="$(jq --arg registry "${registry}" '.auths[$registry] = {}' <<< "${auth_config}")"
done
printf '%s\n' "${auth_config}" > "${public_config}/config.json"

for image in "$@"; do
  if ! docker --config "${public_config}" manifest inspect "${image}" > "${public_config}/manifest.json"; then
    echo "Cannot download ${image} anonymously. Check that the package and reference exist, the package visibility is public, and the registry is reachable." >&2
    exit 1
  fi
  if ! jq -e '
    [.manifests[]?.platform | select(.os == "linux") | .architecture] as $architectures
    | ($architectures | index("amd64")) != null
      and ($architectures | index("arm64")) != null
  ' "${public_config}/manifest.json" > /dev/null; then
    echo "${image} must contain both linux/amd64 and linux/arm64 images." >&2
    exit 1
  fi
  if [[ "${pull}" == "true" ]]; then
    docker --config "${public_config}" pull --platform linux/amd64 "${image}"
    docker --config "${public_config}" pull --platform linux/arm64 "${image}"
  fi
  echo "Public image verified: ${image} (linux/amd64, linux/arm64)"
done
