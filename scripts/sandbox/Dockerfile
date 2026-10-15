# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Qualified frozen inputs; production candidates still require public verification and promotion.
# The complete APK graph is architecture- and hash-pinned, authenticated by a signed index,
# retained outside the runtime image, and installed without network access.
ARG WOLFI_IMAGE=cgr.dev/chainguard/wolfi-base@sha256:b71f01d7fbb063a921577f355a82ea665e98c96e73c89a6ea0bcb773875b129c
ARG VARIANT=dev
ARG SOURCE_URL=https://github.com/AlysisAi/AlysisCode
ARG GIT_SHA=dev
ARG VERSION=dev
ARG SOURCE_DATE_EPOCH=0

FROM ${WOLFI_IMAGE} AS apk-inputs
SHELL ["/bin/sh", "-euxc"]
ARG TARGETARCH
COPY scripts/sandbox/apk-locks/${TARGETARCH}/ /opt/apk-locks/${TARGETARCH}/
RUN case "${TARGETARCH}" in amd64|arm64) ;; *) exit 1 ;; esac; \
    mkdir -p /opt/apk-inputs; \
    cp /opt/apk-locks/${TARGETARCH}/* /opt/apk-inputs/; \
    cp /opt/apk-inputs/packages.lock /etc/apk/world; \
    apk update; \
    repository_dir="/opt/apk-inputs/repository/$(apk --print-arch)"; \
    mkdir -p "$repository_dir"; \
    apk fetch --world --output "$repository_dir"; \
    cd "$repository_dir"; \
    sha256sum -c /opt/apk-inputs/SHA256SUMS; \
    set -- /var/cache/apk/APKINDEX.*.tar.gz; \
    test "$#" = 1; \
    cp "$1" APKINDEX.tar.gz; \
    apk verify APKINDEX.tar.gz; \
    sha256sum APKINDEX.tar.gz > /opt/apk-inputs/index.sha256; \
    printf '/opt/apk-inputs/repository\n' > /opt/apk-inputs/offline-repositories; \
    cp -R /etc/apk/keys /opt/apk-inputs/keys; \
    cp /etc/apk/repositories /opt/apk-inputs/repositories

FROM ${WOLFI_IMAGE} AS base
SHELL ["/bin/sh", "-euxc"]
ARG SOURCE_URL
ARG GIT_SHA
ARG VERSION
ARG SOURCE_DATE_EPOCH
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}
LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.title="Alysis Code Sandbox" \
      org.opencontainers.image.description="Alysis Code shell sandbox runtime image with frozen inputs." \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN --network=none --mount=type=bind,from=apk-inputs,source=/opt/apk-inputs,target=/opt/apk-inputs,ro \
    cd "/opt/apk-inputs/repository/$(apk --print-arch)"; \
    sha256sum -c /opt/apk-inputs/SHA256SUMS; \
    apk verify APKINDEX.tar.gz; \
    xargs apk add --no-network --no-cache \
        --repositories-file /opt/apk-inputs/offline-repositories < /opt/apk-inputs/packages.lock; \
    cd /; \
    awk '/^P:/ {name=substr($0,3)} /^V:/ {print name "=" substr($0,3)}' /lib/apk/db/installed \
        | LC_ALL=C sort > /tmp/installed-apks.lock; \
    LC_ALL=C sort /opt/apk-inputs/packages.lock > /tmp/expected-apks.lock; \
    diff -u /tmp/expected-apks.lock /tmp/installed-apks.lock; \
    rm /tmp/expected-apks.lock /tmp/installed-apks.lock; \
    mkdir -p /usr/local/bin; \
    ln -s /usr/bin/python3.12 /usr/local/bin/python; \
    ln -s /usr/bin/python3.12 /usr/local/bin/python3; \
    python3 -m venv /tmp/alysis-venv-smoke; \
    /tmp/alysis-venv-smoke/bin/python -m pip --version; \
    rm -rf /tmp/alysis-venv-smoke
RUN groupadd --gid 1000 alysis; \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash alysis; \
    install -d -m 0755 -o 1000 -g 1000 /workspace
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD git --version && rg --version
CMD ["sh", "-lc", "echo alysis-sandbox ready"]
WORKDIR /workspace
USER alysis

FROM base AS rust-inputs
USER root
ARG TARGETARCH
COPY scripts/sandbox/lock_rust_inputs.py scripts/sandbox/rust-inputs.lock.json /opt/rust-input-source/
RUN python /opt/rust-input-source/lock_rust_inputs.py \
    --lock /opt/rust-input-source/rust-inputs.lock.json --arch "${TARGETARCH}" --output /opt/rust-inputs

FROM base AS dev-tools
SHELL ["/bin/sh", "-euxc"]

USER root

ARG TARGETARCH
# Each downloaded tool is version- and digest-pinned for both supported platforms.
ARG NODE_VERSION=24.21.0
ARG NODE_LINUX_AMD64_SHA256=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6
ARG NODE_LINUX_ARM64_SHA256=6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2
ARG GO_VERSION=1.26.8
ARG GO_LINUX_AMD64_SHA256=d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b
ARG GO_LINUX_ARM64_SHA256=211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0
ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_LINUX_AMD64_SHA256=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUSTUP_LINUX_ARM64_SHA256=e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c
ARG RUST_VERSION=1.91.1

ENV RUSTUP_HOME=/home/alysis/.rustup \
    CARGO_HOME=/home/alysis/.cargo \
    PATH=/home/alysis/.cargo/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# pkgconf is already in the complete locked base graph through build-base.

RUN case "${TARGETARCH:-amd64}" in \
        amd64) node_arch=x64; node_sha="${NODE_LINUX_AMD64_SHA256}" ;; \
        arm64) node_arch=arm64; node_sha="${NODE_LINUX_ARM64_SHA256}" ;; \
        *) echo "Unsupported Node TARGETARCH=${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    node_archive="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    curl --proto '=https' --tlsv1.2 -fsSLo /tmp/node.tar.xz \
        "https://nodejs.org/dist/v${NODE_VERSION}/${node_archive}"; \
    echo "${node_sha}  /tmp/node.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \
    rm /tmp/node.tar.xz; \
    rm -rf /usr/local/lib/node_modules/npm; \
    node --version

RUN case "${TARGETARCH:-amd64}" in \
        amd64) go_arch=amd64; go_sha="${GO_LINUX_AMD64_SHA256}" ;; \
        arm64) go_arch=arm64; go_sha="${GO_LINUX_ARM64_SHA256}" ;; \
        *) echo "Unsupported Go TARGETARCH=${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    curl --proto '=https' --tlsv1.2 -fsSLo /tmp/go.tgz \
        "https://go.dev/dl/go${GO_VERSION}.linux-${go_arch}.tar.gz"; \
    echo "${go_sha}  /tmp/go.tgz" | sha256sum -c -; \
    rm -rf /usr/local/go; \
    tar -C /usr/local -xzf /tmp/go.tgz; \
    rm /tmp/go.tgz; \
    go version

USER alysis
RUN --network=none --mount=type=bind,from=rust-inputs,source=/opt/rust-inputs,target=/opt/rust-inputs,ro \
    case "${TARGETARCH:-amd64}" in \
        amd64) rust_host=x86_64-unknown-linux-gnu; rustup_sha="${RUSTUP_LINUX_AMD64_SHA256}" ;; \
        arm64) rust_host=aarch64-unknown-linux-gnu; rustup_sha="${RUSTUP_LINUX_ARM64_SHA256}" ;; \
        *) echo "Unsupported Rust TARGETARCH=${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    cp "/opt/rust-inputs/rustup/archive/${RUSTUP_VERSION}/${rust_host}/rustup-init" /tmp/rustup-init; \
    echo "${rustup_sha}  /tmp/rustup-init" | sha256sum -c -; \
    chmod +x /tmp/rustup-init; \
    RUSTUP_DIST_SERVER=file:///opt/rust-inputs \
        /tmp/rustup-init -y --profile minimal --default-toolchain "${RUST_VERSION}" \
        --default-host "${rust_host}" --no-modify-path; \
    rm /tmp/rustup-init; \
    rustc -V; \
    cargo -V

USER root
RUN node -v; \
    go version; \
    pkg-config --version

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD git --version && rg --version
CMD ["sh", "-lc", "echo alysis-sandbox ready"]
WORKDIR /workspace
USER alysis

FROM dev-tools AS npm-builder
USER root
COPY scripts/sandbox/npm-rebuild/ /opt/npm-rebuild-source/
RUN python /opt/npm-rebuild-source/build.py \
    --work-dir /tmp/npm-build --output-dir /opt/npm-rebuild

FROM dev-tools AS dev
USER root
# Copy only the rebuilt distribution and public receipts. The bootstrap npm,
# original vulnerable dependencies, and retained input cache stay in the builder.
COPY --from=npm-builder /opt/npm-rebuild/npm-11.20.0-alysis.1.tgz /tmp/npm-rebuilt.tgz
COPY --from=npm-builder /opt/npm-rebuild/npm-rebuild-evidence.json /opt/npm-rebuild/package-lock.json /opt/npm-rebuild/dependency-inventory.json /usr/local/share/alysis/npm-rebuild/
RUN python -c 'import hashlib,json; from pathlib import Path; e=json.loads(Path("/usr/local/share/alysis/npm-rebuild/npm-rebuild-evidence.json").read_text()); assert hashlib.sha256(Path("/tmp/npm-rebuilt.tgz").read_bytes()).hexdigest() == e["archive_sha256"]'; \
    rm -rf /usr/local/lib/node_modules/npm; \
    mkdir -p /usr/local/lib/node_modules/npm; \
    tar -xzf /tmp/npm-rebuilt.tgz -C /usr/local/lib/node_modules/npm --strip-components=1; \
    rm /tmp/npm-rebuilt.tgz; \
    test "$(npm --version)" = "11.20.0-alysis.1"; \
    test "$(npx --version)" = "11.20.0-alysis.1"
USER alysis

FROM dev AS server
SHELL ["/bin/sh", "-euxc"]

USER root
WORKDIR /opt/alysis
# The digest is an official multi-platform uv image; the lockfile supplies hashes
# for all Python runtime and build dependencies.
COPY --from=ghcr.io/astral-sh/uv@sha256:2381d6aa60c326b71fd40023f921a0a3b8f91b14d5db6b90402e65a635053709 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE NOTICE /opt/alysis/
COPY src /opt/alysis/src
COPY schemas/forge-host-v2/forge-host-v2.schema.json /opt/alysis/schemas/forge-host-v2/forge-host-v2.schema.json
ENV UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PROJECT_ENVIRONMENT=/opt/alysis/.venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/alysis/.venv/bin:/home/alysis/.cargo/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN uv sync --locked --no-dev --no-editable --extra server; \
    uv cache clean; \
    alysis --version

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD git --version && rg --version && alysis --version
CMD ["sh", "-lc", "echo alysis-sandbox ready"]
WORKDIR /workspace
USER alysis

FROM ${VARIANT} AS final
USER alysis
