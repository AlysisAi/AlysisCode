# Selected-file upload objective evidence

The engine can finish one previously initiated selected-file upload only from the actual `computer_verify` **evaluate** response. Main must first admit the original user objective and exact task scope, then configure `computer_use_host.upload_objective` as the closed `{version:1, objective_digest, task_scope_digest}` binding. Both hashes use `sha256:` plus 64 lowercase hex characters. This scope is separate from the controller lease's `scope_digest`. It is never inferred from a response, model text, filename, page status or remote hash.

The engine advertises `features.computer_use_host.upload_objective_evidence_version:1` separately from upload and remote-evidence support. Main must check the exact bit and both upload/verification tool families before supplying the binding. The Python IDE parser copies it into a frozen value before session callbacks and echoes it unchanged from `session.create`; direct trusted `create_session` callers use `computer_use_upload_objective`. Omission preserves the disclosure-only behavior. Present malformed/null bindings or missing tool families refuse. Session clear, restart or a new session does not reconstruct initiated upload IDs from saved status.

The only admitted result is the closed envelope defined by the app's `shared/computerUseUploadObjective.ts`:

```json
{"verification":{"version":1,"status":"verified","reason":null,"evidence_scope":"selected_file_upload_readback","upload_objective":{"version":1,"upload_id":"upload_id","objective_digest":"sha256:...","task_scope_digest":"sha256:...","result_id":"result_id","result_digest":"sha256:..."}}}
```

Main emits it only after its original-objective candidate, fresh independent read, durable terminal/result publication and exact current-owner checks succeed. The engine compares both initial hashes and the full exact upload ID; it does not interpret the hashes as remote or business proof. Even a valid-looking envelope returned by a status tool, a propose call or a model argument cannot clear completion. A status-only ID that this session did not initiate is ineligible. Result IDs are one-use and bounded to 64 per session; tracked uploads are also bounded to 64.

An explicitly configured original upload objective remains incomplete before any upload starts: state-only calls and generic verification cannot satisfy it. This does not invent an artifact or uncertainty and leaves legacy sessions without that binding unchanged. Only that upload's pending bit clears. Unknown starts, setup, other uploads, download artifacts and existing workflow barriers retain their independent aggregate gates. The public upload status still requires `objectiveVerified:false`: remote acknowledgement, byte identity and effect verification do not acquire original-objective meaning through status reads. A subsequent status read remains conservative and cannot reuse an already consumed result ID to clear a pending bit.

For objective-enabled verification, the actual Python IDE retains the original host, agent/client/registry/config, route, destination and job across the exchange. It repeats the existing destination attestation and pure captured ownership/route checks after the response. Cancellation, closed/replaced owner, changed route, wrong nonce/operation/session, or expired request refuse before the tool can adopt completion. No deadline, lease, mutation authority or retry is created. Existing budget-bound session setup keeps provider metadata discovery offline; this change does not loosen provider-attempt accounting.

## Validation

Use the matching app checkout containing the shared validator and `tests/computer-use-upload-objective-host.test.mjs`:

```sh
PYTHONPATH=src:/path/to/runtime/site-packages ALYSIS_APP_SOURCE=/path/to/app ALYSIS_TEST_NODE=/path/to/node python -m pytest -q tests/test_computer_use_upload_objectives.py tests/test_computer_use_upload_objective_app_contract.py tests/test_ide_upload_objective.py tests/test_computer_use_uploads.py tests/test_computer_use_files.py tests/test_computer_use_verification.py tests/test_ide_computer_use_host.py
```

Coverage includes actual TS/Python schema and optional host-binding parity, actual `session.create` with provider callback mutation, real IDE response ownership/expiry/cancellation, scripted AgentSession successful and refused finalization, wrong/prefix IDs and scopes, unknown/other-file/compound-task barriers, and one-use evidence. The cross-repository test runs the real app controller/supervisor/budget fixture (including renewal and charged independent read), then feeds its exact result into a real AgentSession. Its main terminal port is controlled; that test qualifies transport/result behavior, not an actual upload or the host's original-objective admission.

A dedicated regression reproduced a route change from the final ModelRegistry callback after the route's original scalar values were read. The final pure route/client ownership check refuses that case. Production main admission, native remote proof and visible terminal publication remain separately qualified app responsibilities; this engine capability alone does not expose them.
