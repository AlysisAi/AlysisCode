# VS Code Extension Release Checklist

- [ ] Release channel is selected before packaging:
      internal VSIX dogfood, public beta / Marketplace pre-release, or stable Marketplace release.
- [ ] Public beta uses extension manifest version `0.1.x` with `vsce --pre-release`; do not publish
      `0.0.1` as a serious beta, and do not use semver prerelease suffixes in `package.json`.
- [ ] Release tag naming is explicit: `vscode-alysis-v0.1.1-rc.N` for release-candidate
      artifacts and `vscode-alysis-v0.1.1-beta.N` only after beta signoff.
- [ ] Beta candidates are packaged/published only with the VS Code Marketplace pre-release channel;
      `0.1.1` is not published as a stable Marketplace release.
- [ ] Full Python test suite passes.
- [ ] Windows/default-console encoding tests pass:
      `python -m pytest -q tests/test_surface_console_encoding.py`.
- [ ] Forge execution suites pass together and individually:
      `tests/test_forge_exec.py`, `tests/test_forge_swarm.py`, and `tests/test_forge_review.py`.
- [ ] CLI-to-extension backend parity validation passes:
      `python scripts/qa/check_ide_cli_parity.py`.
- [ ] Targeted IDE bridge release-candidate tests pass:
      `python -m pytest -q tests/test_ide_cli_parity_matrix.py tests/test_ide_protocol.py tests/test_ide_stdio_bridge.py tests/test_ide_protocol_contract.py`.
- [ ] Structured management backend methods are reviewed for `mutates`/`trust_required`
      capabilities, Workspace Trust gating, and absence of raw secret params or result values.
- [ ] No passive network calls are introduced by activation, status refresh, Forge recovery, or
      diagnostics flows; `update.check` uses network only after explicit user selection.
- [ ] Manifest activation remains lazy: `onStartupFinished` is absent, every contributed command is
      registered, and command/view/chat-participant entry points activate Alysis Code on demand.
- [ ] Virtual workspaces are rejected explicitly; remote extension hosts are smoked only where the
      workspace and executable Alysis Code CLI are both local to that host.
- [ ] `alysis.cliPath`, `alysis.baseUrl` and `alysis.extraCaCerts` remain `machine`-scoped
      (`test/commands.test.ts` asserts this): host-specific paths do not roam through Settings
      Sync, and a repository's `.vscode/settings.json` can nominate neither the executable that
      receives forwarded credentials nor the host they are sent to.
- [ ] No inline secrets are accepted in IDE protocol params, VS Code commands, slash routes, logs,
      output channels, or packaged diagnostics. Provider credentials stay in SecretStorage.
- [ ] IDE management hardening is reviewed: `session.show` and `conventions.render` are
      redacted/bounded, `update.check` defaults to cached/local status, and skill/extension install
      sources reject traversal, URL userinfo, unsupported schemes, and unreviewed remote/package code.
- [ ] Forge backend hardening is reviewed: `active_job` is running-only with terminal jobs visible
      as `last_job`, Forge Execute documents `no_log`/`max_steps`/subagent policy, Forge review and
      assets are capability/trust gated, and active cancellation is presented only as cooperative
      checkpoint cancellation when capability-supported.
- [ ] Unsafe Forge modes remain absent or blocked: review-only `forge.swarm` never auto-merges or
      writes the base worktree; auto/fullaccess IDE execution, direct merge-to-base, hard-interrupt
      cancellation claims, and terminal-output scraping remain unavailable.
- [ ] VS Code extension verification passes: `npm ci`, `npm run compile`, `npm run lint`,
      `npm test`, `npm audit --audit-level=high`, and `npm run package`.
- [ ] Packaging is strict: `npm run package` delegates to `npm run package:vsix`, no package script
      uses `--allow-missing-repository`, and `npm run package:pre-release` is used for a public
      beta VSIX candidate.
- [ ] The packaged VSIX contains the single bundled entry point `dist/extension.js`, sidebar
      webview assets, and icons, while excluding the unbundled `out/` tree, source, tests,
      `node_modules`, `.vscode-test`, source maps, lockfiles, and tsconfigs.
- [ ] Production managed runtimes are Authenticode-signed on Windows and Developer ID signed and
      notarized on macOS; strict `codesign` and `-R="notarized" --check-notarization` pass.
      Standalone Mach-O executables use Apple's
      online notarization ticket and are not described as directly stapled. ECDSA manifest
      attestation alone is not an OS signature.
