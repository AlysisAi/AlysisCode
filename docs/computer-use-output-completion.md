# Completion evidence for local output files

A GUI application can save a requested file without producing a `fs_write` or
shell-command receipt. The local engine now observes exact local output paths
from the user's acceptance contract before work starts and again at completion.
This is independent of the application, model, and writing tool.
Workspace-relative and absolute paths map to the same exact contract locus. This
also covers GUI saves inside an already-untracked directory whose Git status does
not change when a new file appears; unrelated files never become output evidence.

`ArtifactOutputObserver` reads regular-file metadata only. It uses the existing
action approval authority (`fs_read`). Workspace paths use ordinary read admission;
external paths request permission for the baseline and final observations of the
named output during this turn. It reapplies current
deny rules on every observation. A denied or unavailable check leaves the output
unverified. Protected host-only tool sessions do not receive this local capability.
The observer rejects network/device paths, sensitive paths, symlinks, Windows
reparse points, and directories. It does not enumerate the output directory or
read file contents.

The evidence distinguishes two claims:

- A freshly observed regular file satisfies the required-output **existence**
  criterion.
- A new file, or a changed metadata stamp compared with the turn's baseline,
  supplies evidence for that exact named output locus. An unchanged old file
  does not establish that the requested change happened.

Neither observation proves correct contents, a valid format, a successful image
transformation, or any other behavioral requirement. Those checks remain separate.
Metadata changes can also come from another process; this evidence does not
attribute a write to the agent. The observer never accepts an app's success text,
an MCP result path, or a screenshot as a filesystem receipt.

Evidence is bound to the task and current edit generation and refreshed at
finalization, including interactive turns without repository repair loops. Missing
or newly inaccessible files revoke the current observation. Runtime observers and
their permission scopes are not restored from serialized acceptance payloads.

Quoted absolute output paths retain spaces, Unicode, and clause-like punctuation.
An ambiguous unquoted path is not expanded by guessing a filename.

Regression coverage: `tests/test_artifact_output_observation.py`, acceptance
contracts, turn-contract expectations, completion certificates, and the real
agent-loop fixture with both successful and falsely acknowledged external and
workspace saves.
