# VS Code Extension Release Checklist

This checklist separates generic component validation from a production candidate. The local
component gate must pass without real LLM/provider API keys, but its generic VSIX does not contain a
managed runtime and is never releasable. Only the target-specific managed-runtime workflow can
produce production candidates.

For the `0.3.x` beta line, select `channel: beta` in the signed candidate, installed-provider,
untrusted-workspace, evidence and promotion workflows. Use `marketplace-beta` in the production
signoff and `publish-beta` for promotion confirmation. The protected publishing environment is
`vscode-marketplace-beta`. All six target, signing, provider and remote acceptance gates apply.
See [the Marketplace launch handoff](vscode-marketplace-launch.md) for the current release location,
and remaining prerequisites.

## Generic Component Gate (Not Releasable)

Run from the repository root:

```bash
bash scripts/qa/check_vscode_extension_release_candidate.sh
```

The script fails fast and runs:

- `python scripts/qa/check_ide_cli_parity.py`
- `python -m pytest -q tests/test_ide_cli_parity_matrix.py tests/test_ide_protocol.py tests/test_ide_stdio_bridge.py tests/test_ide_protocol_contract.py tests/test_vscode_extension_dogfood.py`
- `npm ci` in `extensions/vscode-alysis`
- `npm test`
- `npm run lint`
- `npm audit --audit-level=high`
- the component packaging script, `package:dev-vsix -- --pre-release`
- dogfood according to `ALYSIS_RELEASE_DOGFOOD`

`ALYSIS_RELEASE_DOGFOOD=mock` is the default local dogfood mode. Use
`ALYSIS_RELEASE_DOGFOOD=extension-host` or `ALYSIS_RELEASE_DOGFOOD=require-cached` when the
local environment can run full Extension Host component dogfood. Use
`ALYSIS_RELEASE_DOGFOOD=skip` only for explicit local iteration; it is not release signoff.
For strict component validation, run:

```bash
ALYSIS_RELEASE_STRICT=1 \
ALYSIS_RELEASE_DOGFOOD=require-cached \
VSCODE_TEST_EXECUTABLE_PATH=/absolute/path/to/code \
bash scripts/qa/check_vscode_extension_release_candidate.sh
```

Strict mode rejects skipped/no-host dogfood, verifies the pre-release VSIX marker, rejects a
bundled managed manifest, and requires
`status: passed`, `component_valid: true`, `release_valid: false`, and
`extension_host: passed`. This proves the extension component, not a production package.

The GitHub `vscode-extension-component-candidate` workflow runs the same component checks and
uploads artifacts labelled not-for-release. Dogfood writes per-run `report.json`, `report.md`, and
`summary.json`; the gate writes `component_candidate_summary.json`. If VS Code cannot be
downloaded, set `VSCODE_TEST_EXECUTABLE_PATH` to a cached executable and use
`ALYSIS_RELEASE_DOGFOOD=require-cached`.

## Target-Specific Production Candidate Gate

Dispatch `.github/workflows/managed-cli-vsix-release.yml` from an existing immutable `vX.Y.Z` tag.
The workflow is artifact-only and cannot publish. For all six supported targets it must:

- build from committed `uv.lock`, reject vulnerable/deprecated/quarantined dependencies, and emit
  a CycloneDX dependency SBOM;
- apply and reverify Authenticode on Windows or Developer ID signing plus notarization and
  strict `codesign` and `-R="notarized" --check-notarization` verification on macOS;
  Linux uses the documented hash-and-provenance policy;
- create and verify GitHub provenance and SBOM attestations bound to the exact repository, workflow,
  tag, and source commit;
- assemble the six-target ECDSA manifest in a protected signing environment and bind release,
  compatibility, URL/hash/SBOM/native-signature/provenance/key-lifecycle fields;
- package one target-specific VSIX per platform, create and attest its final CycloneDX SBOM, then
  verify those final VSIX attestations; and
- install the VSIX into fresh user-data/extensions directories, activate the installed extension
  in `ExtensionMode.Production`, and verify managed runtime origin, no CLI override, native and
  release signatures, exact target/hash/version, package install, and live bridge health; and
