# Release policy and Marketplace promotion

## Versioning

The extension version comes from `package.json`; the immutable `vX.Y.Z` source tag identifies
its bundled CLI version from `pyproject.toml`. These versions are independent. Marketplace
versions use `major.minor.patch`; stable candidates set `preview: false` and omit `--pre-release`.

The candidate, evidence, installed-provider, untrusted-workspace and promotion workflows take an
explicit `channel` input (`stable` or `beta`). Use `beta` throughout the `0.3.x` candidate line.
Stable packaging uses even minor versions; beta packaging uses odd minor versions and a true
Marketplace pre-release marker. `npm run package:pre-release -- --target <target>` verifies the
staged runtime's signature, bytes, compatibility and pinned origin before packaging a beta.
`npm run package:vsix -- --target <target>` performs the same checks for stable packages.
The native candidate workflow uses the same guard. Stable validators reject beta artifacts;
beta validators reject stable artifacts. Neither channel bypasses production evidence.
Use `npm run package:dev-vsix -- --pre-release` for component CI without a signed runtime;
those packages are never production release evidence.

## Release channels

- **Internal VSIX dogfood** — local package installs for maintainers. Requires extension compile,
  lint, unit tests, audit, package, Python IDE bridge smoke, and deterministic local mock dogfood.
  It is not Marketplace-visible.
- **Public beta / Marketplace pre-release** — requires Windows/default-console encoding tests, full
  Python Forge exec/swarm/review suites, Extension Host integration, a target-specific VSIX with a
  signed managed runtime and passing isolated production install, completed manual real-provider
  dogfood in a disposable repo, no known P0/P1 blockers, and current known limitations.
- **Stable Marketplace release** — requires all beta gates plus explicit release approval, support
  readiness, Marketplace publisher/PAT setup, and no unsupported feature claims in Marketplace copy.

## Marketplace readiness

The manifest includes repository metadata, keywords, gallery banner metadata, and a PNG Marketplace
icon. The SVG Activity Bar icon remains separate.

Marketplace promotion is implemented as a manual, protected, fail-closed workflow. Before it can be
enabled, maintainers need:

- a confirmed Visual Studio Marketplace publisher identity for `alysisai` or a final replacement
- a `vscode-marketplace-beta` or `vscode-marketplace-stable` GitHub environment matching the channel,
  with required reviewers and a scoped Azure DevOps
  Marketplace PAT stored as its `VSCE_PAT` secret
- protected `vscode-installed-live-provider-qa`, `vscode-real-wsl-acceptance`,
  `vscode-real-remote-ssh-acceptance`, `vscode-release-evidence`, and `managed-cli-release-assets`
  environments. Each must have a required reviewer, disable administrator bypass, and allow only
  the custom `v*` deployment tag pattern. Self-review is allowed only with sole reviewer
  `Perdikis10` (GitHub user ID `190930654`), as pinned in the release approval policy; that user
  may fill all signoff roles with actual approvals at each gate. Other configurations require
  self-review prevention and distinct approvers. Workflows verify this configuration before entry.
- `ALYSIS_LIVE_API_KEY` stored only as a secret in `vscode-installed-live-provider-qa`, with
  `ALYSIS_LIVE_QA_PROVIDER`, `ALYSIS_LIVE_QA_MODEL`, and `ALYSIS_LIVE_QA_BASE_URL` plus
  `ALYSIS_LIVE_QA_APPROVED_ORIGIN` stored as variables in that environment. Do not create
  repository- or organization-scoped copies. The mandatory approved origin must be canonical public
  HTTPS on the default port and exactly match the reviewed origin of the base URL; it is validated
  before the provider key is consumed.
- six checked VSIX files for the selected channel from the native candidate workflow
- a schema-v2 production signoff approving `marketplace-beta` or `marketplace-stable`, and the rollback action
  `unpublish_affected_version`; prior pre-release approvals cannot authorize stable promotion
- a checked README, CHANGELOG, SUPPORT file, and release checklist
- full Python tests, including the Forge exec/swarm/review suites
- VS Code `npm ci`, compile, lint, unit/bridge smoke tests, audit, package, and Extension Host tests
- passing generic component dogfood with `component_valid: true`, `release_valid: false`, and
  `extension_host: passed`
- a target-specific production-install summary with `status: passed`, `release_valid: true`, exact
  VSIX/managed-runtime hashes and platform target, managed production origin, no CLI override, and
  passing native/release-signature, package-install, and bridge-health checks
- verified final VSIX provenance and CycloneDX SBOM attestations bound to the immutable tag/commit
- manual VSIX install verification
- a completed `--mode manual-real-provider` checklist and a real Forge Execute v1 review-mode smoke
  in a disposable repository

## Promotion workflow

`.github/workflows/vscode-extension-evidence.yml` and
`.github/workflows/vscode-extension-promote.yml` are dispatch-only. First run the reviewer-gated
evidence workflow from the exact `vX.Y.Z` source tag with the successful candidate, six-target
installed-provider, actual untrusted-workspace, and real WSL/Remote-SSH run IDs, literal
confirmation `attest-release-evidence`, and the reviewed nine-file manual inventory digest from
`scripts/qa/hash_vscode_release_evidence.py`. It validates six schema-v5 provider reports plus the
schema-v3 environment matrix, six redacted installed-provider reports, one actual-untrusted report,
and two real-remote reports against the exact packages, production summaries, runtimes, and SBOMs.
It verifies the original attestations and re-attests one immutable evidence artifact. Run promotion
from the same tag with those run IDs, the exact WSL and Remote-SSH run attempts, the evidence run
ID, matching `channel`, and confirmation `publish-beta` or `publish-stable`. The promotion
receipt (schema v7) binds the channel as well as all input hashes. Release-manager approval
must come from that channel's protected Marketplace environment. Remote runner trust and cancellation recovery are
documented in [`docs/vscode_remote_acceptance.md`](../../../docs/vscode_remote_acceptance.md).

Before any protected environment exposes write access or `VSCE_PAT`, the unprivileged job verifies
every workflow identity/attestation, candidate bytes, exact SLSA and CycloneDX predicates, and a
frozen input receipt. Protected jobs re-download and revalidate every byte. Managed-runtime release
assets reject conflicts; Marketplace publication accepts exact existing target bytes, publishes only
missing targets in the selected channel, polls the public state, and verifies all six downloaded VSIX bytes.
Channel collisions and conflicting bytes stop promotion.

The generic component-candidate workflow verifies the VSIX pre-release marker before uploading
artifacts, but its VSIX remains explicitly not-for-release. Manual provider, no-P0/P1, known-limit,
multi-OS, and rollback evidence are collected only after the exact target candidate exists.

## Rollback

If a released package must be pulled, use the Marketplace publisher portal to unpublish the affected
version, document the reason and replacement plan in release notes, and disable any future publish
workflow secret or environment approval until the issue is understood.
