# Managed CLI signing operations

This runbook covers the complete signing chain used by
`.github/workflows/managed-cli-vsix-release.yml`: Windows Authenticode, Apple Developer ID and
notarization, the ECDSA P-256 managed-runtime manifest signature, GitHub attestations, and the
dependency policy applied before any key can bless an artifact. These controls are complementary;
none is a substitute for another.

See [the release status](vscode-marketplace-launch.md) for remaining production gates.
Signing, attestations, and publication run from immutable source tags in the public
`AlysisAi/AlysisCode` repository. Component tests never authorize production publication.

## Signed release contract

Managed-runtime manifests use closed schema version 3 and the domain
`sylliptor-managed-cli-release-v3`. The pre-rename domain string is frozen on purpose: it is part of
the signed bytes, so renaming it would invalidate every signature already issued under schema v3
(see the note in `extensions/vscode-alysis/src/runtime/ManagedCliReleaseSecurity.ts` and
`scripts/release/build_managed_cli_manifest.py`). Each artifact has its own signature over canonical JSON. The
signed record includes all of the following; changing any one of them invalidates the signature:

- release tag, canonical source-repository identity, and exact 40-character source commit;
- manifest/artifact and CLI versions, extension/CLI/protocol compatibility ranges, and signing
  key ID;
- provenance issuer and the workflow builder identity qualified by the same release tag;
- platform target, executable filename and HTTPS URL, exact byte size, executable SHA-256, and
  CycloneDX SBOM SHA-256;
- native-signature policy, signer identity, and SHA-256 of the validated schema-v2 native evidence
  record (`authenticode` on Windows, `apple-developer-id-notarized` on macOS, and `not-applicable`
  on Linux). The evidence itself binds the exact post-signing executable filename and SHA-256;
  Windows also records timestamp/certificate evidence, while macOS records the canonical Developer
  ID authority and notarization submission UUID.

The builder accepts only one artifact, SBOM, and explicit native-signature policy for each of the
six supported desktop targets. Artifact URLs must bind the configured source repository, release
tag, and filename. The extension pins the official source repository, OIDC issuer, and release
workflow identity independently of the manifest.

On installation, the extension verifies the v3 signed record, downloaded/bundled bytes, compatibility,
and CLI health before atomic activation. It persists the complete signed record in immutable
manager-owned metadata and reconstructs and verifies that record on every later use. Editing stored
compatibility, source, provenance, SBOM, native-signature, key, or artifact data therefore fails
closed.

## GitHub environments and credential inventory

### Native build inputs

Managed executables bundle CPython 3.12.15 from the six checksum- and size-pinned
Astral distributions in `scripts/release/managed_cli_python.lock.json`. The installer
checks the downloaded archive before extraction and probes the installed version,
platform, architecture, OpenSSL, and Expat. The build SBOM binds that receipt to the
committed distribution pin. This packaging interpreter is independent of the
project's supported Python range and the interpreter used to bootstrap CI tools.

Linux builds and executable smoke tests use the pinned manylinux glibc 2.28 images.
The interpreter inside the image only bootstraps the pinned distribution; it is not
the Python embedded in the executable. Windows ARM64 and Intel macOS build
checksum-pinned OpenSSL 3.5.9, run its native test suite, and link it statically into
cryptography. This supplies the missing Windows ARM64 development libraries and
avoids an Intel macOS collision between cryptography and Python's `libssl.3.dylib`.
The resulting OpenSSL component and dependency edge are recorded in the SBOM.

The unsigned `managed-cli-build-check.yml` workflow exercises the same native build
helpers, executable version/health/stdio checks, and runtime inventory before a new
immutable release tag is created. It receives no signing environment and does not
replace the signed candidate's provenance, signature, or installation checks.

### Protected environments

