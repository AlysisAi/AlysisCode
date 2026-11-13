# Windows desktop release preparation

The first Windows desktop distribution was selected as a direct website download.
Use this guide for release gates, together with the app repository's packaging and
update instructions. Recheck the candidate and signing configuration for each
release; old identity-verification conversations do not establish current status.

## Engine and app ownership

The engine provides its capability and bridge contracts. The app owns packaging,
installation, updates, process lifecycle, workspace trust, approvals, and secure
credential storage. Follow the [desktop engine contract](desktop-engine.md) when
updating its pin and dependency recipe. A runtime wheel or signed engine binary
alone does not qualify the desktop application.

## Signing preparation

The identity owner completes any required organization/representative verification
directly with the signing provider. Check the existing validation request before
creating another. Keep identity documents, verification tokens, QR codes, and
biometric material out of source control and task logs. Keep support correspondence
in its private support channel rather than in the repository.

Follow [Managed CLI signing operations](managed-cli-signing-operations.md) for the
existing protected signing environment and publisher-verification contract.

1. Confirm that the existing organization validation is approved.
2. Confirm the Public Trust certificate profile for that approved identity and
   record the exact certificate subject. Create a profile only if one is missing.
3. Prepare the dedicated GitHub OIDC signing identity and certificate-profile-scoped
   signer permission for owner review. Use the existing repository/environment
   restriction from the signing runbook.
4. Configure the seven Windows signing variables in `managed-cli-native-signing`:
   `ALYSIS_AZURE_SIGNING_CLIENT_ID`, `ALYSIS_AZURE_SIGNING_TENANT_ID`,
   `ALYSIS_AZURE_SIGNING_SUBSCRIPTION_ID`, `ALYSIS_AZURE_SIGNING_ENDPOINT`,
   `ALYSIS_AZURE_SIGNING_ACCOUNT`, `ALYSIS_AZURE_SIGNING_CERTIFICATE_PROFILE`, and
   `ALYSIS_WINDOWS_SIGNING_SUBJECT`.
5. Verify a timestamped Authenticode signature from the approved publisher before
   treating the signing setup as working. Extend signing and verification to the
   eventual desktop executable and installer.

## Desktop release gates

- Confirm the supported Windows versions, architectures, and execution backends.
  Exercise the actual native sandbox or Docker/WSL onboarding required by the
  candidate; do not infer native Windows results from macOS checks.
- Validate the packaged runtime's identity, capabilities, bridge protocol, and
  engine pin against the app's admission requirements.
- Verify signatures and expected publisher identity for the distributed engine,
  desktop executable, and installer through the maintained signing procedure.
- Install on a clean supported machine without a preinstalled development Python
  or VS Code. Exercise account/provider setup, workspace trust, approvals, sandbox
  readiness, and the app's supported chat, coding, and Computer Use flows.
- Verify upgrades, interrupted-update recovery, rollback, uninstall behavior, and
  process cleanup while preserving user state and credentials.
- Record the exact candidate/artifact identities and results in the release PR.
  Report incomplete checks and remaining blockers explicitly before shipping.

This guide is a checklist, not a claim that a signing account is approved or a
particular desktop candidate has passed qualification. Publication is a separately
approved step; see the [release process](RELEASING.md).