- [ ] Managed-runtime dependencies resolve only from committed `uv.lock`; exact PyInstaller/hook
      versions, six CycloneDX SBOMs, and GitHub/Sigstore build attestations are present and verified.
- [ ] `managed-cli-signing` protected-environment reviewers, ref restrictions, private-key custody,
      approved public-key fingerprint, annual/incident rotation, and emergency revocation satisfy
      `docs/managed-cli-signing-operations.md`.
- [ ] Every protected release environment has at least one required reviewer, prevents self-review,
      and contains exactly the custom `v*` deployment tag rule; the versioned GitHub environment
      policy validator passes before any protected job starts.
- [ ] `vscode-installed-live-provider-qa` contains only environment-scoped
      `ALYSIS_LIVE_API_KEY` (secret) and `ALYSIS_LIVE_QA_PROVIDER`,
      `ALYSIS_LIVE_QA_MODEL`, `ALYSIS_LIVE_QA_BASE_URL`, and
      `ALYSIS_LIVE_QA_APPROVED_ORIGIN` (variables); no repository or organization copy can
      shadow them. The approved origin is canonical public default-port HTTPS, matches the reviewed
      base-URL origin exactly, and the six reports retain that normalized origin plus the persisted
      profile-reuse and origin-policy receipts.
- [ ] `npm run prepublish:check` passes before any Marketplace upload is considered.
- [ ] Extension Host tests pass in CI with `npm run test:integration` against both the declared
      minimum VS Code (`1.90.0`) and the current Stable channel. Cached/offline runs provide a
      separate executable path for each compatibility target.
- [ ] Extension Host tests use the deterministic mock bridge to cover `/help`, `/plan` success and
      failure, `/doctor` failure, broken health chips, `/execute preview`, `/execute plan`
      review-mode events, approval cards, native diff open, artifact open, unknown slash command
      fail-closed behavior, and bridge exit reporting. The mock bridge advertises async
      `forge.plan.start`/`forge.plan.result`, so the integration guard must cover Forge Plan →
      Execute Review → native diff plus the forced single-card failure path without a detached
      `this`/TypeError regression. Set `VSCODE_TEST_EXECUTABLE_PATH` in cached or offline CI instead
      of skipping the gate.
- [ ] `Alysis Code: Run Task` is smoked against the typed `run.start` workflow; it must not route the
      instruction through `chat.send`.
- [ ] Local generic component gate passes with
      `bash scripts/qa/check_vscode_extension_release_candidate.sh`. It defaults to
      `ALYSIS_RELEASE_DOGFOOD=mock`, which runs skip-host mock dogfood after packaging; use
      `ALYSIS_RELEASE_DOGFOOD=extension-host` or `require-cached` for full component dogfood.
      Strict component validation uses `ALYSIS_RELEASE_STRICT=1`, which rejects skipped/no-host
      dogfood, verifies the pre-release marker, and requires a component-valid summary. This generic
      VSIX has no managed runtime and is never a production release artifact.
- [ ] Local smoke dogfood may pass with
      `python scripts/qa/vscode_extension_dogfood.py --mode mock --extension-host skip`, but that
      report is marked not release-valid.
- [ ] Component Extension Host dogfood passes after the generic VSIX is packaged, using either
      `python scripts/qa/vscode_extension_dogfood.py --mode mock --package-mode require --extension-host run`
      when VS Code download is allowed, or
      `VSCODE_TEST_EXECUTABLE_PATH=/absolute/path/to/code python scripts/qa/vscode_extension_dogfood.py --mode mock --package-mode require --extension-host require-cached`
      in cached/offline CI.
- [ ] Component dogfood explicitly shows `component_valid: true`, `release_valid: false`,
      `status: passed`, `extension_host: passed`, `extension_under_test: packaged_vsix`, a non-empty
      `vsix` path, and a passing `extension_host_preflight` entry. It cannot be used for public beta.
- [ ] `.github/workflows/managed-cli-vsix-release.yml` succeeds from the exact immutable tag for
      all six targets. Each target-specific VSIX contains exactly one signed managed runtime, has a
      final CycloneDX SBOM plus GitHub provenance/SBOM attestations, and passes an isolated clean
      install in `ExtensionMode.Production` twice against the same profile, with stable managed
      runtime identity and bridge-health evidence across the Extension Host restart.
- [ ] The completed schema-v3 `vscode-alysis-environment-acceptance.json` contains the exact
      Windows, Linux, macOS, WSL, Remote-SSH, and untrusted-workspace rows with concrete VS Code
      commit, host/remote/trust/workspace metadata and timestamped structured receipts.