- pass the committed five-pair cold/warm activation latency, CPU, RSS, and heap budgets. The
  candidate workflow retains the JSONL measurements and package jobs cannot start if the gate
  fails. CI and release workflows reject all local threshold overrides.

Only these target-specific artifacts can proceed to manual provider/OS signoff. Complete
`docs/vscode_extension_production_signoff_template.md` and retain the target production-dogfood
summary, exact VSIX SHA-256, final SBOM, and attestation verification evidence.
Every retained production-dogfood summary must use schema v3 and bind successful clean-install
runs on both the declared VS Code 1.90.0 minimum and the resolved current Stable build. A candidate
tested only on the historical minimum is not promotion-eligible.

## Manual Real-Provider Evidence

Generate the reviewer checklist and draft report template after packaging:

```bash
python scripts/qa/vscode_extension_dogfood.py --mode manual-real-provider \
  --vsix-path /absolute/path/to/vscode-alysis-<target>.vsix --package-mode require
```

The generated `report.json` with `status: manual_steps_written` is only a placeholder. It cannot be
used as beta evidence. The completed evidence must be a separate local JSON file with
`schema_name: completed-manual-report`, `schema_version: 5`, `mode: manual-real-provider`,
`status: completed`, immutable release/candidate identity, provider/version/VSIX metadata, exact
lowercase `vsix_sha256`, the exact production-dogfood SHA-256, VS Code/host/workspace metadata,
supported `platform_target`, `runtime_origin: managed`, `runtime_production: true`, managed artifact version
and CLI SHA-256, `native_signature_check: passed`, `release_signature_check: passed`,
`package_install_check: passed`, `bridge_health: passed`, and an empty `cli_path_override`,
`known_limitations_confirmed: true`, `no_p0_p1_blockers: true`,
`secret_leak_check: passed`, and a `completed_checks` object covering every generated
`required_checks` ID. Every check is a timestamped receipt with an immutable artifact SHA-256;
free-form event IDs and bare status strings are rejected. Release-defining checks are non-waivable and must be `passed`. Only
`run_swarm_capability_gate` and `mcp_oauth_local_lifecycle` may be `not_applicable`, and then only
with a concrete rationale explaining why the alternate capability or deployment shape was
unavailable. Blank, pending, skipped, unknown, or missing checks are rejected.

Validate completed evidence against the exact retained target candidate before signoff:

```bash
python scripts/qa/validate_vscode_manual_provider_report.py \
  path/to/completed-provider-report.json path/to/vscode-alysis-<target>.vsix \
  path/to/production-dogfood-<target>.json
```

The validator accepts only local report, VSIX, and production-dogfood paths. It opens the package and requires the exact
VSIX SHA-256, extension version, target runtime hash, managed artifact version, selected channel metadata,
packaged public key, and signed managed-runtime record to agree with the report and repository-pinned
release key. HTTPS evidence URLs and remote or
placeholder reports are rejected. It also rejects secret-looking values such as API keys, bearer
tokens, password assignments, and URL userinfo.

## Protected Marketplace Promotion

Promotion is manual and bound to the selected channel. Beta candidates or approval receipts cannot authorize stable publication. Configure `vscode-installed-live-provider-qa`,
`vscode-real-wsl-acceptance`, `vscode-real-remote-ssh-acceptance`,
`vscode-release-evidence`, `managed-cli-release-assets`, and the selected `vscode-marketplace-beta`
or `vscode-marketplace-stable` environment as protected
GitHub environments. Every environment must have a required reviewer, disable administrator
bypass, and allow only the custom deployment tag pattern `v*`; the workflows audit those
settings through the versioned GitHub API before entering a protected job. Self-review is allowed
only when the sole required reviewer is the source-pinned maintainer `Perdikis10` (GitHub user ID
`190930654`). That maintainer can fill all three signoff roles and must approve each actual gate.
Other reviewer configurations require self-review prevention and distinct signoff identities.
Keep `VSCE_PAT` scoped only to
the selected Marketplace environment.

