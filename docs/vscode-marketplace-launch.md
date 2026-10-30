# IDE release status

This source release includes the VS Code integration, shared sidebar, JetBrains development
preview, and the CLI protocol and release tooling they require. It does not include production
signed VSIX packages and does not announce Marketplace availability. Component packages are
for development and testing only.

## Build and verify

Use [the extension checklist](vscode_extension_release_checklist.md) for component checks,
[signing operations](managed-cli-signing-operations.md) for protected signing configuration,
and [remote acceptance](vscode_remote_acceptance.md) for WSL and Remote SSH validation.
JetBrains preview setup and limitations are documented in
[its README](../extensions/jetbrains-alysis/README.md).

## Production gates still required

1. Build the six signed runtime/VSIX candidates from an immutable public source tag.
2. Verify native signatures, manifests, SBOMs, attestations and clean installations
   for the exact candidates. Keep existing tags immutable.
3. Install the exact candidates and complete live-provider, real WSL/Remote SSH, and untrusted
   workspace acceptance, with dedicated credentials and an approved provider spending limit.
4. Collect candidate-bound signoff and rollback evidence before explicit Marketplace promotion.

Production builds, attestations, and promotion use `AlysisAi/AlysisCode`. The source tag must
match the CLI version; existing tags are never moved. The extension version is independent.
The `0.3.x` line selects `channel: beta` and publishes only through the Marketplace pre-release
channel after all evidence gates pass. A GitHub source release does not replace those gates.