Create two GitHub environments. Both environments must have at least one required release
maintainer and contain exactly the custom deployment tag pattern `v*`, with administrator bypass
disabled. Self-review is normally prevented. The owner-authorized single-maintainer exception
allows self-review only when the sole required reviewer is `Perdikis10` (GitHub user ID
`190930654`), pinned in `scripts/release/vscode_approval_policy.py`. That maintainer may also fill
all three signoff roles, but must actually approve each signing, evidence, and promotion gate.
Environments with other reviewers must prevent self-review. The unprotected source-validation job
checks these rules through the versioned GitHub API before either protected job can start. Do not
copy an environment secret or variable to repository or organization scope.

### `managed-cli-native-signing`

The six-target build matrix enters this environment. Configure exactly these environment-scoped
values:

| Name | GitHub type | Required format and purpose |
| --- | --- | --- |
| `ALYSIS_AZURE_SIGNING_CLIENT_ID` | variable | Application ID of the dedicated GitHub OIDC signing identity. |
| `ALYSIS_AZURE_SIGNING_TENANT_ID` | variable | Microsoft Entra tenant ID owning that identity. |
| `ALYSIS_AZURE_SIGNING_SUBSCRIPTION_ID` | variable | Subscription containing the signing account. |
| `ALYSIS_AZURE_SIGNING_ENDPOINT` | variable | `https://weu.codesigning.azure.net/` for this deployment. |
| `ALYSIS_AZURE_SIGNING_ACCOUNT` | variable | `alysis-code-signing`. |
| `ALYSIS_AZURE_SIGNING_CERTIFICATE_PROFILE` | variable | Approved Public Trust certificate profile name. |
| `ALYSIS_WINDOWS_SIGNING_SUBJECT` | variable | Exact Windows certificate `Subject` of the verified publisher, including the full distinguished name. |
| `ALYSIS_APPLE_DEVELOPER_ID_P12_BASE64` | secret | One-line base64 of the Developer ID Application PKCS#12 identity and private key. |
| `ALYSIS_APPLE_DEVELOPER_ID_P12_PASSWORD` | secret | Password for the PKCS#12 identity. |
| `ALYSIS_APPLE_DEVELOPER_IDENTITY` | variable | Exact `Developer ID Application: Legal Name (TEAMID)` authority returned by `codesign`/Keychain. |
| `ALYSIS_APPLE_NOTARY_KEY_P8_BASE64` | secret | One-line base64 of the App Store Connect API `.p8` key used only by `notarytool`. |
| `ALYSIS_APPLE_NOTARY_KEY_ID` | secret | Key ID belonging to the `.p8` key. It is treated as a secret because the workflow contract reads it from `secrets`. |
| `ALYSIS_APPLE_NOTARY_ISSUER_ID` | secret | App Store Connect issuer ID for the notary key, also stored as a secret by contract. |

The Windows build signs through Azure using GitHub OIDC. There is no Windows PFX or client secret
in GitHub. Each target must have a valid embedded Authenticode signature, an exact match to the
protected publisher subject, and a timestamp. Azure rotates its short-lived leaf certificates, so
the two Windows targets may have different certificate hashes. Each manifest record pins the
actual target's `sha256:<64 lowercase hex>` certificate identity and native-evidence digest.
The package job receives no native-signing environment: it checks the downloaded signature against
that target's signed identity. It must never substitute a subject-only check for that digest check.

Before the first signed build, finish Azure identity validation and create a Public Trust profile.
Create a dedicated Entra application/service principal with a federated credential restricted to:

- Issuer: `https://token.actions.githubusercontent.com`
- Audience: `api://AzureADTokenExchange`
- Subject: `repo:AlysisAi@252910184/alysis-code@1233295735:environment:managed-cli-native-signing`

