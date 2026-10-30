# One admitted copy of one named external file

Prefer sufficient already-provided attachment text first. In readonly, shell staging is unavailable; permitted workspace inspection and supplied bounded PDF/text attachment content remain usable.

Use capability_status.file_staging.command exactly (the engine interpreter), through shell_run under normal policy:
<command> --source "/exact/user-named/file.pdf" --output "new-copy.pdf"
Quote each argument as shell data; never interpolate file text as code. A tilde path may be passed as a quoted argument; the helper expands only the current user's home. Do not invent paths or run a preliminary search/stat/copy chain.

The command starts only after its existing guard admits it. It opens the named regular source once, validates its identity, rejects symlink components/traversal and detects replacement during copying. The byte cap is 50 MiB before and during the copy. Output must be one new filename directly inside the current workspace. No overwrite, external mutation, parent-directory grant, new host capability, or workspace-root widening.
Check status, bytes, SHA-256, and the returned workspace path before inspecting the saved copy. A successful staging receipt proves a copy, not document understanding or current unsaved app state.

Review uses normal approval; auto and Full access retain existing rules, including explicit denies. Do not add a separate approval flow or treat the helper as a permission bypass. Denied folder access, unsupported platform, changed identity, oversized input, or destination conflict stops this attempt. Never retry with cp, another interpreter, screenshots, or a different access technique to evade a refusal. A destination conflict may be resolved only by choosing a new authorized filename.
