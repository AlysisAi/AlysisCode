# Protected call diagnostics

The retained October 13 operation `e425d20a` proves native text entry followed by
`computer_use_host_failure`. It does not retain the later tool name, result code,
or exception. No specific later cause can be reconstructed from that record.

Protected turns now emit `computer_use_call_diagnostic` events at call begin,
trusted host request/return, local result, bounded exception-chain sites, and
terminal host failure. The ordinal is the turn's bounded step number; Computer
Use dispatch is already serialized to one call per step. Opaque call IDs travel
on the existing authenticated protocol only for correlation. The app retains
them in memory and writes only the ordinal to its closed stage ledger.

Local boundaries distinguish argument validation, host request, returned host
result, snapshot validation, observation assertions, image projection, and
result copying. Result errors are a finite vocabulary; other errors become
`other`. Exception diagnostics include at most four class/relation/source-site
records. Only already allowlisted shipped source identities and bounded line
numbers survive. Messages, arguments, values, screenshots, paths, and arbitrary
exception attributes never enter the diagnostic payload. Context state resets
after each call, including failure. Diagnostic sink failure cannot fail a task.

These events do not authorize retries, rebinding, approvals, dispatch, or
successful completion. `returned` means the local tool returned without a
top-level error, not that the user's goal was met. Recovered errors do not gain
terminal authority. Existing host, recovery, cancellation and final-state rules
remain authoritative. Tests simulate successful input followed by a separate
local rejection or exception; they are not evidence of the live failure's cause.
