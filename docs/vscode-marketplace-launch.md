# IDE release status

CLI source version: **0.14.8**. VS Code extension: **0.3.0**, planned Marketplace pre-release.

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

1. Production signing passed for all six native runtimes in candidate `37294262789`
   from immutable `v0.14.5`, including both Windows Authenticode signatures, both
   Apple notarizations, and the assembled ECDSA manifest. Downloaded native artifacts
   were independently checked against their exact source/run attestations and SBOMs.
   An independent encrypted manifest-key recovery copy and custody procedure were
   verified on 4 October against the unchanged public key; audit delivery also passed.
2. Sandbox qualification and publication passed on `a0ef8fff`, public run
   `37221977397`, including six native isolation/full-filesystem checks, zero
   HIGH/CRITICAL scan findings, signatures, and independently verified attestations.
   The native packaging and VSIX SBOM fixes do not alter these published image bytes.
3. Build six signed runtime/VSIX candidates from a new immutable public source tag.
   Candidate `37443579929` from `v0.14.7` passed all six native signatures,
   manifest assembly, and the corrected dependency/rendering gates. Linux and
   Windows x64 clean installations passed, but final evidence validation used
   Python outside its locked environment; current macOS testing also hit the
   older test utility's executable-path bug. A Windows ARM64 interpreter download
   was incomplete. Version 0.14.8 corrects the verification environment, updates
   the test utility, and retries downloads without relaxing integrity checks.
   The replacement candidate still requires all signatures, manifests, SBOMs,
   attestations, and clean-installation evidence to pass. Existing tags remain immutable.
4. Install the exact candidates and complete live-provider, real WSL/Remote SSH, and untrusted
   workspace acceptance, with dedicated credentials and an approved provider spending limit.
5. Collect candidate-bound signoff and rollback evidence before explicit Marketplace promotion.

Production builds, attestations, and promotion use `AlysisAi/AlysisCode`. The source tag must
match the CLI version; existing tags are never moved. The extension version is independent.
The `0.3.x` line selects `channel: beta` and publishes only through the Marketplace pre-release
channel after all evidence gates pass. A GitHub source release does not replace those gates.
