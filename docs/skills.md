# Skills

Skills are named instruction bundles rooted at a `SKILL.md` file. They let teams package repeatable guidance, references, scripts, and assets without changing Alysis Code itself.

Skills are prompt context, not privileged host policy. Higher-priority system instructions, direct user instructions, execution modes, workspace boundaries, and tool policy still apply.

## Skill Bundle

A skill is a directory with a required `SKILL.md` entrypoint:

```text
<skill_name>/
  SKILL.md
  references/
  scripts/
  assets/
```

Only `SKILL.md` is required. The optional directories are available for supporting material.

`SKILL.md` uses lightweight frontmatter plus a Markdown body:

```markdown
---
name: migrations
description: Help inspect, create, and verify database migrations safely.
---

Use this skill when a task involves migration review, generation, or validation.

Prefer repository-approved migration commands and inspect adjacent migration files before editing.
```

Supported frontmatter:

- `name`
- `description`

Unknown frontmatter keys are ignored for interoperability.

## Discovery Paths

Project-local roots:

- `./.alysis_skills/<skill_name>/SKILL.md`
- `./.agents/skills/<skill_name>/SKILL.md`
- `./.claude/skills/<skill_name>/SKILL.md`
- `./.github/skills/<skill_name>/SKILL.md`

User-global roots:

- `~/.config/alysis/skills/<skill_name>/SKILL.md`
- `~/.config/agents/skills/<skill_name>/SKILL.md`
- `~/.claude/skills/<skill_name>/SKILL.md`
- `~/.copilot/skills/<skill_name>/SKILL.md`

Bundled root:

- the packaged `alysis_code/skills/bundled/<skill_name>/SKILL.md` directory

Alysis Code discovers only approved roots. It does not scan arbitrary `skills/` directories.

When multiple skills have the same name:

1. nearest project ancestor wins
2. native project roots win over interop roots at the same level
3. project-local skills win over user-global skills
4. user-global native roots win over user-global interop roots
5. every project-local or user-global skill wins over a bundled skill with the same name

Malformed skills are skipped and reported as discovery issues instead of crashing session startup.

## How Skills Are Used

When `skills_enabled=true`, Alysis Code advertises discovered skill names and short descriptions to the session. Full skill bodies are not injected by default.

With `skills_auto_invoke=true`, the working agent chooses relevant skills from this catalog
using its conversation and current evidence. This applies to parent agents and subagents,
in chat and one-shot runs. There is no separate selection model call or preliminary
recommendation. The working model keeps its configured reasoning settings.

The agent compares descriptions with the requested workflow, honors explicit requests and
exclusions, and chooses the narrowest fit or proceeds directly. It reads a chosen skill's
`SKILL.md` before relying on its workflow; instructions already in context can be reused.
Skill choice does not use a host keyword table, grant tool permissions, or require a
fixed tool sequence.

With `skills_auto_invoke=false`, the catalog and `skill_read` remain available for manual
use, without proactive skill-choice guidance. Setting `skills_enabled=false` disables
skill discovery and the skill tool. Neither setting needs to change to remove the former
selector call.

The model can read a skill on demand with the read-only built-in tool:

```text
skill_read(name)
skill_read(name, path)
```

`path` is relative to the skill bundle and is bounded to that bundle. Typical targets are under `references/`, `scripts/`, and `assets/`.

Skills do not run scripts automatically. If a task calls for a bundled helper script, it must be run through the normal tool surface and execution-mode guardrails.

## Chat Commands

In chat:

```text
/skill
/skill migrations
/skill migrations add an audit-log migration
```

Behavior:

- `/skill` lists discovered skills
- `/skill <name>` shows information for one skill
- `/skill <name> <task...>` attaches that skill to the current turn only

Explicit skill attachment does not persist across later turns.

At an idle prompt, `$` is the shorter explicit form and provides live skill-name completion:

```text
$migrations
$migrations add an audit-log migration
```

`$<name>` shows information and `$<name> <task...>` attaches the skill for one turn.
Only an exact discovered skill name activates the shorthand, so other `$...` text remains an
ordinary user message. While a turn is already running, `$...` remains ordinary steering; it is
not interpreted as an invocation.

## Bundled Skill Pack

Alysis Code ships nine starter skills:

| Skill | Purpose |
| --- | --- |
| `alysis-docs` | Consult official Alysis app and CLI documentation for product help and configuration guidance. |
| `address-pr-comments` | Resolve existing pull-request review threads and prepare a response summary. |
| `code-review` | Review a diff or branch and report severity-ordered findings with file and line evidence. |
| `commit` | Create a Conventional Commit, staging a coherent set when the index is empty. |
| `debug` | Reproduce and isolate a specific observable failure before fixing it. |
| `fix-ci` | Diagnose a failing CI job from its logs, reproduce it when practical, and verify the focused repair. |
| `release-notes` | Draft grouped release notes from merged history since the previous tag. |
| `security-review` | Perform a requested security-focused diff or branch assessment without automatic fixes. |
| `skill-creator` | Create or revise a reusable skill through the supported scaffold and validation workflow. |