Configure the protected `vscode-installed-live-provider-qa` environment with the API credential
only as secret `ALYSIS_LIVE_API_KEY`. Configure `ALYSIS_LIVE_QA_PROVIDER`,
`ALYSIS_LIVE_QA_MODEL`, `ALYSIS_LIVE_QA_BASE_URL`, and
`ALYSIS_LIVE_QA_APPROVED_ORIGIN` as environment variables, not secrets or repository-wide
values. The approved origin is mandatory, canonical HTTPS on the default port, and must exactly
match the reviewed origin of the base URL. Validation rejects userinfo, query/fragment data,
local/private/link-local resolution, and unapproved ports before consuming the key. Retained
evidence includes only the normalized origin. The workflow stores the key through VS Code
`SecretStorage`, clears it before the restart phase, proves profile reuse with a non-secret
persisted nonce, retains no response text, and fails if the exact credential remains in
runner-owned UTF-8 or UTF-16 files.

For DeepSeek QA, set these environment variables:

| Variable | Value |
| --- | --- |
| `ALYSIS_LIVE_QA_PROVIDER` | `deepseek` |
| `ALYSIS_LIVE_QA_MODEL` | `deepseek-v4-flash` |
| `ALYSIS_LIVE_QA_BASE_URL` | `https://api.deepseek.com` |
| `ALYSIS_LIVE_QA_APPROVED_ORIGIN` | `https://api.deepseek.com` |

Create a dedicated test key in the DeepSeek Platform and enter its value directly in the
environment secret. GitHub Actions supplies it to the test process; a GitHub secret cannot be
downloaded for local tests. Local interactive tests instead use **Alysis Code: Configure
Provider** to save the key in that test profile's VS Code SecretStorage.

The live-provider environment variables do not authorize their own credential destination. The
selected provider and approved origin must also match the immutable tag's
`.github/release-policy/vscode-live-provider-origins.json`; each schema-v2 report retains that
policy file's SHA-256. Changing providers or origins therefore requires a reviewed source change,
not only an environment-variable edit.

Attach one completed schema-v5 provider report per target plus the
completed schema-v3 environment-acceptance report to the existing `vX.Y.Z` GitHub release using
these exact names:

```text
vscode-alysis-win32-x64.manual-provider.json
vscode-alysis-win32-arm64.manual-provider.json
vscode-alysis-darwin-x64.manual-provider.json
vscode-alysis-darwin-arm64.manual-provider.json
vscode-alysis-linux-x64.manual-provider.json
vscode-alysis-linux-arm64.manual-provider.json
vscode-alysis-environment-acceptance.json
vscode-alysis-human-check-artifacts.zip
vscode-alysis-production-signoff.json
```

The exact-named ZIP must contain one non-empty, redacted artifact for every unique
`artifact_sha256` referenced by the six manual-provider reports and the environment report. Put
members only below `artifacts/`; do not include credentials, provider response bodies, raw prompts,
workspace secrets, symlinks, traversal paths, duplicate content, or unrelated files. Validate it
before upload with `python scripts/qa/validate_vscode_human_evidence_bundle.py
vscode-alysis-human-check-artifacts.zip path/to/reviewed-evidence-directory`.

