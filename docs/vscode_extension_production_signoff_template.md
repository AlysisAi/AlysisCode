# VS Code Extension Production Signoff Template

Use this template for a signed beta or stable production promotion decision. Attach it to the release issue
or release-candidate record after the target-specific automated gate and manual dogfood evidence
are complete. A generic component VSIX is never eligible for this signoff.

## Candidate

- Extension version:
- Platform target:
- Target-specific VSIX path:
- VSIX SHA-256:
- Git commit:
- Immutable release tag:
- Candidate workflow run ID:
- Installed live-provider workflow run ID:
- Installed live-provider workflow run attempt:
- Untrusted-workspace workflow run ID:
- Untrusted-workspace workflow run attempt:
- Real remote-acceptance workflow run ID:
- Retained WSL report attempt:
- Retained Remote-SSH report attempt:
- Protected evidence workflow run ID:
- Protected evidence workflow run attempt:
- Promotion workflow run ID:
- Release channel: marketplace-beta or marketplace-stable (schema-v2 production signoff; must match the workflow channel)
- Reviewer:
- Completed at:

## Automated Checks

- [ ] Generic component validation passes with
      `ALYSIS_RELEASE_STRICT=1 bash scripts/qa/check_vscode_extension_release_candidate.sh`;
      its artifact remains explicitly not releasable.
- [ ] `.github/workflows/managed-cli-vsix-release.yml` succeeds for all six platform targets from
      the exact immutable tag and protected signing environments.
- [ ] `.github/workflows/vscode-extension-evidence.yml` validates and attests the exact six
      schema-v5 provider reports plus schema-v3 environment matrix in the protected
      `vscode-release-evidence` environment.
- [ ] Six-target installed-provider, actual-untrusted-workspace, and real WSL/Remote-SSH workflows
      passed for the same candidate; their original attestations verify and their reports are
      frozen into the protected evidence receipt.
- [ ] The installed-provider environment used an environment-scoped
      `ALYSIS_LIVE_API_KEY` secret plus reviewed provider/model/base-URL environment variables;
      no credential or provider response text is retained in the evidence artifact.
- [ ] Every signing/provider/evidence/publication environment has a required reviewer, disables
      administrator bypass, and has exactly the custom deployment tag pattern `v*`. Self-review
      is enabled only under the single-maintainer exception enforced by
      `scripts/release/vscode_approval_policy.py`; the workflow policy validator passed before entry.
- [ ] `python scripts/qa/check_ide_cli_parity.py`
- [ ] `python -m pytest -q tests/test_ide_cli_parity_matrix.py tests/test_ide_protocol.py tests/test_ide_stdio_bridge.py tests/test_ide_protocol_contract.py`
- [ ] `python -m pytest -q tests/test_release_smoke.py tests/test_managed_cli_release_manifest.py tests/test_managed_cli_release_smoke.py tests/test_vscode_vsix_sbom.py`
- [ ] `cd extensions/vscode-alysis && npm test && npm run lint && npm audit --audit-level=high`
- [ ] VSIX channel matches signoff: beta has the pre-release marker; stable has no pre-release
      marker and the package is not preview.
- [ ] Target VSIX was installed into fresh user-data/extensions directories and activated twice in
      `ExtensionMode.Production` against the same isolated profile; the test driver was separate
      from the installed extension and runtime identity stayed stable across restart.
- [ ] Managed CLI native signature/evidence, schema-v3 signed manifest, locked dependency audit,
      executable provenance/exact SBOM predicate, and final VSIX provenance/exact SBOM predicate
      all verified.

## Dogfood Evidence

- Release-valid dogfood summary path:
- Confirm summary fields:
  - [ ] `status: passed`
  - [ ] `release_valid: true`
  - [ ] `schema_name: installed-production-vsix`, `schema_version: 3`
  - [ ] `vscode_compatibility.minimum` proves the declared VS Code 1.90.0 floor
  - [ ] `vscode_compatibility.current_stable` proves the current Stable build on the same target
  - [ ] `extension_host: passed`
  - [ ] `extension_under_test: packaged_vsix`
  - [ ] `vsix` identifies the exact candidate artifact
  - [ ] `vsix_sha256` matches the retained candidate artifact
  - [ ] `platform_target` matches the candidate target
  - [ ] `runtime_origin: managed`
  - [ ] `runtime_production: true`
  - [ ] `cli_path_override` is empty
  - [ ] `release_signature_check: passed`
  - [ ] `native_signature_check: passed`
  - [ ] `package_install_check: passed`
  - [ ] `bridge_health: passed`
  - [ ] `restart_check: passed`, `restart_profile_reused: true`
  - [ ] `restart_runtime_identity_check: passed`, `extension_host_launches: 2`
- Manual real-provider report path:
- Manual report validator result (`<report.json> <target.vsix> <production-dogfood.json>`):
- Environment acceptance report path (`vscode-alysis-environment-acceptance.json`):
- Closed redacted check-artifact bundle path (`vscode-alysis-human-check-artifacts.zip`):
  - Every unique `artifact_sha256` in the manual and environment reports resolves to exactly one
    non-empty safe member below `artifacts/`; no unreferenced or secret-bearing members are allowed.