Bundled skills have the lowest discovery precedence. Add a project or user-global skill with the same name to replace the bundled definition entirely.

Disable or re-enable one bundled skill through the normal global state file:

```bash
alysis skill disable code-review
alysis skill enable code-review
```

Lifecycle removal never deletes packaged bundles. Set `bundled_skills_enabled=false` to remove the entire bundled root from discovery while leaving other skill roots enabled.

## CLI

Inspect skills:

```bash
alysis skill list
alysis skill info migrations
```

Create a skill:

```bash
alysis skill init migrations --description "Help inspect and verify DB migrations"
alysis skill create docs-consistency --portable
```

Validate skills:

```bash
alysis skill validate ./.alysis_skills/migrations
alysis skill validate --name migrations --path .
alysis skill validate --all --path .
```

Install skills:

```bash
alysis skill install ./vendor/skills/migrations
alysis skill install ./downloads/migrations.zip
alysis skill install https://example.com/team/skills.git --subdir skills/migrations
```

Enable, disable, or remove:

```bash
alysis skill disable migrations
alysis skill enable migrations
alysis skill remove migrations
```

Use `--project --path <workspace>` when a lifecycle operation should apply to a project scope instead of the user-global scope.

See [Skills lifecycle](skills_lifecycle.md) for the full authoring, install, validation, and removal workflow.

## Managed And Unmanaged Skills

Managed native roots:

- project-local: `./.alysis_skills/`
- user-global: `~/.config/alysis/skills/`

Managed state files:

- project: `./.alysis/skills.json`
- user: `~/.config/alysis/skills.json`

Lifecycle commands create and remove managed native skills. Interop roots remain discoverable, but Alysis Code does not delete arbitrary foreign-root bundles.

## Trust Model

Skills are untrusted instructions. Project-local skills come from the repository, user-global skills come from the user's environment, and bundled skills ship with Alysis Code. None can override system instructions, direct user requests, execution modes, workspace binding, or tool policy.

Review skills before relying on them, especially when they recommend commands, external services, or broad edits.

## Configuration

```bash
alysis config set skills_enabled true
alysis config set skills_auto_invoke true
alysis config set bundled_skills_enabled true
```

Defaults:

- `skills_enabled = true`
- `skills_auto_invoke = true`
- `bundled_skills_enabled = true`

Set `skills_enabled=false` to disable skill discovery, advertisement, automatic skill choice, and
`skill_read` registration in both the parent session and every delegated child. This master switch
also suppresses bundled skills even when `bundled_skills_enabled=true`; subagents remain available
when enabled because delegation and skills are independent capabilities. Set
`skills_auto_invoke=false` for manual discovery and explicit `/skill` or `$` usage. Set
`bundled_skills_enabled=false` to hide only the packaged starter pack while other skill roots remain
enabled. Automatic skill choice happens in the working model's normal request, without an
additional selector request or a separate reasoning setting.

## Bundled Pack Evaluation

Run the bundled pack's model-in-loop gate from the repository root:

```bash
.venv/bin/python -m alysis_code.skills.eval_runner \
  --manifest tests/fixtures/skills_eval/bundled_pack_cases.json \
  --mode combined_auto \
  --deadline-seconds 300 \
  --launch-gate
```

The command writes raw records plus JSON and Markdown summaries under `.alysis/evals/skills/`
by default. It returns non-zero when any case fails or when a launch-readiness threshold is not
met; release-gate details are preserved in both summaries. Omit `--launch-gate` for diagnostic
runs that should enforce individual case results without applying the aggregate launch thresholds.
Launch-gate runs require the complete manifest and cannot be combined with `--case`; the manifest
must select exactly one `combined_auto` mode and include verified normal, explicit, and adjacent
readonly negative-control coverage for every expected skill. A readonly authentication preflight
runs before the suite. Each launch-gate agent run has a finite 300-second wall-clock deadline by
default; `--deadline-seconds` overrides it. Diagnostic runs remain unbounded unless the option is supplied.
A manifest tagged as the bundled pack must cover every packaged bundled skill.
These evaluations call the configured model; they are not part of the unit test suite.
Eval manifests are trusted developer input because each `verification_command` executes through
the shell. Review any untrusted manifest before running it.

## Limitations

The starter pack above is bundled and versioned with Alysis Code. These distribution and runtime limitations remain:

- no marketplace or registry browsing
- no packaged skill update channel
- no dynamic per-skill slash aliases; explicit per-skill invocation uses `$<name>`
- no implicit script execution
- no arbitrary-root scanning outside approved paths
- no automatic skill loading inside swarm workers
