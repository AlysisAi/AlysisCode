# Alysis Code across editors

## Build contract — 8 September 2026

The user approved extending Alysis Code to Cursor and JetBrains while retaining the existing
VS Code experience. One Python agent owns execution, permissions, tools and sessions.
Editor integrations own project trust, executable selection, editor operations and UI transport.

The first executable slice shares the existing sidebar assets, proves the VS Code extension in
Cursor, and adds an IntelliJ Platform tool window for chat, streaming, permission prompts and
cancellation. JetBrains initially uses an explicitly selected development CLI and its existing
provider configuration. No credentials go into the webview. Production managed-runtime packaging,
JetBrains browser panels, native task/debug actions and ACP are subsequent milestones.
Unsupported UI actions must be hidden or disabled and rejected by the host.

## Decisions

- Keep the current versioned JSON-lines stdio protocol. It is not ACP; ACP needs a separate adapter.
- Share HTML, CSS and browser JavaScript, with an injected `alysisHost` transport. VS Code's
  `acquireVsCodeApi` is confined to the fallback transport. Packaged copies are generated from
  `extensions/shared-ui` and checked for drift.
- Implement the JetBrains shell against IntelliJ Platform APIs, using JCEF to render the same UI.
  Project trust and process authority stay in the native shell, never in browser messages.
- Preserve VS Code's mature controllers during extraction. The portable chat controller is a
  bounded starting point for other editors, not a claim of full feature parity.
- Cursor uses the VS Code extension family. Compatibility claims require its real Extension Host;
  the Marketplace and Open VSX remain separate distribution channels.

## Acceptance and verification

- Existing VS Code lint, renderer/security tests and extension tests pass after extraction.
- Cursor runs the existing deterministic Extension Host journeys in an isolated profile.
- The JetBrains plugin compiles against an actual IntelliJ Platform SDK and packages its shared UI.
- The portable chat path crosses a real stdio subprocess, including streamed output, approval,
  denial/cancel and shutdown; stale events cannot cross session or process boundaries.
- Startup is passive. The selected executable runs only after explicit user action and project trust.
- No live provider calls, publishing or changes to the user's editor profiles are part of this slice.

## Work and evidence

- [x] Extract shared sidebar assets and host transport; preserve VS Code behavior.
- [x] Run Cursor Extension Host checks.
- [x] Implement and package the JetBrains chat integration.
- [x] Exercise the real protocol path and record exact verification limits.

### Verification on 8 September 2026

- Extension lint: passed (shared-asset drift, TypeScript, webview checking, ESLint).
- Extension unit tests: **985 passed**, using a fresh isolated compilation directory.
- Shared controller/renderer: **10 passed**, including duplicate sends, foreign/replayed events,
  approvals, cancellation, disconnect fences and applying permissions before a follow-up turn.
- Cursor 3.12.30: **25 Extension Host tests passed** in a disposable profile, against a frozen
  extension build. Browser, Forge, approvals, diagnostic updates, host Tasks and swarm journeys ran.
- VS Code: **25 Extension Host tests passed** against that same frozen build.
- JetBrains: compiled and packaged using CLion 2022.3.2's platform 223 SDK and JDK 17. The actual
  shared renderer/JetBrains JS adapter → production Java transport → real Python bridge smoke test
  passed with deterministic streamed replies, allow/deny, cancellation, new task and shutdown.
- Python Ruff checks for the new JetBrains build/test scripts: passed.

The extension launcher's test-only Windows JavaScript fixture now uses the outer test runner's Node
executable. Cursor's Electron executable is not a reliable replacement. Production process launchers
retain their existing behavior. `npm run test:cursor` locates an installed Cursor (or uses
`CURSOR_EXECUTABLE_PATH`) and runs the existing Extension Host suite; it never downloads an editor.
The runner also requires a fresh suite-completion report with passing tests and no failures. Some
Cursor runs returned exit code zero after Mocha reported failures; an editor's process exit alone
is therefore insufficient. Missing, empty, failed or inconsistent reports now fail the outer command.
An actual empty Cursor suite was verified to exit 1. The new check also caught an intermittent
Tasks test submission before provider readiness; that test now explicitly loads the provider catalog
and waits for the same readiness gate the real composer uses, with bounded failure diagnostics.

Windows process-tree restart checks initially timed out because sandboxed `taskkill` could not
terminate the exact mock-agent child. Running the isolated suite outside that restriction passed;
the production termination guards were not weakened. A concurrent UI task in this checkout also
necessitated frozen test artifacts so its compilation could not delete this suite's output.

### Remaining release gates

The JetBrains smoke uses JSDOM and a Java transport harness, not a running IDE. Native JCEF rendering,
file dialogs, project trust enforcement and native session ownership still require IDE tests, as do
Plugin Verifier and a supported-product/version matrix. The generated ZIP is a development preview,
not a production-ready compatibility guarantee. It has not been installed or published.

JetBrains browser panels, history/resume, attachments and editor context, host Tasks/Debug,
model/persona management, slash commands and managed-runtime delivery remain separate implementation
milestones. The existing VS Code runtime packaging/release gates also remain in force; this change
does not publish a Cursor/Open VSX release or assert production runtime completeness.

Build/setup instructions: `extensions/jetbrains-alysis/README.md`.
Architecture contract: `extensions/shared-ui/README.md`.

## References checked

- [Cursor extensions](https://prod.cursor.com/help/customization/extensions)
- [IntelliJ product compatibility](https://plugins.jetbrains.com/docs/intellij/plugin-compatibility.html)
- [JCEF integration](https://plugins.jetbrains.com/docs/intellij/embedded-browser-jcef.html)
- [JetBrains ACP](https://www.jetbrains.com/help/ai-assistant/acp.html)

Current environment: Cursor 3.12.30 and CLion 2022.3.2 (platform 223) are installed. The latter
provides an offline SDK and JDK 17 for a local compatibility build; newer JetBrains versions need
their own verification before release support is claimed.