The public repository uses GitHub's immutable subject format, verified on 29 September 2026
through `GET /repos/AlysisAi/AlysisCode/actions/oidc/customization/sub` (`use_default: true`,
`use_immutable_subject: true`). Its returned `sub_claim_prefix` includes the owner and repository
IDs shown above. Use that exact prefix with the environment context; the older name-only subject
does not match this repository's tokens. Recheck the endpoint before provisioning or changing
the trust. See [GitHub's OIDC reference](https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims).

Grant **Artifact Signing Certificate Profile Signer** only on that certificate profile. Do not
grant subscription Contributor or Owner. Have the authorized maintainer approve this new access
assignment, then set the seven variables above in the protected environment. Read the expected
certificate subject from the approved profile/certificate; do not guess its formatting. The
workflow rejects missing settings and signs only the matrix target's single executable.

Integration references: [Azure OIDC setup](https://github.com/Azure/artifact-signing-action/blob/c7ab2a863ab5f9a846ddb8265964877ef296ee82/docs/OIDC.md)
and [short-lived certificate management](https://learn.microsoft.com/en-us/azure/artifact-signing/concept-certificate-management).

The Apple build imports its identity into an ephemeral runner keychain, checks the exact authority,
passes that identity to PyInstaller so embedded libraries are signed before the one-file archive
is sealed, submits the signed executable to Apple, and requires an `Accepted` response. It checks
the signature with `codesign --verify --strict` and the online ticket with
`codesign --verify --strict -R="notarized" --check-notarization`. Apple documents this check for
[non-app code](https://developer.apple.com/forums/thread/130560); `spctl --type execute` expects
an app bundle and rejects standalone tools even after notarization. Standalone Mach-O files cannot be
described as directly stapled. The manifest builder reads the validated evidence file directly;
signer identity is never transferred through `GITHUB_ENV`.

### `managed-cli-signing`

Only the `sign-and-assemble` job enters this environment. Configure exactly:

| Name | GitHub type | Required format and purpose |
| --- | --- | --- |
| `ALYSIS_MANAGED_CLI_SIGNING_KEY` | secret | Unencrypted ECDSA P-256 private key in `BEGIN EC PRIVATE KEY` PEM form. GitHub encrypts it at rest; the workflow writes it only to a mode-`0600` runner-temporary file and deletes it in an `always()` step. |
| `ALYSIS_MANAGED_CLI_PUBLIC_KEY_SHA256` | variable | Lowercase 64-hex SHA-256 of the DER-encoded public key derived from the private key. |

The derived public key must byte-match
`extensions/vscode-alysis/resources/managed-cli-release-public.pem`, and its DER fingerprint must
match the protected variable. The immutable key ID in the workflow must identify this exact key;
changing key material requires a new key ID and the rotation sequence below.

### Provisioning validation

Before enabling either environment:

1. Have the authorized release maintainer verify the inventory in GitHub **Settings > Environments** and
   confirm there are no same-named repository or organization secrets/variables shadowing the
   intended values.
2. Decode the Apple PKCS#12 only inside the approved secret-management or signing boundary.
   Confirm it contains a private key, the expected subject/team, code-signing usage, trust chain,
   and a validity window that exceeds the next release plus the rollback window.
3. Confirm the Azure OIDC subject and audience, profile-scoped signer role, and protected publisher
   subject. Verify a timestamped signature on each Windows target. Certificate rotation is expected;
   each target's evidence and signed manifest must still bind its exact certificate SHA-256.
4. Confirm the Apple authority matches `codesign -dvv` exactly and test the API key, key ID, and
   issuer together with `notarytool` against a disposable signed artifact.
5. Derive the ECDSA public PEM and DER SHA-256 independently, compare the PEM with the checked-in
   file, and compare the 64-hex fingerprint with the environment variable under the approval policy above.
6. Inspect the environment protection rules and the workflow permission block. The workflow must
   retain `contents: read`, the minimum OIDC/attestation permissions on attesting jobs, no release
   or package publication permission, and GitHub-hosted runners only.

## Manifest-key custody and approval

Before exporting any private material, identify the approved recovery location and
custodian. The offline checker can first inspect the public pin without a secret:

```bash
python scripts/release/verify_managed_cli_key_custody.py
```

The DER fingerprint observed on 28 September 2026 is
`9c9054ccb2540eef5af95fc904d3af986798c880f86353717dc18ff6de6fa07e`.
This is the expected public identity, not proof of private-key custody or a configured
GitHub environment. The resource PEM and embedded trust anchor remain unchanged.

On 4 October 2026, the owner approved Perdikis10 as recovery custodian and a separate
organization-controlled Azure Key Vault copy. Retrieval inside Azure Cloud Shell
passed this checker against the unchanged public pin; private material went directly
through stdin without a file or secret output. Access logging was verified from
delivered records and temporary network access removed. The private operations
record holds the vault/version coordinates and recovery procedure. This proves
recovery custody, not the protected workflow's provisioned key or native signing.

Once the custodian locates the approved backup, run the same checker **inside the
approved secret-management boundary** with `--private-key-stdin` and
`--approved-fingerprint <reviewed-public-DER-SHA256>`. Pipe the secret manager's export
directly into stdin; do not paste it into a terminal, interpolate it into arguments,
enable shell tracing, or save it in this checkout. The checker accepts one unencrypted
P-256 `EC PRIVATE KEY` PEM, compares its derived public PEM byte-for-byte, and emits only
public fingerprint/match fields. It neither uploads a secret nor configures GitHub.
The actual protected signing workflow must still verify the provisioned key.

The legacy repository secret `SYLLIPTOR_MANAGED_CLI_SIGNING_KEY` was last updated
29 July 2026 at 14:04:26 UTC according to GitHub's read-only metadata. GitHub cannot
return its value, and this timestamp does not identify its owner or prove a key match.
Preserve it while establishing custody; do not dispatch a diagnostic workflow to
extract it or silently substitute it for the environment key.

- Store `ALYSIS_MANAGED_CLI_SIGNING_KEY` only in the protected GitHub environment
  `managed-cli-signing`; never use a repository, organization-wide, local `.env`, or runner file
  secret for the production key.
- Require a release maintainer as an environment reviewer under the approval policy above, restrict
  the environment to the exact custom `v*` deployment tag pattern, and grant the workflow no
  release-publication permission.
- Store the lowercase SHA-256 fingerprint of the public-key DER as the protected environment
  variable `ALYSIS_MANAGED_CLI_PUBLIC_KEY_SHA256`. A key is approved only when that variable,
  the checked-in public PEM, and the public key derived from the protected private key all match.
- Keep the offline recovery copy in an organization-controlled secret manager with access logging,
  hardware-backed encryption, retrieval approved under the maintainer policy above, and a documented owner. Do not export it to a
  developer workstation for routine releases.

## Native certificate and notary-key lifecycle

Assign a named primary and backup owner. Check the Windows and Apple certificate expiry dates at
least monthly and before every candidate; alert at 90, 60, 30, and 14 days. Test the Apple notary
API key monthly because it has no useful certificate-expiry signal and may be revoked independently.
Record the check time, owner, certificate subject/team, SHA-1 lookup thumbprint (Windows), SHA-256
certificate digest, and remaining validity without recording private material.

Azure manages routine Windows leaf-certificate rotation. For an account, profile, or publisher
identity change, approve the new profile-scoped role and protected variables, then run an
artifact-only candidate. Independently run `Get-AuthenticodeSignature` on both downloads and check
each against its own signed manifest record. Remove old identity access after in-flight runs end.
Revoke affected certificates and access immediately if compromise is suspected.

For planned Apple rotation, update the PKCS#12, password, and exact Developer ID authority together.
Rotate the notary `.p8`, key ID, and issuer as one tested set when the API key changes. Require both
macOS targets to pass strict `codesign` verification, notarization, and the explicit
`notarized` requirement with `--check-notarization`; retain the certificate SHA-256 and Apple
submission IDs in the release record. Revoke replaced or exposed material in the Apple/CA portals
after the clean run is verified.

For an emergency native-key incident, disable `managed-cli-native-signing`, cancel in-flight runs,
revoke the affected certificate or API key with its issuer, and temporarily disable
`managed-cli-signing` so no manifest can bless uncertain bytes. Invalidate every unpublished
candidate produced after the last known-good use. For already distributed artifacts, publish the
affected hashes and remediation guidance, rotate cleanly, and require a new six-target run.

## Rotation

The runtime trust set assigns every key one explicit state:

- `active`: may authenticate new installs and already-installed runtimes;
- `retiring`: may authenticate an already-installed last-known-good runtime during the bounded
  rollback window, but cannot authenticate a new install;
- `revoked`: cannot authenticate either new or installed bytes.

1. Open a security-reviewed rotation change that names the retiring and replacement fingerprints.
2. Generate the replacement key in the approved secret manager/HSM boundary.
3. Add the replacement public key with a new immutable key ID as `active`; change the previous key
   to `retiring`. Ship this trust-set update before signing with the replacement key.
4. Update the protected environment secret and fingerprint variable under the maintainer approval policy above.
5. Run the artifact-only managed CLI workflow and verify all six manifest signatures, CycloneDX
   SBOMs, and GitHub/Sigstore build attestations before any distribution decision.
6. After the rollback window, mark the old key `revoked` and revoke its private key. Remove its
   public key only after no supported installed runtime can legitimately require it. Record the
   state transitions, final artifact digest, workflow run, reviewers, and timestamps in the
   release issue.

Do not reuse a key ID, silently replace the public material for an existing ID, or switch an old key
back from `revoked`/`retiring` to `active`. Emergency revocation may intentionally leave no active
release key until a replacement-trust update ships; the extension must remain fail-closed.

Rotate at least annually and immediately after suspected exposure, maintainer offboarding, secret
manager compromise, or an unexplained signature/fingerprint mismatch.

## Service outage handling

- **Windows timestamp service:** never publish an untimestamped or partially signed fallback. Keep
  the environment disabled for promotion, record the provider response and time, and retry with a
  fresh workflow run after recovery. Switching timestamp providers requires a reviewed workflow
  change and an independently verified dry run; do not patch a tag or bypass `Status == Valid`.
- **Apple notarization:** an outage, timeout, or status other than `Accepted` is a hard failure. Do
  not replace notarization with ad-hoc signing, forge the evidence JSON, or treat `codesign` alone as
  sufficient. Cancel or let the failed run expire, preserve the submission ID/log, and rerun all
  affected target stages after Apple recovers.
- **GitHub OIDC/attestation:** do not sign the ECDSA manifest or promote a VSIX when provenance or
  SBOM attestations cannot be created and independently verified. Retry from the immutable tag after
  service recovery; never reuse unattested intermediate downloads from a failed run.

## Dependency audit policy and reviewed exception

The workflow runs `scripts/release/audit_locked_dependencies.py` against the committed `uv.lock`.
Known vulnerabilities always block. New adverse statuses, duplicate/malformed records, uv JSON
schema drift, or a changed status for an allowlisted package also block.

The only maintenance-status exception is exactly `socksio` with status `archived`. Version 1.0.0 is
a transitive dependency of `ddgs` through `httpx[socks]`; it is currently retained because the audit
reports no known vulnerability and replacing it is not a release-local change. This is not a
vulnerability waiver. Reconfirm the dependency path, version, zero-vulnerability result, and lack
of a supported upstream replacement for every release, record the reviewer in the release issue,
and remove the allowlist as soon as the upstream chain no longer needs it. Any vulnerability or
status other than `archived` fails closed without an exception.

`Pillow >= 12.3.0` is a deliberate security floor because 12.3.0 contains multiple parser,
decompression-bomb, memory-safety, and command-injection fixes. Do not lower the floor or accept an
older locked wheel. Upgrades must regenerate `uv.lock`, pass the locked audit on every target,
preserve the Linux glibc 2.28 compatibility gate, and appear in the signed dependency and final
VSIX SBOMs. Review the upstream
[Pillow 12.3.0 security notes](https://pillow.readthedocs.io/en/stable/releasenotes/12.3.0.html)
when recording the dependency decision.

## Operator dry run and release checklist

The workflow is artifact-only and has no publish permission, so a controlled rerun from the exact
immutable version tag is the production-like dry run. A dry-run candidate must be labelled **never
promote** in the release issue and allowed to expire or be deleted after evidence review.

Before dispatch:

- [ ] Confirm the selected ref is the existing protected `vX.Y.Z` tag matching
      `pyproject.toml`, and record its 40-character commit.
- [ ] Record both environment reviewers and verify the credential inventory/scopes above without
      revealing secret values.
- [ ] Confirm Windows and Apple expiry monitoring is green, the Apple notary key test is current,
      and no rotation or incident is in progress.
- [ ] Confirm the checked-in ECDSA public PEM, protected DER fingerprint, key ID, and private key
      form one approved set.
- [ ] Run the locked dependency audit; record the reviewed `socksio (archived)` result and confirm
      Pillow is at least 12.3.0.
- [ ] Confirm the pinned manylinux 2.28 image digests and all action/uv pins were reviewed in the
      tagged commit.

During the run:

- [ ] Approve `managed-cli-native-signing` only after reviewing the exact tag and commit.
- [ ] Require all six frozen executables to pass native-host smoke; require both Linux artifacts to
      build and smoke inside the pinned glibc 2.28 images.
- [ ] Require both Windows identities to be valid and equal, both Apple authorities to be valid and
      equal, Apple notarization to be `Accepted`, and all six provenance/SBOM attestations to verify.
- [ ] Approve `managed-cli-signing` only after the native evidence and exact source attestations are
      green. Never approve it merely to diagnose a failed native build.
- [ ] Require the signed manifest to contain six exact targets and the package matrix to produce one
      pre-release VSIX per target with exactly one matching runtime.
- [ ] Require every final VSIX SBOM to merge the signed target dependency inventory and require both
      final VSIX provenance and SBOM attestations to verify.
- [ ] Require every isolated installed-production test to report the matching target, managed
      origin, empty CLI override, verified release signature, exact hashes/versions, and live bridge
      health.

After the run:

- [ ] Independently download and hash all six VSIX files and managed runtimes. Re-run
      `gh attestation verify` with the repository, signer workflow, source tag, and source commit
      constraints used by CI.
- [ ] Re-run `Get-AuthenticodeSignature` for Windows and strict `codesign --verify` plus
      `codesign --verify --strict -R="notarized" --check-notarization` for macOS
      for macOS; compare identities with the signed manifest and release record.
- [ ] Preserve the workflow URL, approvals, logs, Apple submission IDs, manifest/public-key
      fingerprint, dependency and final SBOMs, production-dogfood summaries, and exact SHA-256
      values. Do not preserve decoded private material or secret-bearing temporary files.
- [ ] Complete `docs/vscode_extension_production_signoff_template.md`. A green artifact workflow is
      necessary but does not replace real-provider, multi-OS, known-limitations, rollback, and final
      promotion approval.

## Release evidence and incident response

- Preserve the workflow run URL, source commit, exact VSIX/runtime SHA-256 values, public-key
  fingerprint, SBOMs, and attestation verification output with the release record.
- Verify provenance with `gh attestation verify <artifact> --repo AlysisAi/AlysisCode` before signing
  the manifest and again before promotion.
- A missing environment reviewer, invalid/missing fingerprint variable, unexpected public-key diff,
  absent SBOM/attestation, or native-signature failure blocks promotion.
- Each extension activation reconciles its signed bundled runtime before selecting the installed
  pointer. A newer compatible bundle is activated atomically; an older bundle cannot downgrade a
  newer valid runtime; a failed upgrade retains the validated current/last-known-good candidate.
- On suspected compromise, disable the environment, cancel in-flight runs, revoke/rotate the key,
  invalidate unpublished artifacts, publish a security notice for distributed affected hashes, and
  do not resume until a new trust set and clean six-target build are independently verified.
