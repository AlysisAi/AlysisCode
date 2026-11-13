# Alysis Code engine identity

The engine uses only the Alysis identity. This is a breaking compatibility change;
update the desktop app before bundling this engine into an installed application.

## Canonical names

| Surface | Identity |
| --- | --- |
| Python distribution / import | `alysis-code` / `alysis_code` |
| Commands | `alysis`, `alysis-code`, `alysis-ide-bridge` |
| Environment | `ALYSIS_*` |
| Project data | `.alysis/` |
| User config and data | Platformdirs app/author `alysis` |
| Server data | Platformdirs app `alysis-code-server`, author `alysis` |
| Plugin manifest / compatibility key | `alysis-plugin.toml` / `compatibility.alysis` |
| VS Code configuration | `alysis.*` |
| Capability contract | `alysis.capabilities`, version 1 |
| Forge Host / status protocols | `alysis.forge-host` v2 / `alysis.forge.status` v1 |
| Verification contract schema | `alysis.forge.verification-contract/v2` |
| Computer Use workflow metadata | `alysis_workflow` |
| Release signature domain / key ID | `sylliptor-managed-cli-release-v3`, frozen for schema v3 / `alysis-release-2026-01`; `sylliptor-release-2026-01` is still accepted |
| MCP OAuth authenticated-data tag / keyring service | `alysis-mcp-oauth-store` / `alysis-code` |
| Product site / gateway hostname | `https://alysiscode.com` / `api.alysiscode.com` |

| | Before | After |
|---|---|---|
| Command | `sylliptor` | `alysis` |
| PyPI package | `sylliptor-agent-cli` | `alysis-code` |
| Python module | `sylliptor_agent_cli` | `alysis_code` |
| Env prefix | `SYLLIPTOR_*` | `ALYSIS_*` |
| Per-repo directory | `.sylliptor/` | `.alysis/` |
| Plugin manifest | `sylliptor-plugin.toml` | `alysis-plugin.toml` |
| Repository | `AlysisAi/Sylliptor` | `AlysisAi/AlysisCode` |
| Sandbox image | `ghcr.io/alysisai/sylliptor-sandbox` | `ghcr.io/alysisai/alysis-sandbox` |

## Updating existing installations

Automatic brand aliases, directory copying, project-directory moves, profile
renaming, setting migration, alternate plugin manifests, OAuth keyring adoption,
and alternate encryption tags have been removed. The engine does not delete
existing user directories, credentials, sessions, or token stores.

Back up existing state before an attended app migration. The app must update its
capability admission, protocol discriminators, workflow metadata, context markers,
configuration paths and environment, runtime builder, and release verifier together.
Stored plan, approval, verification, and admission fingerprints are recomputed under
the new domains; previous approvals cannot authorize newly named contracts.
Previously signed runtime releases keep verifying: the release signature domain is frozen
for schema v3 and the pre-rename key name is still accepted. Existing OAuth
stores sealed under a different authenticated-data tag require reauthentication;
renaming the file cannot change its cryptographic domain.

Run the engine contract tests and the app's admission validator before an app
runtime pin update. Then validate a packaged app with chat, Forge, and Computer Use.

## Repository audit scope

The current tracked engine files and filenames use the canonical identity.
Git history, the checkout's directory name, local user data, cached bytecode,
build output, previous release archives, and ignored QA evidence are separate
from the current source and are retained for recovery. Rebuild distributable
artifacts from this source; do not ship an older cached runtime.