- [ ] `.github/workflows/vscode-extension-untrusted-workspace.yml` passes against the exact
      Linux candidate on its non-configurable VS Code 1.90.0 compatibility build and retains the
      candidate-bound, attested actual-untrusted report.
- [ ] `.github/workflows/vscode-extension-evidence.yml` succeeds from the exact tag and candidate
      run after protected `vscode-release-evidence` review; its required inventory SHA-256 matches
      `python scripts/qa/hash_vscode_release_evidence.py <seven-report-directory>`, it validates all
      seven manual reports plus all six installed-provider, actual-untrusted, real WSL, and real
      Remote-SSH reports, and retains the attested immutable evidence artifact and candidate-bound
      receipt. Its closed `workflow_run_attempts` JSON binds the installed-provider,
      untrusted-workspace, WSL, and Remote-SSH attempts. Rerun all six installed-provider matrix
      jobs together; a partial rerun cannot supply the exact six-report attempt and fails closed.
- [ ] Real-remote runner provisioning, prerequisite trust, partial-rerun selection, and cancellation
      cleanup follow [`docs/vscode_remote_acceptance.md`](../../docs/vscode_remote_acceptance.md).
- [ ] `.github/workflows/vscode-extension-promote.yml` is dispatched with the exact successful
      candidate, installed-provider, untrusted, remote, and evidence run IDs plus a closed
      `workflow_run_attempts` JSON object containing the exact installed-provider,
      untrusted-workspace, WSL, Remote-SSH, and protected-evidence attempts. Protected runtime-asset
      and Marketplace jobs revalidate the frozen same-attempt receipt, refuse conflicting existing
      bytes, and retain the observed six-target Marketplace publication receipt. Promotion retries
      rerun all jobs together and never reuse another attempt's receipt.
- [ ] `docs/vscode_extension_production_signoff_template.md` is completed with automated checks,
      target production-dogfood summary path, manual real-provider report path, OS smoke evidence,
      accepted known limitations, no P0/P1 blockers, and rollback plan.
- [ ] After the exact target candidate exists, manual real-provider, no-P0/P1, known-limitations,
      multi-OS, and rollback evidence is collected against that retained VSIX hash. It is not
      accepted as a pre-build input to the generic component workflow.
- [ ] Completed manual real-provider evidence passes
      `python scripts/qa/validate_vscode_manual_provider_report.py <report.json> <target.vsix> <production-dogfood.json>`.
      The validator must bind the evidence to the exact signed candidate bytes. The generated
      `manual_steps_written` report is only a checklist placeholder and cannot be used for beta
      signoff. Completed evidence must use `schema_name: completed-manual-report`,
      `schema_version: 5`, bind immutable release/candidate/host identity and the exact
      VSIX/production-dogfood/platform/managed-runtime hashes, confirm native and release
      signatures, package install, and bridge health, leave `cli_path_override` empty, and include
      a timestamped structured receipt for every current `required_checks` ID.
- [ ] Production candidate output is a target-specific pre-release VSIX from
      `managed-cli-vsix-release.yml`, not the generic component workflow, and its manifest contains
      `Microsoft.VisualStudio.Code.PreRelease`.
- [ ] Real Python bridge smoke test passes without API keys or model calls.
- [ ] VSIX package is generated reproducibly with `npm run package`.
- [ ] Manual real-provider dogfood is prepared with
      `python scripts/qa/vscode_extension_dogfood.py --mode manual-real-provider --vsix-path
      /absolute/path/to/vscode-alysis-<target>.vsix --package-mode require`.
- [ ] Manual smoke evidence covers both a mock provider/no-credential run and a real provider run
      in a disposable repository, including `/plan`, `/execute preview`, review-mode `/execute plan`,
      Forge assets, Forge review, `/assistant`, `/goal`, `/task`, `/model-info`, `/subagent status`,
      `/paste-image` path validation, `/trace` redaction/bounding, `/terminals` managed-terminal
      unavailable or bounded-output behavior, unsupported broad Forge execute/cancellation
      messaging, and secret redaction.
- [ ] Manual live QA re-test checklist is completed for the candidate:
      first-run Manage tree shows supported domains without false "needs newer Alysis Code CLI";
      normal chat produces a single assistant reply; Greek/UTF-8 input and output do not crash or
      corrupt the Cockpit; Forge Plan → Execute Review → native diff → approvals works; mode and
      model chips show the live mode/model/profile and honest gates; a forced Forge Plan failure
      renders one expandable error card, not duplicate sibling cards.
