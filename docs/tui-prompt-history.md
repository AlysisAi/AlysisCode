# TUI prompt history

Press Up at the first composer row to recall accepted prompts. Down at the last
row advances history and restores the unsent draft, cursor, pastes and attachments.
Ctrl+R opens a searchable history picker. Type words, use Up/Down, then Enter to
recall a prompt into the composer. Enter again sends it; Escape cancels search.
Reasoning summary expansion uses Alt+R and is inactive in modals.

History belongs to the current OS account's Alysis data profile, including an
`ALYSIS_DATA_DIR` override, and is separated by workspace. Provider sign-in does
not separate history within that profile. The newest 200 entries, up to 2 MiB,
survive restart; an oversized entry is omitted. Classic chat history is separate
and is not imported. A warning reports persistence failures, and a read-only
profile retains history only in memory.

Accepted prompts, steering and queued messages are stored. Slash commands,
searches, rejected submissions and abandoned drafts are excluded. Running a queued
message does not record it again. Consecutive identical entries collapse for
recall, while every explicit submission still runs. Shift+Left separately edits
the queue; accepting an edit keeps both versions in history.

Recalled pastes retain their contents. Attachments retain paths rather than file
copies, so they refer to the files' current contents. A missing attachment blocks
submission until it is restored or removed from the composer.

History contains prompt text and file paths. To clear persisted history, close
the TUI and delete `prompt_history/<workspace-hash>.json` from the data profile.
