# Personas

Personas are named postures for interactive chat: **code**, **architect**, **ask**, and
**debug**, plus any custom personas you define. A persona bundles a prompt overlay, a model
role, a default execution mode, and (for architect) a write scope.

The core rule: **a persona is a convention, never a permission.** Execution modes, approval
prompts, write scoping, and the sandbox stay the only enforcement layer. A persona switch can
keep or *lower* your effective execution mode — the clamp rule — and can never raise it above
the mode you chose. If you set `readonly`, every persona runs readonly.

## Built-in personas

| Persona | Execution mode | Model role | What it does |
|---|---|---|---|
| `code` | keeps yours | `coding` | Implementation work; the no-op default — behaves exactly like a persona-less session |
| `architect` | `review` (clamped) | `planner` | Plans and designs; may create/edit **markdown documents only** (`*.md`, `**/*.md`), each write behind the normal review approval |
| `ask` | `readonly` (clamped) | `comprehension` | Read-only questions with inspection tools; write and shell tools are removed by the host |
| `debug` | keeps yours | `coding` | Reproduce-before-fix: reproduce the failure, fix minimally, rerun the reproduction |

Architect deliberately runs at `review`, not `readonly`: markdown writes must be possible, and
review keeps each one behind an approval. Because `fullaccess` bypasses write scoping, the
clamp pulls fullaccess users down to `review` while architect is active; your mode returns the
moment you switch to code or debug. If the session already has a user write scope, a path must
match both that scope and the persona scope. Command-style and extension tools whose writes
cannot be bounded to those paths (shell, verification commands, custom tools, MCP tools, and
opaque IDE task/debug launches) are unavailable while a persona write scope is active.

## Switching

- `/persona architect` — switch directly; bare `/persona` opens a picker (classic and TUI).
- **Tab** on an empty input in the TUI cycles code → architect → ask → debug (→ customs).
  Silent by design: the footer badge flipping (`code · safe` → `architect · safe`) is the
  feedback.
- The model can *propose* a switch with the `switch_mode` tool. You approve or decline
  through a normal approval prompt; an approved switch applies when the turn ends, declining
  is not an error, and a repeated identical proposal is auto-declined. The tool exists only
  in top-level interactive chat — one-shot runs and subagents never
  see it, so automation cannot switch personas silently.
- `/permissions` is the execution-permissions command. An explicit
  `/permissions <exec-mode>` always wins:
  it redefines your base mode and clears any persona-held restore point and write scope.
- Permission selections apply to the next user message; repeated selections replace
  the pending choice without changing a running turn's tools. Persona changes
  during a running turn apply after it finishes.
- Chat plan mode is gone: the former `/plan mode` overlay and the `/plan <task>`
  draft → review → approve → execute loop were both removed, and `/plan` is not a chat
  command any more. Architect is the planning posture.
- The active persona survives `/resume`: the base mode is restored from the session start and
  the last applied persona is re-applied on top, reproducing the narrowed mode, scope, and
  model exactly.

## Sticky persona models

Each persona resolves its model through the existing role chain:

```
persona_models.<persona>  ->  role  ->  env var / plan / role_models / default model
```

When the resolved model or its role temperature differs from the live client's, the switch swaps
the main chat client (cached by model and temperature; returning to code restores the original
client object). Every candidate model is checked against `model_metadata_policy` before the
persona state changes. With no role or persona model configuration, every persona resolves to
your default model and normally requires no swap.

```bash
alysis config set role_models.planner your-planner-model   # architect now uses it
alysis config set persona_models.ask review                # ask uses the review role
```

## Custom personas

Drop markdown files in `.alysis_personas/` in your project (or `personas/` in the user
config directory; project wins on name collisions, builtins can never be shadowed):

```markdown
---
name: docs-writer
description: Documentation writer
exec_mode: review
model_role: coding
allow_write_globs:
  - docs/**
---
Write and edit documentation only. Keep prose concise; never modify source code.
```

The body becomes ephemeral user-context for each turn, prefixed with an untrusted-content
prelude; unlike built-in overlays, it is never promoted to a system message. Loading is
fail-closed: symlinked/non-regular or oversized files are skipped, directory scans are bounded,
unsupported `exec_mode` values (anything above `review`) drop to `readonly`, unknown model
roles drop to `coding`, and invalid files or name collisions are skipped with a startup warning.
Custom personas appear in `/persona`, the pickers, and the Tab cycle; the `switch_mode` tool
proposes builtins only.

## Scripting

```bash
alysis run --persona architect "Design the auth flow and write it to PLAN.md"
alysis chat --persona ask
```

`run --persona` applies the clamp, write scope, overlay, and role model for the single turn;
unknown personas are rejected up front. `chat --persona` sets the starting persona for that
invocation; `config set default_persona architect` makes it permanent.

## Configuration

| Key | Meaning |
|---|---|
| `default_persona` | Persona chat starts in (`code` default) |
| `persona_models.<persona>` | Model role override per persona |
| `persona_modes_enabled` | Kill switch; `ALYSIS_PERSONA_MODES=off` wins over config |

With the feature off, `/persona` explains itself, `/permissions` accepts execution modes, the
`switch_mode` tool is not registered, and every prompt is byte-identical to pre-persona
Alysis Code.

## Observability

Every switch emits a `persona_changed` surface event and a `persona_switch_applied` session-log
event (persona, effective mode, source `user`/`model`/`config`/`resume`, and the live model).
`/status` shows the active persona; the model sees it as a `persona:` line in its environment
context and receives a short system-prompt section explaining the persona contract.

See also: [Subagents](subagents.md) and [Security model](security_model.md).
