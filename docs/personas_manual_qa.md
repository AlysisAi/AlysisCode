# Personas & Modes — Manual QA Script

Run in a scratch git repo. Start with defaults (`review` mode, `code` persona) unless a step
says otherwise. Badge = the TUI footer's left segment. Every ✅ is a hard expectation; 🔶 is
model-behavior (should hold, but wording varies).

## 1. Baseline — code persona

| Type | Expected |
|---|---|
| (start `alysis chat`) | Welcome hint line contains `tab to switch persona`. Badge: `code · safe` |
| `/status` | Rows include `persona: code` and `mode: safe (review)` |
| `create hello.py printing hello` | ✅ Approval prompt for the write; after approve, `hello.py` exists |

## 2. Ask persona — read-only, can't be talked into writing

| Type | Expected |
|---|---|
| `/persona ask` | ✅ `Persona set for this session: ask` · badge `ask · read` |
| `what files are in this repo?` | ✅ Answers using inspection tools; **no** approval prompts |
| `create notes.txt with "hi"` | ✅ No file appears. 🔶 Model explains it's read-only and may call `switch_mode` → an approval prompt appears. **Decline** → turn continues calmly, `notes.txt` still absent |
| `please create notes.txt anyway` | ✅ No new approval prompt for the same persona (dedupe); 🔶 model says you already declined |
| `/permissions auto` | ✅ `Permissions for the next message: fast (auto)` · badge `ask · read→fast next`; the next real prompt activates `ask · fast` — explicit permissions outrank the persona |
| `/persona code` | ✅ Badge `code · fast` (auto kept — your explicit choice is the new base) |

## 3. Architect — markdown only, gate-enforced

| Type | Expected |
|---|---|
| `/permissions review`, send a prompt, then `/persona architect` | ✅ The prompt activates safe Permissions; badge then becomes `architect · safe` |
| `design a todo app and save the plan to PLAN.md` | ✅ Write approval → `PLAN.md` exists |
| `now create app.py with the skeleton` | ✅ `app.py` does NOT appear — tool fails with `Blocked write outside allowed scope: app.py`. 🔶 Model explains markdown-only and may propose switching to code |
| `which persona are you in?` | 🔶 Says architect (it sees `persona: architect` in its context) |

## 4. Debug persona

| Type | Expected |
|---|---|
| `/persona debug` | ✅ Badge `debug · safe`; review restored (architect's narrowing undone) |
| (add a buggy script) `fix the bug in buggy.py` | 🔶 Reproduces the failure (runs it) **before** editing, fixes, reruns to confirm |

## 5. Tab cycling (TUI)

| Do | Expected |
|---|---|
| Empty input, press Tab ×4 | ✅ Badge cycles `code · safe → architect · safe → ask · read → debug · safe → code · safe`. **Zero transcript lines** |
| Type `hel`, press Tab | ✅ Completion behavior only — persona unchanged |

## 6. The clamp — modes × personas

| Type | Expected |
|---|---|
| `/permissions readonly`, send a prompt, then `/persona code` | ✅ Badge `code · read` — code does NOT unlock anything; you chose readonly |
| `/permissions fullaccess` (warning prints), send a prompt, then `/persona architect` | ✅ Full activates only for the prompt; badge then becomes `architect · safe` — clamped DOWN to review so the markdown scope binds |
| `/persona debug` | ✅ Badge `debug · full` — your fullaccess comes back |
| `/permissions architect` | ✅ Invalid permissions value; nothing switches |
| `/permissions review`, then send a prompt | ✅ Selection first shows `debug · full→safe next`; the prompt activates `debug · safe` and clears the restore point |

## 7. Model-proposed switch (approve path)

| Type | Expected |
|---|---|
| (in code) `from now on only explain things, never change files` | 🔶 Model calls `switch_mode(ask, …)` → approval prompt. **Approve** → 🔶 reply notes the switch applies at turn end. ✅ After the turn: dim `Persona → ask`, badge `ask · read` |

## 8. Removed chat plan mode

| Type | Expected |
|---|---|
| `/plan`, `/plan mode`, `/plan approve`, `/plan off`, `/plan refactor parser` | ✅ `Unknown command: /plan. Try /help.` — no draft, no model call, badge and mode unchanged |
| Type `/pl` in the TUI | ✅ Completer offers nothing for `/plan` |
| `/help` | ✅ No `/plan` row; planning is `/persona architect` |
| Tab (empty input) right after any of the above | ✅ Persona still cycles normally — no interlock remains |

## 9. Resume

| Do | Expected |
|---|---|
| `/persona architect`, then `/exit`, restart `alysis chat`, `/resume` (pick that session) | ✅ Badge `architect · safe`; classic prints `Persona restored: architect`. Try a non-md write → still `Blocked write outside allowed scope` |

## 10. Custom personas

Create `.alysis_personas/docs-writer.md`:

```md
---
name: docs-writer
description: Documentation writer
exec_mode: review
model_role: coding
allow_write_globs:
  - docs/**
---
Write and edit documentation only. Never modify source code.
```

Also create `.alysis_personas/bad.md` with `exec_mode: fullaccess` in its frontmatter.

| Do | Expected |
|---|---|
| Restart chat | ✅ Startup warning: `persona bad: exec_mode 'fullaccess' unsupported; using readonly` |
| `/persona` (bare) | ✅ Picker shows `5) docs-writer (custom)` |
| `/persona docs-writer` → `write docs/guide.md with a short guide` | ✅ Approval → file exists under `docs/` |
| `write src/x.py` | ✅ Blocked outside allowed scope |
| Start with user scope `src/**`, switch to architect, write `src/plan.md`, then `PLAN.md` and `src/x.py` | ✅ Only `src/plan.md` is in both scopes; the other writes are blocked |
| In architect, ask it to use shell or a custom/MCP tool to write source | ✅ Unbounded execution tool is blocked/unavailable while the persona scope is active |
| Tab cycling | ✅ …debug → docs-writer → code… |

## 11. Sticky persona models (needs a second configured model)

| Do | Expected |
|---|---|
| `alysis config set role_models.planner <other-model>` → `/persona architect` | ✅ `/status` model row and footer show `<other-model>` |
| `/persona code` | ✅ Model back to your default |

## 12. Kill switch

| Do | Expected |
|---|---|
| `ALYSIS_PERSONA_MODES=off alysis chat` | ✅ Badge shows only `safe` (no persona half). `/persona ask` → `Persona modes are disabled…`. Tab does nothing. `/status` has no persona row. (Known cosmetic: the welcome hint still mentions tab) |

## 13. One-shot `run`

| Command | Expected |
|---|---|
| `alysis run --persona ask "what does this repo do?"` | ✅ Answer; no files touched |
| `alysis run --yes --persona architect "write ARCH.md describing the layout"` | ✅ `ARCH.md` created; a `.py` write in the same run would be scope-blocked |
| `alysis run --persona wizard "hi"` | ✅ `Unknown persona: wizard`, exit code 2 |
| `/persona wizard` (in chat) | ✅ `Invalid persona.` hint listing builtins + custom note |

---

Quick smoke (about 5 minutes): §2 rows 1–3, §3 rows 2–3, §5 row 1, §6 rows 1–2, §9.
Run the full script for release validation.