First complete `.github/workflows/vscode-installed-live-provider-qa.yml` for all six targets,
`.github/workflows/vscode-extension-untrusted-workspace.yml` against the installed Linux target,
and `.github/workflows/vscode-remote-acceptance.yml` for both real WSL and Remote-SSH hosts. The
runner provisioning trust boundary, partial-rerun attempt binding, and cancellation cleanup
procedure are defined in [Real VS Code remote acceptance](vscode_remote_acceptance.md). The
untrusted-workspace workflow is reproducibly pinned to the minimum supported VS Code 1.90.0
compatibility build; it has no operator-selectable VS Code version input. Release evidence accepts
only a successful first-attempt (`run_attempt: 1`) managed candidate run; after any candidate rerun,
dispatch a fresh candidate workflow so earlier environment approvals cannot authorize later bytes.
Then dispatch
`.github/workflows/vscode-extension-evidence.yml` from that exact tag with the successful
candidate, installed-provider, untrusted-workspace, and real-remote run IDs; confirmation
`attest-release-evidence`, and the reviewed manual inventory digest printed by
`python scripts/qa/hash_vscode_release_evidence.py path/to/nine-file-evidence-directory`. Supply the
required `workflow_run_attempts` input as a closed JSON object with the exact positive
`installed_live_provider`, `untrusted_workspace`, `remote_acceptance_wsl`, and
`remote_acceptance_remote_ssh` attempts. An installed-provider retry must rerun all six matrix jobs
so one attempt contains the complete six-report set; a partial rerun fails evidence assembly
closed. Its
protected job validates the nine reviewed files, six installed real-provider reports, one actual
untrusted-workspace report, and two real-remote reports against the exact candidate/runtime bytes.
It verifies their original attestations, re-attests the validated bundle plus a candidate-bound
receipt, and uploads one immutable artifact. Then dispatch
`.github/workflows/vscode-extension-promote.yml` from the same tag with the exact candidate,
installed-provider, untrusted, remote, and evidence run IDs; the matching `channel` and
confirmation (`publish-beta` or `publish-stable`);
and a closed `workflow_run_attempts` JSON object adding the exact `evidence` attempt to the four
acceptance attempts used above. The first promotion job has no Marketplace secret or write
permission. It verifies every workflow identity and attestation, freezes all bytes in a promotion
receipt, and verifies exact SLSA and CycloneDX predicates. Protected jobs re-download and revalidate
the immutable inputs. Rerun all promotion jobs together after a failed attempt; protected jobs
will not accept a validation receipt from another attempt. Publication observes Marketplace state,
accepts already-published targets
only when their bytes match, publishes only missing targets, then polls and verifies all six
downloaded package bytes. It has no missing-repository/secret or evidence bypass. Retain
the attested `vscode-marketplace-verification` bundle: both public query responses and plans, the
promotion validation receipt, official candidate/evidence/promotion approval histories, the closed
approval-binding receipt, `vscode-marketplace-publication.json`, and the six VSIX files
downloaded back from Marketplace. The receipt binds the promotion run attempt and proves each
public SHA-256 equals the exact candidate SHA-256.

## Public Beta Multi-OS Smoke

Public beta signoff requires Windows, macOS, and Linux evidence before the release manager accepts
the candidate. Each OS smoke record must name the OS/version, VS Code version, CLI version, VSIX
path, workspace path, reviewer, timestamp, and whether the workspace was trusted.

Run the same smoke coverage on all three OSes:

- CLI discovery from `alysis.cliPath`, PATH fallback, and **Alysis Code: Locate Alysis Code CLI**.
- Bridge startup through `alysis ide-bridge health` and the extension stdio bridge.
- SecretStorage provider setup through **Alysis Code: Configure Provider**.
- Normal chat and `Alysis Code: Run Task` / `run.start`.
- `/image` and `/paste-image` path handling, including missing file, outside-workspace, and
  unsupported-type recovery when practical.
- Forge plan, Forge show/status, Forge Execute Preview, and review-mode Forge Execute.
- Artifact and diff opening from the Activity Bar or Cockpit.
- Extension reload/restart with persisted plan/session recovery.
- Workspace Trust behavior for readonly flows, mutating actions, Forge assets, and plan edits.
- MCP OAuth start/status/cancel/logout on a local extension host, including explicit HTTPS browser
  confirmation and verification that authorization codes and tokens never cross JSONL.
- Review-only Run Swarm plus bridge-restart recovery through the native recovery picker: verify
  bounded interrupted/failed rows, revision revalidation, explicit Resume confirmation, fresh
  zero-grant permission scope, durable usage, result polling, cancellation, and review ownership.
- Managed-browser agent-tool use against a public disposable page, including per-action host
  approvals, blocked local/private navigation, redacted previews, and exact owned-process cleanup.
- Direct IDE **Start local testing** against a disposable loopback server: modal confirmation,
  `public_loopback` status, agent isolation, successful loopback access, and denied LAN/link-local.

Do not collapse macOS and Linux into one line for public beta signoff; both must be smoked or the
release issue must explicitly record why the beta is blocked.

## Manual Smoke Tests

