# Migrating from Sylliptor to Alysis Code

Sylliptor is now **Alysis Code**. The CLI, the Python package, the VS Code
extension and every environment variable changed name. This page is the
complete list, what happens automatically, and what you have to do yourself.

Nothing here is urgent. Every old name still works for now.

## The short version

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

## Upgrading

The package name changed, so this is not a plain `--upgrade`. Installing the
new package without removing the old one leaves two distributions competing for
the same `sylliptor` console script.

**pipx**

```
pipx uninstall sylliptor-agent-cli
pipx install alysis-code
```

**pip**

```
pip install --upgrade alysis-code
pip uninstall sylliptor-agent-cli
```

`alysis update` detects a pre-rename install and prints the right command
rather than attempting an upgrade that cannot work.

## What migrates by itself

You should not have to do anything for any of these.

- **Config and credentials.** `~/.config/sylliptor` (or the platform
  equivalent) is copied to the Alysis Code location on first run, including
  `config.json` and `credentials.json`. The old directory is left in place and
  marked with `.migrated-to-alysis`, so an older install on the same machine
  keeps working. Delete it once you're satisfied.
- **MCP OAuth tokens.** The keyring entry moved from service
  `sylliptor-agent-cli` to `alysis-code`; the old one is adopted automatically
  and left intact. Token blobs sealed with the previous authenticated-data tag
  still decrypt and re-seal on the next write.
- **Your Pro profile.** A profile named `sylliptor` is renamed to `alysis`,
  including when it is your active profile, and the stored gateway key follows.
- **Environment variables.** Every `ALYSIS_*` variable falls back to its
  `SYLLIPTOR_*` predecessor. The new name wins if both are set. You get one
  deprecation notice per variable per run.
- **VS Code settings.** `sylliptor.*` settings are copied to `alysis.*` on
  first activation, preserving user vs. workspace scope. The old keys are left
  behind so downgrading still works.
- **`.sylliptor/` in your repos.** Used in place, not renamed — renaming a
  tracked directory would show up as an unexplained diff in your working tree.
  Rename it yourself whenever it's convenient.
- **Plugins.** Both manifest filenames load, and `compatibility.sylliptor` is
  still accepted as a spelling of `compatibility.alysis`.
- **The `sylliptor` command.** Still installed as an alias. It prints a notice
  to stderr and forwards; stdout is unaffected, so pipelines keep working.

## What you have to do yourself

- **Update CI and Dockerfiles.** Pinned references to `sylliptor-agent-cli` or
  `ghcr.io/alysisai/sylliptor-sandbox` keep resolving to the retired
  package and image, which stop receiving releases.
- **Update hook and custom-tool scripts eventually.** They still receive both
  `SYLLIPTOR_*` and `ALYSIS_*` variables, so nothing breaks today.

## Removal timeline

The compatibility layer is not permanent. Every fallback listed under "what
migrates by itself" is scheduled for removal no earlier than the next major
release, and each will warn for at least one minor release before it goes.

## Things that deliberately did not change

- Gateway keys keep the `slk_` prefix — it is validated server-side.
- The product site is still `sylliptor.alysisai.com`. It moves to
  **alysiscode.com**, but not until that host actually serves `/activate`:
  the CLI sends Pro users there to approve a device-login code, so switching
  early breaks sign-in with no client-side workaround. Everything user-visible
  now derives from one constant in `alysis_code/alysis_cloud.py`, and both
  gateway hostnames stay recognised across the move.
- `alysisai.com` remains the Alysis AI company site and is unaffected.
- Past `CHANGELOG.md` entries still say Sylliptor. They describe releases that
  shipped under that name, and rewriting them would make the record wrong.
