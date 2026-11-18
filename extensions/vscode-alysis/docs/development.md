# Development

```bash
npm install
npm run compile
npm run lint
npm test
npm run bundle
npm run package
npm run test:integration
```

## Two build outputs

`npm run compile` runs `tsc` into `out/` for type-checking, the unit tests and the build-time
scripts, then esbuild (`scripts/bundle.js`) from `src/extension.ts` into a single
`dist/extension.js`, which is what `package.json` `main` points at and therefore what VS Code
loads. One command, both outputs, so every `test:*` script that launches VS Code after compiling
finds an entry file. `npm run compile:tsc` skips the bundle for a pure type-check. Every packaging
path replaces the dev bundle with a minified one (`bundle:production`, also wired as
`vscode:prepublish` so a bare `vsce package` cannot ship a stale or missing bundle). `out/` is
excluded from the VSIX entirely. For F5, `npm: watch` and `npm: watch:bundle` run together as
the `watch: all` task.

Because the two layouts differ, extension sources never resolve assets through `__dirname`; they
go through `context.extensionUri`. `test/bundleWiring.test.ts` enforces this and the manifest
wiring above.

## Test suites

`npm test` includes Node unit tests plus a real Python stdio bridge smoke that sends `initialize`,
`getCapabilities`, `session.create`, `session.list`, and `session.cancel` to the local source
checkout. It uses a dummy model/API key and localhost base URL, does not send a chat turn, and does
not call a model provider.

`npm audit --audit-level=high` is expected to pass. Mocha's vulnerable dev-only transitive
dependencies are pinned through package overrides and are not packaged into the VSIX.

`npm run test:integration` launches a VS Code Extension Development Host through
`@vscode/test-electron` and uses an external mock CLI fixture outside the packaged extension. It
uses `VSCODE_TEST_VERSION` when it needs to download VS Code. Release CI runs the same suite against
the declared minimum (`1.90.0`) and the current Stable channel. The runner first verifies
`VSCODE_TEST_EXECUTABLE_PATH` when it is set, or checks that the VS Code download host is reachable
before calling `@vscode/test-electron`. Set `VSCODE_TEST_EXECUTABLE_PATH=/absolute/path/to/code` to
use a pre-downloaded VS Code executable in offline or cached CI environments. Cached release runs
require separate minimum-version and current executables. On headless Linux, run it under
`xvfb-run -a`. Release workflows must run this command rather than silently skipping Extension Host
coverage. The runner removes its temporary workspace, user-data, and extensions directories after
every pass.

The mock bridge E2E suite does not call model providers or require API keys. It covers sidebar open,
`/help`, `/doctor` failure surfacing in Timeline and Diagnostics, broken bridge health/missing-method
chips, native Tasks host actions, attached diagnostics, unknown slash commands failing closed, and
bridge exit reporting.

## Packaging

`npm run package` delegates to strict VSIX packaging through `npm run package:vsix`. It does not
publish anything and no packaging script uses `--allow-missing-repository`; manifest metadata must
remain complete. `npm run package:pre-release` adds the Marketplace pre-release marker for manual
beta packaging, and `npm run prepublish:check` runs compile, lint, tests, high-severity audit, and
packaging before any maintainer considers publication. `npm run publish:dry-run` is intentionally
non-publishing: it runs the prepublish gate, creates a pre-release package, and lists packaged
files.

`package:vsix` runs `scripts/package-release.js`, which fails closed unless a release target is
declared (`--target <platform>` or `ALYSIS_VSIX_TARGET`), `resources/managed-cli/manifest.json`
names a signed executable for that target that exists on disk at the manifest's size, and
`README.md` carries no `PUBLISH BLOCKER` marker and at least one absolute-URL image. Those are the
publish gates; do not weaken them locally.

`npm run package:dev-vsix` is the one script that bypasses every gate. It produces a VSIX with no
bundled runtime, so an install of it can only run a CLI you point `alysis.cliPath` at (labelled as a
development override in Runtime Details). Use it for local dogfooding and screenshots; never upload
it, and never rename it to look like a release artifact.

## Running without a bundled runtime

A source checkout has no `resources/managed-cli/`. In the Extension Development Host
(`ExtensionMode.Development`) the extension falls back to `alysis` on PATH and says so; a
production install of the same tree does not. Either put the monorepo's CLI on PATH (`uv run` or an
activated `.venv`) or set `alysis.cliPath` to its absolute path.

## Manual VSIX install

```bash
code --user-data-dir /tmp/alysis-vsix-profile \
  --extensions-dir /tmp/alysis-vsix-extensions \
  --install-extension vscode-alysis-<version>.vsix --force
code --user-data-dir /tmp/alysis-vsix-profile \
  --extensions-dir /tmp/alysis-vsix-extensions \
  --list-extensions --show-versions
```

Confirm the list contains `alysisai.vscode-alysis@<version>`, then launch that same isolated
profile against a disposable workspace and complete the first-run, chat, cancel, reload, and
missing-CLI checks from [`RELEASE_CHECKLIST.md`](../RELEASE_CHECKLIST.md). On PowerShell, use two
fresh directories under `$env:TEMP` in place of the `/tmp/...` paths. Close VS Code and remove the
two temporary directories after the smoke. You can also use VS Code's Extensions view and choose
"Install from VSIX...".