Run these in a disposable workspace with a current local `alysis` CLI:

- Locate CLI from the walkthrough or command palette and verify `alysis ide-bridge health`.
- Configure a provider through VS Code SecretStorage; do not store API keys in plaintext settings.
- Run Doctor and confirm stdout/stderr are redacted in Output, Timeline, and Diagnostics.
- Create a session in the Cockpit.
- Send one chat message with a low-risk prompt.
- Run `Alysis Code: Run Task` and verify the extension starts the structured `run.start` workflow
  rather than sending the instruction through `chat.send`.
- Change an active session with `/mode readonly` and `/mode review`.
- Run `/model-info` and verify provider/model metadata is redacted and contains no API keys,
  bearer tokens, or secret headers.
- Run `/subagent status`, then `/subagent on` and `/subagent off` in a trusted workspace; verify
  toggles affect subsequent live-session turns only and do not change Forge Execute subagent policy.
- Attach an image with `/image <path>` and verify workspace path validation.
- Attach an image with `/paste-image <path>` or the no-arg file-picker fallback and verify no image
  binary is serialized through JSONL.
- Run `/trace`, `/trace compact`, and `/trace events`; verify returned trace data is backend
  redacted, bounded, and reports `secret_values_included: false`. Run `/trace full` only after the
  explicit confirmation prompt.
- Run `/terminals` and `/terminals list`; if the active session has no terminal manager, verify the
  structured unavailable result. If it has managed background terminals, run `/terminals show <id>`
  and verify output is redacted/bounded. Run `/terminals kill <id>` and `/terminals clear <id>` only
  in a trusted disposable workspace with explicit confirmation.
- Run Forge Plan from the Cockpit or `/plan`.
- Run Forge show/status and Forge review for the active plan.
- Run `/forge plan state`, `/forge plan validate`, `/assistant show`, `/assistant <instruction>`,
  `/goal show`, `/goal <goal>`, `/task <task_id> show`, one safe `/task <task_id> status <status>`
  update, and `/plan regenerate focus <text>` or `/forge plan regenerate focus <text>`; verify
  show invocations are reported as read-only, update/regenerate invocations are reported as
  mutating, mutations require Workspace Trust, use optimistic revisions, and persist to the plan
  store.
- Run bare `/execute`, `/forge exec`, and `/forge execute` and verify they route only to Forge
  Execute Review. Run a broad execute argument such as `/forge exec auto` and verify the IDE v1
  unsupported warning.
- Run Forge Execute Preview and confirm it does not mutate files.
- Run Forge review-mode execute only after Preview is ready and explicit confirmation is accepted.
- Run the review-only Forge swarm console and verify per-task approvals, cooperative cancellation,
  Keep/Discard review semantics, and no direct merge-to-base. Interrupt a run by restarting the
  bridge and verify the durable job appears in the native recovery picker with its bounded revision,
  attempts, and usage. Resume it only after the modal confirmation; verify the host revalidates the
  exact revision, supplies a fresh zero-grant permission scope, does not reuse prior grants, and
  restores Stop plus review/apply/discard ownership.
- From a local extension host, run **MCP Login** for an OAuth-capable disposable server. Verify the
  authorization URL opens only after explicit confirmation, status polling is cancellable, logout
  fences late token writes, and no authorization code, token, verifier, or client secret appears in
  bridge messages, Output, Timeline, or Diagnostics. Confirm the action is unavailable on a
  Remote-SSH extension host.
- Ask the top-level IDE agent to inspect a public disposable page with the managed-browser tools.
  Verify start, navigate, click, type, and close each require a fresh host approval; query strings
  and typed text are omitted from approval previews; local/private destinations cannot be enabled
  by the model; nested subagents receive no browser tools; and closing the session terminates only
  the exact owned browser process tree. Confirm health advertises the validating egress proxy,
  numeric DNS pinning, persistent child-target interception, no direct network fallback, removed
  loopback bypass, and disabled non-proxied UDP. Exercise the proxy regression tests for mixed DNS
  answers, rebinding, redirects/subresources, popups/workers, proxy/DevTools self-access, byte/time
  bounds, and exact shutdown. In the native Browser Cockpit, verify the agent-shared session can be
  selected; Start/Navigate/Click/Type are Workspace-Trust gated; screenshot bytes are chunked,
  hash-verified, and shown only through a VS Code webview URI; Save screenshot opens the host Save
  As dialog; and Close and delete removes the exact browser plus its ephemeral preview. Then use
  **Start local testing** and verify a fresh modal confirmation is required, the request contains
  `network_scope: "public_loopback"` plus `confirm: true` and never
  `allow_local_destinations`, the resulting session is unavailable to agent tools, loopback works,
  and LAN/link-local destinations remain denied.
