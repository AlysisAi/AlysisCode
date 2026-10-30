# Host upload tool contract

The engine offers `computer_upload({element_id})` and
`computer_upload_status({upload_id})` only when the authenticated host binding
includes the complete upload pair. `capabilities.upload_tools` is independent of
`file_tools`. The exact ordered host sets are the base tools, optionally followed
by verification, the download pair, and the upload pair, in that order. Partial,
reordered, or unknown sets are refused; legacy bindings omit both optional pairs.

The model supplies only an observed native input ID or an opaque host-issued
upload ID. Paths, bytes, filenames, URLs, snapshots, grants, and approval tokens
are not model arguments. The wrapper supplies its current trusted snapshot to an
upload, consumes that snapshot before dispatch, and does not restore page
authority from upload or status results. Upload IDs remain available in transient
tool context for status queries and are scrubbed from persisted unprivileged
activity and conversation data.

## Approval and projection

An upload uses the existing protected prepare/approval/execute exchange. A host
setup response returns immediately without an execute request. A valid upload
challenge always requires an ordinary explicit user decision, including when a
host marks the challenge optional. The original host deadline is preserved;
denial, cancellation, expired review, and wrong or late session/job/tool-call
responses do not authorize execution. A model-provided token or phase cannot
bypass this exchange.

The closed `upload_result` projection is either `needs_user_setup` with no upload,
or the status shape accepted by
`alysis_code.agent.computer_use_uploads.validate_upload_result`. It has
bounded byte counts (at most 8 MiB) and one of `granted`, `staging`, `staged`,
`release_prepared`, `release_unknown`, `page_disclosed`, `not_disclosed`, or
`uncertain`. Receiver acknowledgement and remote byte verification are retained
host evidence: acknowledgement requires `page_disclosed`, and remote byte
verification additionally requires acknowledgement. In the `page_disclosed`
phase, only remote byte verification permits `requiresReconciliation` to be false;
a true value remains valid
when the exact-effect admission has not been acknowledged. `objectiveVerified`
remains literal false. Unknown fields and inconsistent phase/byte/reconciliation claims fail
closed. The existing reserved workflow envelope is validated separately; it does
not expand the upload projection or supply file authority.

The engine advertises `features.computer_use_host.upload_remote_evidence_version:1`.
The host must negotiate that exact bit with the actual current IDE runtime before
enabling stronger remote projections. Older engines recognize only false remote
flags and retain the existing disclosure-only path. This compatibility bit does
not issue verification evidence, file authority, or original-objective approval.

## Completion limits

Upload status alone cannot establish objective completion. `page_disclosed`
means native file assignment: page event handlers
may already have received bytes. The host must separately verify receiver/object
identity and authorized readback before projecting those stronger flags. Even
matching remote bytes and reconciled upload effects do not prove the original
objective. Completion requires a matching `computer_verify` evaluation under the
negotiated `upload_objective` binding described in the [IDE protocol](ide_protocol.md).
A status read never initiates verification or retries disclosure. Uncorrelated
failures remain uncertain, and an unrelated status cannot clear them.

The existing terminal file guard aggregates download and upload state. It emits
`computer_use_file_setup_required` for pending setup and
`computer_use_file_delivery_unverified` for unresolved effects. A model's final
success prose is suppressed when that guard fails. The host supplies the source
picker, upload implementation and receiver verification.
