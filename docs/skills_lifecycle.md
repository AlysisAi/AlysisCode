# Skills Lifecycle

This document covers the local authoring and lifecycle layer that sits on top of alysis's raw
skill discovery.

It is intentionally local-first and conservative:

- create/scaffold skill bundles
- validate bundles before using or sharing them
- install bundles from local directories, zip archives, or git repos
- enable/disable skills without mutating foreign interop roots
- remove only managed native installs

Git sources require HTTPS in both the CLI and desktop. Local directories and zip archives
remain supported; `file://`, SSH and other Git transports are refused. Installer Git children
use an empty temporary home, no inherited Git configuration or credential helpers, no
interactive stdin, and a 120-second timeout per command. Explicit CA/proxy environment
settings remain available. Configuration-only CA/proxy settings, URL rewrites and Git LFS
filters are not loaded; LFS content therefore remains pointers. Git for Windows system TLS
settings are also excluded; native Windows TLS compatibility has not been measured.

It does **not** add marketplace browsing, publishing, trust-tier redesign, matcher redesign, or
automatic skill execution.

## Managed Vs Unmanaged

Managed native roots:

- project-local: `./.alysis_skills/`
- user-global: `~/.config/alysis/skills/`

Managed lifecycle state:

- project overrides/state: `./.alysis/skills.json`
- global lifecycle state: `~/.config/alysis/skills.json`

Runtime and inspection flows treat malformed lifecycle state as non-fatal:

- invalid JSON / UTF-8 or malformed managed records fall back to empty/default state
- session startup, `alysis skill list`, and `alysis skill info` continue to work
- the CLI surfaces a concise `Lifecycle state warning` instead of a stack trace

Mutation commands stay conservative. They still require a usable lifecycle state rather than
silently overwriting corrupted managed metadata.
For managed native writes, that stricter path is also rollback-safe:

- malformed lifecycle state causes `init|create`, `install`, and `remove|uninstall` to fail before
  bundle mutation
- lifecycle state JSON writes publish through an atomic temp-file + replace path
- `--force` replacement keeps the previous managed bundle if the later state commit fails
- managed remove first moves the bundle to a backup path and restores it if the state update fails

Unmanaged skills are still discoverable, including interop roots such as:

- `./.agents/skills/`
- `./.claude/skills/`
- `./.github/skills/`
- `~/.config/agents/skills/`
- `~/.claude/skills/`
- `~/.copilot/skills/`

Lifecycle commands may toggle discovered skill names through state overlays, but `remove` only
deletes managed native installs.

## Scaffold A Skill

Native project-local scaffold:

```bash
alysis skill init verification-playbook --description "Recommend verification commands safely"
```

Portable project-local scaffold:

```bash
alysis skill create docs-consistency --portable
```

User-global scaffold:

```bash
alysis skill init architecture-review --user --description "Review architectural changes"
```

Project-family selection:

```bash
alysis skill init react-ui --family claude
```

Scaffolding creates:

```text
<bundle>/
  SKILL.md
  references/
  scripts/
  assets/
```

Native project/user scaffolds are recorded as managed installs. Portable/interop scaffolds remain
unmanaged by design.

## Validate A Skill

Validate a bundle path:

```bash
alysis skill validate ./.alysis_skills/verification-playbook
```

Validate a discovered skill by name:

```bash
alysis skill validate --name verification-playbook --path .
```

Validate all discovered bundles for a workspace:

```bash
alysis skill validate --all --path .
```

Validation checks:

- `SKILL.md` exists
- `SKILL.md` is valid UTF-8
- YAML-style frontmatter exists
- `name` and `description` are present and non-empty
- the body/instructions are non-empty
- required content does not rely on symlinks
- optional `references/`, `scripts/`, and `assets/` directories are accepted

Unknown frontmatter keys are tolerated. Malformed bundles fail validation cleanly instead of
crashing discovery or session startup.

## Install A Skill

From a local bundle directory:

```bash
alysis skill install ./vendor/verification-playbook
```

From a local zip archive:

```bash
alysis skill install ./downloads/verification-playbook.zip
```

From git with a nested subdirectory:

```bash
alysis skill install https://example.com/team/skills.git --subdir bundles/verification-playbook
```

Project-local managed install:

```bash
alysis skill install ./vendor/verification-playbook --project --path .
```

Install behavior:

- the source must resolve to exactly one skill bundle
- `--subdir` narrows archive/repo/directory sources when needed
- validation runs before the final managed copy
- ambiguous multi-skill sources fail instead of guessing
- zip traversal and zip symlinks are rejected
- zip installs are bounded to `256` files and `8 MiB` total uncompressed size
- git installs use a temporary clone and record the resolved commit when available

## Enable And Disable

Global toggle:

```bash
alysis skill disable verification-playbook
alysis skill enable verification-playbook
```

Project override:

```bash
alysis skill disable verification-playbook --project --path ./packages/app
alysis skill enable verification-playbook --project --path ./packages/app
```

Project overrides are name-based and apply only within that workspace. They do not rename or edit
foreign interop bundle directories.

## Remove / Uninstall

Remove a managed user-global install:

```bash
alysis skill remove verification-playbook
```

Remove a managed project-native install:

```bash
alysis skill uninstall verification-playbook --project --path .
```

This removes the managed native bundle directory plus its managed metadata. Attempting to remove an
unmanaged interop bundle fails clearly instead of deleting arbitrary foreign-root content. Managed
bundle paths are resolved back under the managed native root and tampered `bundle_dir` values are
rejected instead of deleting outside that root. Removal is also rollback-safe: the bundle is moved
to a temporary backup location first, the lifecycle state is persisted, and only then is the
backup deleted.

## Agent Workflow

When a user asks alysis to help create a new skill, the preferred flow is:

1. `alysis skill init` or `alysis skill create`
2. edit `SKILL.md` and any optional `references/`, `scripts/`, or `assets/`
3. `alysis skill validate`
4. optionally `alysis skill install` / `enable` / `disable` / `remove`

This lifecycle guidance is available even if the workspace has no discoverable skills yet. The
separate discovery/`skill_read` guidance only appears once at least one skill bundle is
discoverable for that session.

This keeps skill authoring CLI-first and leaves the runtime `/skill <name> [task]` chat command
focused on one-turn skill attachment rather than lifecycle management.

## Out Of Scope

- marketplace/registry browsing
- publishing or packaged distribution
- trust-tier redesign
- matcher or activation redesign
- implicit script execution
- per-skill permissions or version/update channels