- Open artifacts and diffs from the Activity Bar and Cockpit.
- Refresh Manage tree domains for tools, skills, hooks, MCP, conventions, and extension packages.
- Verify Manage context actions appear only when capability, state, and Workspace Trust allow them.
- Clear provider credentials and verify missing-provider recovery routes to Configure Provider /
  SecretStorage without exposing secret values.
- Point `alysis.cliPath` at a broken or old CLI fixture and verify setup/upgrade recovery appears
  before any SecretStorage API key is forwarded.

## Required Safety Facts

- Parity drift is a release blocker: every `package.json` command, hidden context command, tree
  command, slash command, and backend route must be represented in the parity matrix.
- The generated burn-down reports `docs/generated/ide_cli_parity_burndown.md` and
  `docs/generated/ide_forge_parity_burndown.md` must be current. `planned_for_protocol` entries
  must carry owner/milestone/next-step metadata, and `blocked_until_security_model` entries must
  carry an explicit security or lifecycle blocker.
- IDE protocol drift is a release blocker: backend action parameters must satisfy
  `docs/generated/ide_protocol_methods.json` and `docs/ide_protocol.md`.
- Activation, tree refresh, health refresh, and Manage refresh must not perform online
  `update.check` calls or remote extension/package searches.
- `update.check` is cached/offline by default; online mode is allowed only after explicit user
  action such as `/update online` or the command-palette online selection.
- Backend action prompts must not ask for API keys, bearer tokens, passwords, or secret-looking
  config values. Provider keys remain in SecretStorage through Configure Provider.
- Forge assets and review actions must remain capability-gated and Workspace-Trust-gated.
- Forge plan edits through `/assistant`, `/goal`, and `/task` must remain typed dual-mode
  read/update actions: show routes are audited as read-only, update routes are audited as mutating,
  mutations are Workspace-Trust gated, revision-safe, and free of terminal-output scraping.
- `/model-info` must not expose API keys, provider headers, or unredacted secret-bearing URLs.
- `/subagent` supports status/toggle only. Explicit subagent execution remains outside IDE v1.
- `/paste-image` must remain path/file-picker based; image binaries must not cross JSONL.
- `/trace` must remain backend-redacted and bounded; `full` trace requires explicit confirmation
  and no raw headers, environment variables, API keys, or secret-bearing prompts may be returned.
- `/terminals` must remain scoped to existing managed background terminals; it must not become
  arbitrary shell execution, terminal-output scraping, or unmanaged PTY streaming.
- Forge cancellation must be capability-driven: cooperative `cancellation_requested` is in-progress
  until backend terminal status, and unsupported or non-cancellable work must be displayed honestly
  rather than as success.
- MCP login must be advertised only when the complete start/status/cancel lifecycle and OAuth
  security capability are present. The extension may open only the returned HTTPS authorization
  URL after explicit user confirmation, must poll by opaque flow id, and must never carry an
  authorization code, token, verifier, or client secret through JSONL. The current IPv4-loopback
  callback is unavailable on Remote-SSH extension hosts.
- Managed-browser agent tools must use the IDE-owned service. Mutations require a fresh one-time
  host approval even in auto/fullaccess modes; the model cannot enable local/private destinations,
  choose an executable/profile path, delete artifacts, or expose browser tools to nested subagents.
- Direct IDE local testing is a separate actor boundary: it requires Workspace Trust and a fresh
  modal confirmation, sends only `network_scope: "public_loopback"` with `confirm: true`, remains
  agent-inaccessible, and must deny LAN/link-local. The extension must never send the legacy broad
  `allow_local_destinations` flag.
