# Sandbox Image

This directory contains the Dockerfile used to build Alysis Code sandbox images
for shell execution, verification, and server workers when Docker is selected.

## Contents

- `Dockerfile` builds the supported `base`, `dev`, and `server` variants through
  the `VARIANT` build argument.

## Scope

The image is one execution backend, not the whole security model. Execution
modes, workspace binding, safe HTTP checks, MCP policy, hook trust, and tool
validation still apply outside the container.

Production deployments should pin images by digest and verify signatures and
attestations as described in the sandbox guide.

## Supply-chain contract

- Wolfi base, uv helper, and Dockerfile frontend images are digest-pinned. The
  complete inherited/transitive APK graph is version- and archive-hash-pinned for
  each architecture, authenticated by the vendor-signed APK index, and installed
  with networking disabled. The final installed inventory must equal the lock.
- Node, Go, and rustup downloads are version-pinned and SHA-256 verified for
  both `linux/amd64` and `linux/arm64`. Rust's manifest and minimal components are
  hash-pinned and installed from a local mirror with networking disabled.
- npm is a [complete locked rebuild](npm-rebuild/README.md), `11.20.0-alysis.1`,
  with four reviewed dependency fixes. Node's original bundled npm is removed in
  the installation layer. The bootstrap npm and retained input cache stay in the
  separate builder. Final `npm`/`npx`, offline operations and complete dependency
  inventory are checked before promotion.
- The server variant installs from `uv.lock` with network downloads of a
  replacement Python interpreter disabled.
- Release candidates are scanned and runtime-smoked on both architectures
  before any moving or release alias is promoted.
- Actual Docker-default ACL boundary checks and complete filesystem inventories
  run on both native architectures for every variant before promotion. Retain
  inputs and receipts for every supported release throughout its supported
  lifetime, including the rollback window.

Changing a pinned version requires changing its matching digest in the same
review and passing the sandbox release-contract tests.

## Development

Image changes should be checked with `alysis sandbox doctor --smoke` against
a locally built image when Docker is available.

## See Also

- [Shell sandbox](../../docs/shell_sandbox.md)
- [Server mode](../../docs/server.md)
