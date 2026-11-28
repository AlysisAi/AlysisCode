# Real VS Code remote acceptance

The `vscode-real-remote-acceptance` workflow is a release gate for two actual VS Code Remote Extension Hosts. Hosted runners and local Electron tests cannot satisfy it.

## Required protected environments

Create `vscode-real-wsl-acceptance` and `vscode-real-remote-ssh-acceptance`. Each environment must route to a dedicated, interactive Windows x64 runner with the shared labels `self-hosted`, `windows`, `x64`, and `alysis-release`, plus exactly one environment label:

- WSL: `real-wsl`
- Remote-SSH: `real-remote-ssh`

The hosts must be dedicated to release acceptance. The Windows runner must run in an interactive desktop session, not as a Windows service. The remote host must be Linux x64, have `sh`, `base64`, and `hostname`, and allow VS Code Server installation. SSH authentication must already be configured for non-interactive `BatchMode=yes`; no private key or password is passed to the workflow.

Set these protected environment variables:

| Variable | Required value |
| --- | --- |
| `ALYSIS_ACCEPTANCE_RUNNER_ROLE` | `real-wsl` or `real-remote-ssh`, matching the environment |
| `ALYSIS_VSCODE_EXECUTABLE` | Absolute path to the provisioned VS Code 1.90.0 executable |
| `ALYSIS_VSCODE_EXECUTABLE_SHA256` | Lowercase SHA-256 of that executable |
| `ALYSIS_REMOTE_EXTENSION_VSIX` | Absolute path to the provisioned Microsoft Remote prerequisite VSIX |
| `ALYSIS_REMOTE_EXTENSION_SHA256` | Lowercase SHA-256 of that prerequisite VSIX |
| `ALYSIS_REMOTE_AUTHORITY` | Concrete `wsl+<distribution>` or `ssh-remote+<alias>` authority |
| `ALYSIS_REMOTE_CONNECTION` | WSL distribution name or preconfigured SSH alias |
| `ALYSIS_REMOTE_WORKSPACE_ROOT` | Dedicated narrow root under `/tmp/alysis-*` or `/var/tmp/alysis-*` |

The workflow source pins Remote-WSL to 0.88.2 and Remote-SSH to 0.112.0. Preflight verifies the IDs, versions, and protected hashes of those VSIX files and verifies the VS Code version, commit, and executable hash. A missing or mismatched prerequisite fails before any candidate is installed.

Workspace Trust stays enabled. The fresh profile must open the exact disposable remote folder in
Restricted Mode; a pre-trusted remote resolver or profile fails the run. The isolated driver opens
VS Code's Workspace Trust editor, and the Windows harness sends the pinned VS Code 1.90.0
`Ctrl+Enter` action through an ephemeral loopback-only DevTools endpoint. The driver must observe
`onDidGrantWorkspaceTrust`, record that the initial state was restricted, and retain that witness
through the subsequent Extension Host reload. `Ctrl+Shift+Enter` (trust parent), disabling Workspace
Trust, or seeding VS Code's private state database are not accepted. This UI contract is restricted
to VS Code commit `89de5a8d4d6205e5b11647eb6a74844ca23d2573`; another commit fails closed until its trust action is
reviewed and added deliberately.

These prerequisite hashes are integrity pins, not an independent provenance root: the executable
paths and hashes are maintained in the same protected environment variable boundary. Runner
provisioning must therefore be treated as trusted release infrastructure. Provision VS Code and
the Microsoft Remote VSIX from reviewed Microsoft release locations, validate Microsoft's Windows
signature where the downloaded binary exposes one, record the source URL and SHA-256 outside the
runner, and have an environment reviewer compare that record before changing the protected values.
Do not treat the remote gate as proof of Microsoft-origin bytes until that provisioning record has
been reviewed independently.

Before either self-hosted job installs or executes the Alysis Code candidate, it verifies both the
hosted `managed-cli-vsix-release` SLSA provenance and the exact CycloneDX predicate for the
downloaded VSIX. Raw reports are attempt-qualified artifacts. A separate GitHub-hosted job validates
all available raw reports, selects the newest passing WSL and Remote-SSH report independently, and
attests only that selected pair. This permits a legitimate partial rerun such as WSL attempt 1 plus
Remote-SSH attempt 2 without silently rebinding both reports to attempt 2. Evidence and promotion
dispatches must supply both attempt numbers; each report, artifact name, and validation receipt is
checked against its environment-specific value.

## What the gate proves

For the exact release tag and successful `managed-cli-vsix-release` run, each job downloads only `vscode-alysis-linux-x64`, installs it in the real remote Extension Host, and checks:

- `vscode.env.remoteName` and the remote workspace URI type;
- Workspace Trust was enabled, began restricted, and was granted to the current workspace through
  the pinned Trust-editor action rather than a parent-folder or disabled-trust shortcut;
- the salted identities of the authority, Linux hostname, workspace path, and installed extension path;
- production extension mode and the exact candidate version/hash;
- signed managed-runtime origin, Linux target, release/source identity, executable hash, and live bridge health;
- the packaged remote OAuth policy fails closed;
- runtime identity survives a real `workbench.action.reloadWindow` cycle.

Evidence contains hashes rather than raw hostnames, aliases, or paths. Each selected report is
schema-validated, GitHub-attested, and retained for 90 days. The gate cannot be considered executed
until both protected runner jobs and the hosted attestor complete successfully for the release
candidate.

## Recovery after cancellation

Normal failures run a guarded `finally` cleanup that uninstalls
`alysisai.alysis-remote-acceptance-driver` and `alysisai.vscode-alysis`, removes the generated
remote workspace, and removes the temporary local profile. An abrupt runner or workflow
cancellation can bypass that cleanup. Before rerunning the affected environment:

1. Derive the exact acceptance ID from the canceled run: `<run-id>-<attempt>-wsl` or
   `<run-id>-<attempt>-remote_ssh`. Do not guess or use a wildcard.
2. Using the pinned executable and protected authority on the dedicated Windows runner, run:

   ```powershell
   & $env:ALYSIS_VSCODE_EXECUTABLE --remote $env:ALYSIS_REMOTE_AUTHORITY --uninstall-extension alysisai.alysis-remote-acceptance-driver
   & $env:ALYSIS_VSCODE_EXECUTABLE --remote $env:ALYSIS_REMOTE_AUTHORITY --uninstall-extension alysisai.vscode-alysis
   ```

3. On the concrete remote Linux host, set `target` to
   `$ALYSIS_REMOTE_WORKSPACE_ROOT/<acceptance-id>`, print and review it, then use the same guard as
   the harness:

   ```sh
   printf '%s\n' "$target"
   case "$target" in
     /tmp/alysis-*/*|/var/tmp/alysis-*/*) rm -rf -- "$target" ;;
     *) echo "refusing unsafe cleanup target" >&2; exit 64 ;;
   esac
   ```

4. Inspect the runner's configured temporary directory for the exact abandoned
   `alysis-wsl-*` or `alysis-remote_ssh-*` profile created by that run. Remove only the
   reviewed directory, then confirm both extension IDs are absent from `code --remote ...
   --list-extensions --show-versions` before rerunning.