- `hooks.watch` must not be advertised until it has a typed subscription lifecycle, bounded
  redacted event buffers, dropped-event accounting, watcher cancellation, and stop cleanup.
- `session.cancel` and `forge.cancel` must not mark running jobs cancelled unless the backend
  reaches a cooperative checkpoint or pending approval boundary and reconciles job state.
- Forge swarm in the IDE is the review-only Run Swarm console (per-task Keep/Discard, never
  auto-merged), capability- and Workspace-Trust-gated, with cooperative checkpoint cancellation and
  non-modal task-attributed approvals replacing --yes. Broad auto/fullaccess execution and direct
  merge-to-base must remain unavailable in the IDE.
- Durable swarm list/resume must require the full `features.resumable_swarm` contract, a live
  session/workspace binding, Workspace Trust for resume, optimistic revision checks, and a freshly
  supplied permission fingerprint. The extension must not silently restart work after bridge
  recovery; the native picker must require an explicit, revision-revalidated Resume confirmation.
- Release notes must keep the known IDE v1 limitations explicit: Forge swarm is review-only
  (per-task Keep/Discard, never auto-merged) and capability-gated; broad Forge exec and
  auto/fullaccess execution are unavailable; cancellation is cooperative checkpoint only with no
  hard interrupt; MCP OAuth is unavailable on
  Remote-SSH extension hosts; agent-controlled Managed Browser access is public-only and
  unavailable to nested subagents, while direct IDE local testing is loopback-only; `hooks.watch`,
  raw trace, arbitrary terminal start, interactive terminal streaming, setup/config terminal
  menus, and inline API key setters are unavailable.

## External Release Blockers

- Windows managed-runtime executables require Authenticode signing with an organization-controlled
  code-signing identity and release-time signature verification. The ECDSA P-256 managed-runtime
  manifest authenticates release artifacts but is not an Authenticode signature.
- macOS managed-runtime executables require an organization-controlled Developer ID Application
  identity, Apple notarization credentials, notarization, and successful strict `codesign`
  plus `-R="notarized" --check-notarization`
  verification. Standalone Mach-O executables use Apple's online ticket and are not claimed to
  support direct stapling. Do not describe Darwin bundles as notarized until CI verifies them.
- The managed-CLI/VSIX assembly workflow is manual and artifact-only while protected external
  signing identities and approvals are not configured. Repository contents remain read-only; build
  jobs may write only artifact
  attestations/metadata. Locked dependencies, exact PyInstaller/hook versions, six CycloneDX SBOMs,
  Sigstore provenance, protected-environment key custody, and approved fingerprint verification are
  mandatory under `docs/managed-cli-signing-operations.md`. It must not regain an automatic tag
  trigger or release-publication job until Authenticode and Developer ID/notarization verification
  verification is enforced.
- Promotion evidence must come from actual Windows, macOS, Linux, WSL, Remote-SSH, and untrusted
  workspace runs. A local Windows mock Extension Host report does not satisfy the other rows.

## Non-Goals In IDE v1

- Auto/fullaccess execution is unavailable in the VS Code extension.
- Forge swarm is review-only; durable Resume is explicit, revision-revalidated, and never reuses
  prior permission grants.
- MCP OAuth is supported on a local extension host; Remote-SSH callback handling remains
  unavailable until a VS Code URI-handler or device-code flow exists.
- Managed-browser built-in agent tools and the native Browser Cockpit share the top-level IDE
  `public` session, but local/private model navigation and nested-subagent browser tools are
  unavailable. Direct IDE `public_loopback` testing is explicitly confirmed, agent-isolated, and
  never broad LAN/private access.
- `hooks.watch` is unavailable as an IDE request/response workflow.
- Raw unredacted `/trace` disclosure is unavailable.
- `/terminals start`, arbitrary shell execution, and interactive terminal streaming are unavailable
  until a managed IDE terminal subscription lifecycle exists.
- Explicit subagent execution remains unavailable until an IDE approval/lifecycle model exists.