- [ ] VSIX is installed with `--force` into fresh `--user-data-dir` and `--extensions-dir`
      directories (or via "Install from VSIX..." in an equivalent clean profile), and
      `code --list-extensions --show-versions` reports the expected
      `alysisai.vscode-alysis@<version>` before UI smoke begins.
- [ ] Windows smoke is completed with OS/version, VS Code version, CLI version, VSIX path,
      reviewer/timestamp, workspace trust state, CLI discovery, bridge startup, SecretStorage,
      chat/run, image path handling, Forge plan/execute, artifacts/diffs, extension reload, and
      Workspace Trust behavior.
- [ ] macOS smoke is completed with OS/version, VS Code version, CLI version, VSIX path,
      reviewer/timestamp, workspace trust state, CLI discovery, bridge startup, SecretStorage,
      chat/run, image path handling, Forge plan/execute, artifacts/diffs, extension reload, and
      Workspace Trust behavior.
- [ ] Linux smoke is completed with OS/version, VS Code version, CLI version, VSIX path,
      reviewer/timestamp, workspace trust state, CLI discovery, bridge startup, SecretStorage,
      chat/run, image path handling, Forge plan/execute, artifacts/diffs, extension reload, and
      Workspace Trust behavior.
- [ ] WSL smoke is completed with distro/version, Windows host, VS Code version, remote CLI origin,
      bridge lifecycle, chat/run, Forge review, artifacts, reload, and Workspace Trust behavior.
- [ ] Remote-SSH smoke is completed with local/remote OS and VS Code versions, remote CLI origin,
      bridge lifecycle, chat/run, Forge review, artifacts, reload, and the documented OAuth limit.
- [ ] Untrusted-workspace smoke confirms readonly inspection remains available while process,
      credential, browser mutation, Forge execution, and file mutation paths fail closed.
- [ ] Disposable fixture repository from the dogfood report is opened in VS Code.
- [ ] Provider API key is configured through `Alysis Code: Configure Provider` and SecretStorage, not
      through settings or plaintext files.
- [ ] `/doctor` is run from the Cockpit and its result appears in Timeline, Diagnostics, and Output.
- [ ] Normal chat and `Alysis Code: Run Task` are tested; Run Task must use structured `run.start`
      rather than `chat.send` or terminal output.
- [ ] `/model-info` returns redacted model/provider metadata without API keys, bearer tokens, or
      secret headers.
- [ ] `/subagent status`, `/subagent on`, and `/subagent off` are tested in a live session; toggles
      require Workspace Trust and do not alter Forge Execute subagent policy.
- [ ] `/paste-image <path>` and the no-arg file-picker fallback attach only workspace-scoped regular
      image files; no image binary appears in JSONL logs or dogfood reports.
- [ ] `/trace`, `/trace compact`, `/trace events`, and confirmed `/trace full` return
      backend-redacted bounded data with `secret_values_included: false`.
- [ ] `/terminals` and `/terminals list` either return a structured unavailable result when no
      manager exists or list existing managed background terminals. `/terminals show <id>` is
      redacted/bounded, and `/terminals kill <id>` plus `/terminals clear <id>` are tested only in a
      trusted disposable workspace with explicit confirmation.
- [ ] `/plan <task>` is run for the fixture clamp-function task.
- [ ] Forge show/status is tested for the active plan.
- [ ] `/assistant show`, `/assistant <instruction>`, `/goal show`, `/goal <goal>`,
      `/task <task_id> show`, one safe `/task <task_id> status <status>` update, and
      `/plan regenerate focus <text>` or `/forge plan regenerate focus <text>` are tested against
      the active Forge plan. Show invocations are reported as read-only, update/regenerate
      invocations are reported as mutating, mutating edits require Workspace Trust, use optimistic
      revisions, and persist to plan state.
- [ ] Bare `/execute`, `/forge exec`, and `/forge execute` route to Forge Execute Review only; broad
      execute arguments such as auto/fullaccess return the IDE v1 unsupported warning.
- [ ] Timeline shows command started, validation, executable-origin check, bridge start/reuse,
      planning started, and planning completed or failed.
- [ ] Forge Plan cards and task detail show task status, objective, file scope, acceptance criteria,
      dependencies, warnings, and verification commands.
- [ ] `/execute preview` renders readiness, selected tasks, missing prerequisites, sandbox status,
      required approvals, known risks, and verification commands.
- [ ] `/execute plan` starts with Preview and requires explicit Execute Review confirmation.
- [ ] Approval prompt/card shows kind, reason, command/preview, files, scope, Allow once, scoped
      Allow for session when supported, and Deny.
- [ ] Verification gate and review decision events appear in the Timeline during execution.
- [ ] Native diff open works from the Cockpit and shows only backend-scoped disposable-repo changes.
- [ ] Artifacts open from the Cockpit and are grouped by session/job/plan.
- [ ] Output, Timeline, Diagnostics, generated reports, and packaged artifacts contain no secrets.
- [ ] Status bar, Bridge chip, CLI Origin, CLI Health, Sandbox chip, and Diagnostics agree.
- [ ] Active Forge cancellation is displayed honestly as cooperative checkpoint cancellation: request
      state is shown as `cancellation_requested`, repeated cancel clicks are suppressed, and terminal
      `cancelled` is not shown until backend job status reconciles.
- [ ] MCP OAuth login on a local extension host exercises start/status/cancel/logout, state + S256
      PKCE, owned loopback callback, bounded polling, encrypted persistence, and shutdown fencing;
      no authorization code, raw token, or secret header crosses JSONL. Remote-SSH remains hidden or
      explicitly unavailable until a remote-safe callback/device-code flow exists.
- [ ] `hooks.watch` and `hooks.watch.*` lifecycle placeholders are not advertised or passively
      started; hooks inspection remains bounded/read-only unless a trusted mutation is explicit.
- [ ] `session.cancel` and `forge.cancel` never mark a running job cancelled without backend
      cooperative interruption and cleanup/state reconciliation; no hard-interrupt claim appears in
      UI, docs, or Marketplace text.
- [ ] Forge swarm is exposed only as the review-only Run Swarm console (per-task Keep/Discard, never
      auto-merged), capability- and Workspace-Trust-gated, with cooperative checkpoint cancellation
      and non-modal task-attributed approvals replacing --yes. Broad auto/fullaccess execution and
      direct merge-to-base remain unavailable in the IDE.
- [ ] Real Forge Execute v1 review-mode smoke is run in the disposable repository.
- [ ] Public beta / Marketplace pre-release gate has no known P0/P1 blockers and known limitations
      are listed in README, changelog, and release notes.
- [ ] Broken or old CLI recovery UX shows setup/upgrade guidance without forwarding API keys.
- [ ] Missing API-key recovery UX is clear and does not expose secrets.
- [ ] Screenshot or video capture for release notes is created if needed, with secrets, private
      paths, customer code, and unsupported Marketplace claims removed.
- [ ] VSIX is uninstalled or the Extension Development Host profile is reset after testing.
- [ ] Workspace Trust behavior is manually verified.
- [ ] SecretStorage behavior is manually verified.
- [ ] Logs, output channels, webview text, and packaged artifacts contain no secrets.
- [ ] Marketplace metadata, icon, categories, keywords, gallery banner, and repository fields are reviewed.
- [ ] README, CHANGELOG, and SUPPORT are checked.
- [ ] `docs/generated/ide_cli_parity_matrix.json` and `docs/ide_cli_parity_matrix.md` are current
      for any changed CLI commands, IDE methods, package command registrations, command-palette
      visibility, hidden context-menu/tree commands, backend action routes, or slash commands.
- [ ] `docs/generated/ide_cli_parity_burndown.md` and
      `docs/generated/ide_forge_parity_burndown.md` are current and reviewed; Forge swarm and
      unsafe execution modes remain blocked or explicitly experimental-gated with lifecycle
      rationale.
- [ ] Forge Plan and Forge Execute Review are described accurately; no README, changelog, or
      Marketplace text claims full Forge Swarm, auto/fullaccess IDE execution, hard-interrupt
      cancellation, or Marketplace availability before those are actually shipped.
- [ ] Known IDE v1 limitations are copied into release notes: Forge swarm is review-only (per-task Keep/Discard, never auto-merged) and capability-gated; broad Forge exec and auto/fullaccess execution unavailable; cancellation is cooperative checkpoint
      only with no hard interrupt; MCP OAuth unavailable on Remote-SSH extension hosts; `hooks.watch`
      unavailable; raw trace unavailable; arbitrary terminal start and interactive terminal streaming
      unavailable; setup/config terminal menus and inline API key setters unavailable.
- [ ] Marketplace publisher ownership and `VSCE_PAT` secret setup are documented before any publish
      job is enabled.
- [ ] Rollback/unpublish plan is documented for public beta issues before publish.
- [ ] Marketplace publishing is still manual or explicitly approved; no workflow runs `vsce publish`
      automatically without secrets, maintainer approval, and release signoff.