- Six installed live-provider report paths:
- Actual untrusted-workspace report path:
- Real WSL and Remote-SSH report paths:
- Candidate-bound protected evidence receipt path:
- Closed production signoff/rollback receipt path (`vscode-alysis-production-signoff.json`):
- OS smoke evidence:
  - [ ] Windows:
    CLI discovery / bridge startup / SecretStorage / chat-run / image paths / Forge plan-execute / artifacts-diffs / extension reload / Workspace Trust:
  - [ ] macOS:
    CLI discovery / bridge startup / SecretStorage / chat-run / image paths / Forge plan-execute / artifacts-diffs / extension reload / Workspace Trust:
  - [ ] Linux:
    CLI discovery / bridge startup / SecretStorage / chat-run / image paths / Forge plan-execute / artifacts-diffs / extension reload / Workspace Trust:
  - [ ] WSL:
    Host+distro / remote CLI origin / bridge startup / chat-run / Forge review / artifacts-diffs / extension reload / Workspace Trust:
  - [ ] Remote-SSH:
    Local+remote hosts / remote CLI origin / bridge startup / chat-run / Forge review / artifacts-diffs / extension reload / OAuth limit:
  - [ ] Untrusted workspace:
    Workspace-local executable/settings ignored / secrets never forwarded to unsafe origins /
    signed managed-runtime readonly inspection available where supported / file, shell, browser,
    credential, and Forge mutation paths trust-gated:
  - [ ] VS Code Extension Host report:

## Known Limitations Accepted

Confirm release notes and Marketplace copy do not overclaim these IDE v1 limits:

- [ ] Forge swarm is review-only (per-task Keep/Discard, never auto-merged), capability- and Workspace-Trust-gated, with cooperative checkpoint cancellation. Broad auto/fullaccess swarm execution and merge-to-base stay blocked.
- [ ] Broad Forge exec unavailable; IDE exposes selected-task review-mode execute and the review-only Run Swarm console only.
- [ ] Forge auto/fullaccess execution unavailable.
- [ ] Active runtime cancellation is cooperative checkpoint only; no hard interrupt is claimed.
- [ ] Native durable Forge recovery is exercised: bounded interrupted/failed rows, exact revision revalidation, explicit Resume confirmation, fresh zero-grant permission scope, Stop, usage, and review ownership all pass without silently reusing prior grants.
- [ ] MCP OAuth is supported on local extension hosts; Remote-SSH OAuth is unavailable until a URI-handler or device-code callback flow exists.
- [ ] Managed-browser agent tools and the native Browser Cockpit are top-level, agent-shared,
  public-only, and owner-scoped for normal starts. Start/Navigate/Click/Type are Workspace-Trust
  gated; previews use verified host-created URIs; export uses native Save As; confirmed close
  deletes ephemeral data; local/private model navigation and nested-subagent browser tools are not
  claimed. Direct IDE **Start local testing** is separately modal-confirmed,
  `public_loopback`-scoped, agent-isolated, and denies LAN/link-local; no legacy
  `allow_local_destinations` parameter is sent.
- [ ] `hooks.watch` unavailable.
- [ ] Raw trace unavailable; trace output is backend-redacted and bounded.
- [ ] Arbitrary terminal start and interactive terminal streaming unavailable.
- [ ] Setup/config terminal menus and inline API key setters unavailable.

## Blocker Review

- [ ] No known P0/P1 blockers.
- [ ] Known limitations are documented in README, CHANGELOG, release checklist, and release notes.
- [ ] Security review confirms no inline secrets, raw provider headers, or unredacted reports.
- [ ] Marketplace copy avoids stable/production availability claims unless stable release approval is complete.
- [ ] Windows managed runtimes are Authenticode-signed and verified; ECDSA manifest attestation alone is not accepted as an OS code signature.
- [ ] macOS managed runtimes and embedded libraries are Developer ID signed, notarized, and pass
      strict `codesign` plus `-R="notarized" --check-notarization` verification.
      Standalone Mach-O executables use Apple's online notarization ticket and are not claimed to
      support direct stapling; unsigned/unnotarized Darwin bundles block promotion.

## Rollback Plan

The Markdown record is not rollback evidence. Complete the closed
`vscode-production-signoff-and-rollback` schema-v2 JSON receipt and validate it against the exact
six-candidate directory. The protected evidence workflow retains and attests that file.

- [ ] Rollback owner is an accountable approver. The source-pinned single maintainer may fill all
      three roles; otherwise the approvers are distinct.
- [ ] Trigger conditions are a sorted subset of the validator's reviewed incident classes.
- [ ] Communication action is `github_release_and_security_advisory`.
- [ ] Remediation issue is an immutable `github.com/AlysisAi/AlysisCode/issues/<id>` URL.
- [ ] Marketplace action is `unpublish_affected_version`.
- [ ] CLI action is `restore_last_known_good_compatible_runtime`.
- [ ] Post-action verification contains all four required install/runtime/Marketplace/notice checks.
- [ ] `python scripts/qa/validate_vscode_production_signoff.py \
      vscode-alysis-production-signoff.json path/to/six-candidate-directory \
      --release-tag vX.Y.Z --source-sha <40-char-sha> --candidate-run-id <id>` passes.

## Approval

- Release manager:
- Engineering:
- Security/release governance:
