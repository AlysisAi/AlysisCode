# Changelog

## Unreleased

- Preserve Git rename and copy paths containing ` b/` directory components
  when diff headers require adjacent metadata to disambiguate the paths.
## 0.15.1 - 2026-10-15

- Keep the release qualification's simulated stalled provider connected until
  the test completes. Its previous five-second disconnect could report a
  provider failure before the managed deadline on Windows. Deadline and
  process-cleanup assertions remain enforced.
- Include all changes listed under 0.15.0 below. The 0.15.0 tag is preserved,
  but its release qualification failed before package publication.

## 0.15.0 - 2026-10-15

- Expand the terminal workspace with inline diffs, file mentions, prompt history,
  session navigation, integration inspection, planning, review, tool traces,
  structured questions, and clearer subagent progress.
- Add IDE command and skill discovery, explicit skill invocation, mid-turn
  steering, and opt-in reasoning summary events.
- Improve ChatGPT subscription registration, account selection, credential
  refresh, saved profiles, endpoint migration, and reviewed model discovery.
- Add GPT-6.1 Sol presets and model-specific reasoning controls. Preserve
  reasoning choices across settings reloads and separate safe reasoning summaries
  from final answers.
- Recover more reliably from output limits, interrupted streams, provider retry
  exhaustion, and cancellation. Improve caching and provider diagnostics.
- Add approval review and safer command classification, with fixes for Windows
  command execution, macOS image paste, sandbox fallback, and local listeners.
- Clarify hosted-account billing and capacity errors without mislabeling them as
  rejected API keys; direct model configuration guidance to `/config`.
- Remove `/objective` and explicit task-relation state. State the current goal in
  the conversation; steer an active turn when requirements change.
- Stamp builds consistently and persist an installation identifier across client
  sessions.
- Add optional PDF, Word, spreadsheet, and presentation tools through the
  `documents` extra, with path-scoped input and output acceptance checks.
- Add host integration contracts for browser and computer actions, GitHub,
  Google services, open panels, task budgets, and Forge verification.
- Route `forge plan` and `forge run` through the shared host planner and approval
  flow. Machine planning accepts an explicit goal and intent.
- Add account usage/status, reviewed feedback delivery, corporate CA support,
  and safer managed-session persistence on Windows.
- Add `profile check`, subagent settings, bundled Alysis documentation guidance,
  and persona restoration when IDE sessions start or resume.
- Fix Windows `.git/info/exclude` line endings and VS Code provider setup,
  startup readiness, and package inventory checks.
- Align public links and release provenance checks with `AlysisAi/AlysisCode`.
- Keep hosted deployment URLs and project keys out of public source and packages.
  Hosted login, account status, and online feedback require runtime configuration
  through `ALYSIS_CLOUD_API_URL`; see [Reference](reference.md). Other configured
  providers remain available.
- Preserve focused setup actions during streamed sidebar updates and avoid
  repeatedly searching the transcript for static controls.
- Keep background update checks bound to the data directory that scheduled them.
- Repair sandbox dependency locks and server packaging, and update the bundled
  Go toolchain to the 1.26.9 security patch.
- Retire the `sylliptor` command and legacy branding aliases. Use `alysis` or
  `alysis-code`, canonical `ALYSIS_` environment variables, `.alysis` project
  data, and `alysis-plugin.toml` manifests. Back up and migrate legacy locations
  explicitly before upgrading; this release does not move them automatically.


## 0.14.9 — Claude Haiku 5.5 on Alysis Free — 2026-10-07

- Add Claude Haiku 5.5 to the Alysis Free model picker and direct Anthropic
  profiles, with native Messages routing, a 1M context window, prompt caching,
  and adaptive thinking. Choose low, medium, high, xhigh, max, or off; Auto uses
  the model's medium default. Pricing increases 5x above 100K total prompt tokens.
- Replace the retired Sonnet Free campaign entry with Haiku 5.5. Existing
  account credits and saved model preferences are retained.

## 0.14.8 — production VSIX verification

- Run final candidate evidence validation inside the frozen Python environment,
  where its cryptographic verification dependencies are installed.
- Update the VS Code test utility to 3.1.0 so current macOS releases resolve
  their executable from the application metadata, while older versions remain supported.
- Exercise both minimum and current Stable VS Code in extension CI before tagging.
- Retry interrupted or incomplete pinned Python downloads up to three times,
  requiring the original size and SHA-256 checks to pass before extraction.

Version 0.14.7 passed main CI and PyPI publication. Its six native runtimes and
signed manifest passed, but later VSIX verification exposed these harness issues.
Existing release tags and published distributions remain unchanged. Marketplace
promotion still requires the documented acceptance and signoff gates.

## 0.14.7 — VSIX dependency audit and sidebar rendering

- Update the VS Code extension's locked source-map-js dependency to 1.2.2,
  resolving GHSA-68fv-2mgg-jv7q and restoring the release audit gate.
- Avoid repainting unchanged permission controls and settings filters during
  conversation updates, reducing sidebar render cost on Intel macOS.
- Run extension CI on Intel macOS as well as Apple Silicon before tagging releases.
- Synchronize the queued-command overflow test with actual input receipt so
  loaded CI runners exercise the intended full-inbox behavior reliably.

This replacement preserves the published 0.14.6 source tag and distributions.
The VS Code 0.3.0 beta still requires candidate installation, acceptance, and
promotion gates before Marketplace publication.

## 0.14.6 — final VSIX attestation and startup validation

- Give each final VSIX dependency report an RFC 4122 document ID accepted by
  GitHub's attestation parser. Derive it from the exact package bytes so promotion
  can reproduce the signed report without changing its identity.
- Wait for startup runtime validation before reporting installed runtime evidence,
  so a fresh installation does not report the bundled CLI as temporarily unavailable.

All six 0.14.5 native runtimes and their assembled manifest passed signing, but
final VSIX attestation failed before installation testing. This replacement
preserves the existing tags; Marketplace pre-release 0.3.0 still requires the
documented installation, acceptance, and promotion gates.

## 0.14.5 — native managed-runtime packaging

- Pin the six bundled runtimes to checksum-verified CPython 3.12.15 distributions,
  including their observed OpenSSL and Expat versions in each dependency SBOM.
- Build Linux executables with shared-library-capable Python on the existing
  glibc 2.28 baseline, and build tested, static OpenSSL 3.5.9 for Windows ARM64
  and Intel macOS cryptography.
- Sign embedded macOS libraries during packaging and verify notarization using
  Apple's documented check for standalone code. Preserve Windows API paths in Git Bash.
- Add an unsigned native build and executable smoke check for all six targets.

This source version prepares a replacement candidate after the protected `v0.14.4`
native build failed. Marketplace pre-release 0.3.0 remains subject to the documented
signing, installation, acceptance, and promotion gates.

## 0.14.4 — IDE integrations and release tooling

- Add the VS Code 0.3.0 integration with a shared sidebar, provider management, bounded editor
  context, reviewed changes, recovery checkpoints, and native host actions.
- Add shared UI transport and a JetBrains development preview using the same Python IDE bridge.
- Isolate provider credentials, handle physical workspace paths, and keep unit tests portable.
- Add six-target managed-runtime packaging, Azure OIDC Windows signing verification, Apple
  notarization/provenance contracts, and protected evidence and Marketplace promotion workflows.
- Update pinned sandbox build inputs; fresh image scans remain required.

This is a CLI/source release. Production signed VSIX packages and Marketplace publication remain
pending the [documented signing and acceptance gates](vscode-marketplace-launch.md).

Notable user-facing changes to Alysis Code are recorded here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) while the public API remains pre-1.0.

## [Unreleased]

## [0.14.3] - 2026-09-29

### Added

- Add Claude Sonnet 5.5 to the Alysis account model picker, using native Messages
  transport, streaming, signed reasoning and tool continuation, and the shared
  hosted credit allowance.

### Changed

- Default hosted accounts to automatic prompt caching. Migrate legacy Manual
  settings only when their other cache settings match the old defaults; preserve
  Off, customized settings, and subsequent explicit choices.
- Use five-minute cache controls for hosted Sonnet and provider-managed caching
  for hosted DeepSeek, GLM, and Luna. See the
  [caching guide](hosted-prompt-caching.md) for migration details and controls.

### Fixed

- Resolve cache previews and diagnostics using each model's actual protocol,
  preserve hosted credit-limit error details, and report Sonnet reasoning usage
  without counting it twice.
- Honor the configured HTTP MCP startup timeout for initialization acknowledgements.
  Report MCP startup errors once and exit cleanly without retrying through classic chat.

## [0.14.2] - 2026-09-28

### Fixed

- Show only supported reasoning efforts for hosted Luna and clear incompatible
  settings inherited from another model. Map saved or environment-provided
  `minimal` and `ultra` settings to `low` and `max`, preventing invalid-effort
  errors during the free-credit campaign.
- Preserve the hosted gateway's capacity retry guidance and account request-limit
  messages so users can distinguish temporary capacity from exhausted credits.
- Cancel working-tip link clicks when a fast mouse drag arrives before the next
  screen repaint.

## [0.14.1] - 2026-09-25

### Added

- Add GPT-6 Luna to the Alysis Code account model picker, with hosted Responses
  routing, configurable reasoning, and stateless function-tool continuation.
  Luna uses the account's shared credit allowance alongside the Flash models.

### Fixed

- Rebuild the correct chat and compactor transports when switching between hosted
  Luna and Flash models during a conversation, while preserving the session on a
  failed switch.
- Keep hosted web search as a function tool and show the gateway's capacity,
  request, and credit-limit reasons instead of a generic rate-limit message.

## [0.14.0] - 2026-09-24

### Added

- Add GPT-6 Sol and GPT-6 Luna to the OpenAI API and ChatGPT subscription model
  choices, with pricing, context limits, and reasoning settings. Subscription
  availability follows the live account catalog.
- Add Claude Opus 5.5 to Anthropic and OpenRouter, Grok 4.7 to xAI and OpenRouter,
  Mistral's hosted GLM 5.3, and current OpenRouter model routes.
- Show DeepSeek V4.1 Flash and GLM 5.3 Flash as hosted account model choices.
- Add `/objective` commands to inspect, replace, and amend the current task across
  retries and resumed sessions.
- Allow a configurable set of public package registries for `web_fetch` while
  retaining URL and network safety checks.

### Changed

- Show clearer model names in setup and move the request timeout control to
  Advanced settings.
- Improve subagent scheduling, task evidence, and verification reporting.

### Fixed

- Suppress unexecuted tool-call markup in replies, retry once with structured
  tool calls, and report a failure if the model repeats the malformed output.
- Require AnyIO 4.14.2 or newer in the HTTP runtime dependency set.
- Keep hosted account credentials on the selected gateway when an unsupported
  model is chosen, and preserve retired Xiaomi selections without silently
  redirecting them to a different provider's model.
- Omit unsupported temperature settings for GPT-6 reasoning requests and reject
  attempts to disable reasoning for always-thinking OpenRouter models.
- Improve session recovery and deadline handling for long running work.
- Verify durable service listener ownership on macOS without requiring root access.

## [0.13.8] - 2026-09-16

### Changed

- Remove legacy chat Plan Mode, its draft/approve flow, and its prompts and toolbar
  state. Use the Architect persona for planning in chat; Forge retains its plan views
  and editor.
- Replace `/mode` with `/permissions`. Permission selections and Shift+Tab apply to
  the next user message, with the pending choice visible beside the active permissions.
- Apply persona changes and approved persona-switch proposals only after the active
  turn finishes. Preserve pending permission selections when resuming a conversation.
- Show pending steering messages and queued follow-ups. Shift+Left recalls the newest
  queued message for editing. Interruptions preserve undelivered work and wait for the
  current worker to finish before another turn starts.
- Read large Git diffs in complete pages with a content identifier that detects
  changes between pages. The agent can select a path or inspect staged changes.
- Allow a background-process wait of up to 900 seconds, bounded by the remaining
  run budget and returning early when its requested condition is met.
- Explain how to correct rejected verification commands, including how to use the
  session's configured verification contract.

### Fixed

- Keep plain assistant replies in conversation history so follow-up requests retain
  the previous answer even when the provider returns no extra metadata.
- Redact credential fields inside nested JSON, tool arguments, feedback text, and
  support bundles. Omit data that exceeds the safe redaction depth and non-UTF-8
  diagnostic artifacts that cannot be sanitized reliably.
- Clean up incomplete support bundles after cancellation or I/O errors, support long
  Windows export paths, and open report drafts without disrupting terminal output.
- Inspect Git changes without running configured diff programs, text converters,
  filesystem monitors, or content filters. Worktree reads that require content
  filters or submodule helpers return guidance for an approved shell command;
  staged diffs and unaffected scoped paths remain available.
- Preserve hosted-account explanations for rolling usage limits, concurrent requests,
  reserved credits, and temporary routing or capacity problems.

## [0.13.7] - 2026-09-11

### Added

- Rotating usage tips above the chat input while the agent works, with clickable links.
  Tips hide during dialogs and stay out of conversation history.

### Changed

- The context footer shows remaining conversation space after startup prompts and tools.
  Provider measurements and calibrated estimates inform the gauge, while capacity warnings
  continue to use the full request budget.
- Ctrl+J is the multiline input shortcut; Enter submits. The Alt+Enter alias is removed.
- Updated public documentation and contributor guidance.

### Fixed

- Setup continuation hints stay visible above the footer on small terminals and long screens.
- Benchmark wheel builds preserve local build metadata and existing build artifacts, including
  when a build fails or is interrupted.

## [0.13.6.2] - 2026-09-08

### Fixed

- **Alysis Code account setup and configuration use subscription controls.**
  Setup identifies the Alysis Code endpoint without the retired MiMo label.
  Hosted profiles show Alysis Code subscription access and account sign-in,
  reconnect, and disconnect controls instead of an API-key editor.
- **Hosted model changes no longer ask for a base URL.** The managed endpoint
  stays in place through model selection and back navigation. Custom and BYOK
  providers keep their endpoint settings.
- **Switching to an Alysis Code account keeps the correct connection.** Signing
  in refreshes the full active profile before configuration is saved, preventing
  the previous provider's URL from overwriting the hosted endpoint. Login also
  switches back to the native agent from a selected delegated runtime. Account
  status reflects the Alysis login, including after logout when an unrelated
  provider key is still saved.

## [0.13.6.1] - 2026-09-08

### Fixed

- **V4.1 Flash beta on Alysis Code Pro.** The Pro model picker now includes
  `deepseek-v4.1-flash-expires-on-0910` and `deepseek-v4-flash-vision-exp`
  with descriptions. Newly discovered models are captioned "available on your
  Alysis Code Pro plan" instead of the retired MiMo "trial" wording.

## [0.13.6] - 2026-09-08

### Added

- **DeepSeek V4.1 Flash internal beta.** `deepseek-v4.1-flash-expires-on-0910`
  (announced 2026-09-08) is now on the DeepSeek preset menu. It is a
  new-architecture model with native multimodal input, served on the unchanged
  `https://api.deepseek.com` base URL under a temporary id that stops resolving
  on 2026-09-10; DeepSeek bills it at the `deepseek-v4-flash` rate and caps
  accounts at 20 concurrent requests. Registry metadata assumes the V4 Flash
  shape (1M context, 384K output, vision, thinking `low|high|max`) until
  DeepSeek publishes the GA id and limits. `deepseek-v4-pro` stays the default.

### Changed

- **DeepSeek footer labels follow vendor spelling.** Names now appear as
  `DeepSeek-V4-Pro` and `DeepSeek-V4.1-Flash`; dated beta ids omit their
  `-expires-on-<date>` suffix from the displayed label.

## [0.13.5] - 2026-09-07

### Added

- Ship eight bundled workflow skills: `address-pr-comments`, `code-review`,
  `commit`, `debug`, `fix-ci`, `release-notes`, `security-review`, and `skill-creator`.
- Select a relevant skill automatically with a bounded, reasoning-off model call;
  read its instructions before other task tools, with ordinary routing as fallback.
- Invoke skills with `$<name> [task]` at an idle chat prompt, with live completion
  and one-turn attachment. `/skill` remains supported.
- Add `bundled_skills_enabled` configuration, project and user override precedence,
  and model-in-loop bundled workflow evaluations with launch-readiness checks.

### Fixed

- Harden skill instruction boundaries, selection ordering, and per-turn cleanup.
- Preserve resolved skill settings in child agents and restore slash completion.
- Recognize silent and inline verification results and preserve authoritative
  verification evidence when supplemental checks run.
- Bound stalled OpenAI-compatible and Responses streams, including traffic that
  contains no meaningful progress.

## [0.13.4] - 2026-09-05

Model-catalog and TUI-access refresh.

### Added

- Added GPT-6 Astra as the default OpenAI model, with aliases, pricing, context-window,
  reasoning, OpenRouter, and ChatGPT-subscription metadata.
- Added newly available first-party and gateway routes for Claude Fable 5.1, Gemini 3.8
  Flash, Qwen 3.8, GLM-5.3, Kimi K3, Doubao Seed 2.1, DeepSeek V4 snapshots, Grok 4.6,
  and related hosted variants.

### Changed

- Refreshed every hosted preset's model picker, aliases, validation model, retirement
  mappings, pricing, context limits, and reasoning contracts against provider catalogs.
- Updated TUI model labels to preserve vendor naming and made profile pickers show each
  profile's model, host, active state, retired-model state, and duplicates.
- Migrated the Perplexity preset from the retiring Sonar Chat Completions endpoint to the
  Agent API, including hosted web search and saved-profile migration.
- Replaced the OpenAI economy and validation model with GPT-5.6 Luna and kept
  GPT-5.3 Codex limited to the Responses preset.

### Fixed

- OpenAI Chat Completions now uses `max_completion_tokens` for GPT-5 and newer models.
- Native Gemini tool and response schemas are projected onto Gemini's supported OpenAPI
  subset before requests are sent.

## [0.13.3] - 2026-08-28

### Fixed

- Restored the Ubuntu Python 3.11 and 3.12 CI jobs after the repository cleanup by aligning Ruff
  formatting, updating moved sandbox contract paths, preserving the security-documentation
  contract, making immediate shell-output coverage independent of thread scheduling, and allowing
  harmless floating-point clock precision in deadline coverage.

## [0.13.2] - 2026-08-28

Public launch and repository-layout refresh.

### Added

- A theme-adaptive README banner with dedicated light and dark artwork.
- `alysis-code` as the canonical executable, while retaining `alysis` as the short alias.

### Changed

- Simplified the public README around installation, core capabilities, modes, and documentation.
- Moved governance files under `.github/`, release and changelog documents under `docs/`, and
  sandbox and benchmark sources under `scripts/` for a cleaner repository root.
- Legacy Sylliptor environment and command compatibility now forwards silently while retaining
  diagnostic logging for support.

### Fixed

- Device-login approval URLs now remain pinned to the configured Alysis site instead of trusting
  a backend-provided origin.

## [0.13.1] - 2026-08-27

### Fixed

- Kept terminal input, selection, scrolling, paste hints, and theme behavior consistent across
  chat and configuration screens.
- Made intentional clean stops exit successfully while preserving non-zero exits for genuine
  failures and unknown stop reasons.
- Added bounded retries for connections interrupted during a response, with consistent deadline
  and diagnostic reporting.

## [0.13.0] - 2026-08-26

### Changed

- Renamed Sylliptor to Alysis Code. The package is now `alysis-code`, the command is `alysis`, and
  the Python module is `alysis_code`.
- Preserved the deprecated `sylliptor` command and migration support for existing configuration,
  credentials, plugins, and project directories. See the [migration guide](migration-alysis-code.md).
- Improved run-budget handling, deadline-aware shell waits, persistent services, edit diagnostics,
  sampling controls, and build provenance.

### Security

- Added redaction at log and telemetry write boundaries for credential-shaped values.

## [0.12.0] - 2026-08-18

### Added

- Added Code, Architect, Ask, and Debug personas.
- Added provider-neutral Terminal-Bench and Harbor adapters.
- Added deterministic release builds, dependency auditing, SBOM generation, and provenance
  attestations.

### Changed

- Improved evidence-based finalization, blocker reporting, provider authentication, model
  discovery, context management, and verification repair.
- Strengthened boundaries around credentials, protected paths, filesystem access, and sensitive
  tool output.

## [0.11.2] - 2026-07-27

### Added

- Added route arbitration, turn-contract enforcement, regression baselines, and evidence-backed
  completion checks.
- Added tracked process cleanup, workspace provisioning, incomplete-subagent containment, and new
  Moonshot/Kimi model presets.
- Added a master switch for web tools and search backends.

### Changed

- Refreshed the ChatGPT subscription model catalog and provider-reported final-answer handling.

## [0.11.1] - 2026-07-20

### Changed

- Redesigned the interactive configuration screen with grouped settings, clearer summaries, and
  improved provider and model selection.
- Added provider-specific reasoning controls and clearer per-role model overrides.

### Fixed

- Applied configuration changes made before the first message without restarting the terminal UI.
- Corrected input-budget calculations for shared-window models.

## [0.11.0] - 2026-07-18

### Added

- Added specialist subagent roles and capability-aware delegation.
- Added live subagent progress and result attribution to the terminal UI.
- Added an opt-in visual designer for image generation.

### Security

- Constrained child tools, execution modes, deadlines, and result handling so delegated output
  cannot change parent authority or permissions.

## [0.10.0] - 2026-07-13

### Added

- Added ChatGPT Codex subscription login, encrypted refreshable credentials, and live model
  discovery.
- Added DDGS web search as a keyless fallback and expanded hosted search support.
- Added repository mapping, focused test discovery, cached update prompts, and safer workspace
  previews.

### Changed

- Improved provider-aware prompt caching, usage accounting, terminal navigation, Forge planning,
  and OpenAI Responses streaming recovery.

## [0.9.8] - 2026-06-29

### Added

- Added stronger completion contracts and verification evidence for one-shot and managed tasks.
- Added shell waiting, durable service tools, and stricter web-fetch provenance.

### Changed

- Improved Forge verification, runtime deadlines, provider adapters, usage tracking, and recovery
  from unknown tool calls.

## Earlier releases

Release notes for versions before 0.9.8 remain available in the
[GitHub Releases archive](https://github.com/AlysisAi/AlysisCode/releases).

[Unreleased]: https://github.com/AlysisAi/AlysisCode/compare/v0.14.1...HEAD
[0.14.1]: https://github.com/AlysisAi/AlysisCode/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.8...v0.14.0
[0.13.8]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.7...v0.13.8
[0.13.7]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.6.2...v0.13.7
[0.13.6.2]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.6.1...v0.13.6.2
[0.13.6.1]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.6...v0.13.6.1
[0.13.6]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.5...v0.13.6
[0.13.5]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.4...v0.13.5
[0.13.4]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.3...v0.13.4
[0.13.3]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.2...v0.13.3
[0.13.2]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.1...v0.13.2
[0.13.1]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/AlysisAi/AlysisCode/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/AlysisAi/AlysisCode/compare/v0.11.2...v0.12.0
[0.11.2]: https://github.com/AlysisAi/AlysisCode/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/AlysisAi/AlysisCode/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/AlysisAi/AlysisCode/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/AlysisAi/AlysisCode/compare/v0.9.8...v0.10.0
[0.9.8]: https://github.com/AlysisAi/AlysisCode/releases/tag/v0.9.8
