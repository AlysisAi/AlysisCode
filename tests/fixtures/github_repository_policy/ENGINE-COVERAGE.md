# ENGINE literal corpus coverage — #7 follow-up

Engine base: fb9b3f70. Frozen corpus SHA256: 6cf983787d07e2da41229e44a54beb656160b586410c2789d2cff5d66041c3c6.

The original 108 cases plus 27 newly mapped cases exercise production ENGINE boundaries. Another 20 new rows inject a private policy replacement at a real broker wait: these are guard-only tests, not production-reachable retirement delivery. The remaining 38 rows are APP-only. Thus 135 production-boundary literal projections + 20 synthetic owner guards + 38 APP-only = 193; this is not 193 paired end-to-end conformance cases.

## Boundary mapping and evidence correction

- Native schema negatives mutate the registry schema; they exercise the production pin refusal without inventing a preimage for the synthetic digest.
- Policy/account/workspace retirement in prepare, approval and result waits uses direct assignment to the private session object. Production assigns that object only at session.create. These 20 rows prove the guard under injected owner drift only. APP must prove its actual cancellation/close/recreation and final provider guard; no new update API is implied.
- Installed-descriptor drift edits actual temporary installed bytes. Stop uses session.cancel; deadline uses the broker timeout. These are production ENGINE boundaries.
- Discover/describe replies mint real runtime refs. A second discovery invalidates prior refs; effective schema and wrong runtime hashes exercise real refusal paths. Same-name remote provenance remains APP-only.
- Person denial traverses approval.respond. The exact APP admission/final transport checkpoints remain APP-only.
- Even a real emitted host request proves no provider effect. The injected post-dispatch rows prove unknown/no-retry behavior under synthetic owner loss only.
- The three original non-corpus test_policy_retirement_during_real_wait_never_retries cases are also synthetic-owner guard tests; they are not included in the 108 literal cases.

## Complete remaining-85 ledger

| Literal case | Consumer / boundary |
| --- | --- |
| `G-list-changed-schema` | native_schema — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-list-policy-before_admission` | app_only — APP-only exact before_admission checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-list-policy-after_setup_await` | prepare_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-list-policy-after_approval` | app_only — APP-only exact after_approval checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-list-policy-before_dispatch` | app_only — APP-only exact before_dispatch checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-repo-read-changed-schema` | native_schema — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-repo-read-policy-before_admission` | app_only — APP-only exact before_admission checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-repo-read-policy-after_setup_await` | prepare_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-repo-read-policy-after_approval` | app_only — APP-only exact after_approval checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-repo-read-policy-before_dispatch` | app_only — APP-only exact before_dispatch checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-pr-list-changed-schema` | native_schema — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-pr-list-policy-before_admission` | app_only — APP-only exact before_admission checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-pr-list-policy-after_setup_await` | prepare_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-pr-list-policy-after_approval` | app_only — APP-only exact after_approval checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-pr-list-policy-before_dispatch` | app_only — APP-only exact before_dispatch checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-pr-create-changed-schema` | native_schema — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-pr-create-policy-before_admission` | app_only — APP-only exact before_admission checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-pr-create-policy-after_setup_await` | prepare_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-pr-create-policy-after_approval` | approval_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-pr-create-policy-before_dispatch` | app_only — APP-only exact before_dispatch checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-pr-create-person-denied` | person_denial — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-git-fetch-changed-schema` | native_schema — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-git-fetch-policy-before_admission` | app_only — APP-only exact before_admission checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-git-fetch-policy-after_setup_await` | prepare_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-git-fetch-policy-after_approval` | approval_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-git-fetch-policy-before_dispatch` | app_only — APP-only exact before_dispatch checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-git-fetch-person-denied` | person_denial — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-git-pull-changed-schema` | native_schema — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-git-pull-policy-before_admission` | app_only — APP-only exact before_admission checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-git-pull-policy-after_setup_await` | prepare_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-git-pull-policy-after_approval` | approval_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-git-pull-policy-before_dispatch` | app_only — APP-only exact before_dispatch checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-git-pull-person-denied` | person_denial — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-git-push-changed-schema` | native_schema — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-git-push-policy-before_admission` | app_only — APP-only exact before_admission checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-git-push-policy-after_setup_await` | prepare_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-git-push-policy-after_approval` | approval_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-git-push-policy-before_dispatch` | app_only — APP-only exact before_dispatch checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-git-push-person-denied` | person_denial — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-changed-schema` | app_only — APP-only raw local source schema/pin; engine receives effective schema plus runtime hash, not raw adapter source. |
| `G-rest-root-policy-before_admission` | app_only — APP-only exact before_admission checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-rest-root-policy-after_setup_await` | prepare_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-rest-root-policy-after_approval` | app_only — APP-only exact after_approval checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-rest-root-policy-before_dispatch` | app_only — APP-only exact before_dispatch checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-rest-branches-changed-schema` | app_only — APP-only raw local source schema/pin; engine receives effective schema plus runtime hash, not raw adapter source. |
| `G-rest-branches-policy-before_admission` | app_only — APP-only exact before_admission checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-rest-branches-policy-after_setup_await` | prepare_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-rest-branches-policy-after_approval` | app_only — APP-only exact after_approval checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-rest-branches-policy-before_dispatch` | app_only — APP-only exact before_dispatch checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-rest-pr-create-changed-schema` | app_only — APP-only raw local source schema/pin; engine receives effective schema plus runtime hash, not raw adapter source. |
| `G-rest-pr-create-policy-before_admission` | app_only — APP-only exact before_admission checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-rest-pr-create-policy-after_setup_await` | prepare_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-rest-pr-create-policy-after_approval` | approval_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-rest-pr-create-policy-before_dispatch` | app_only — APP-only exact before_dispatch checkpoint inside main admission/final transport; no matching engine await. Engine post-prepare/person waits tested separately. |
| `G-rest-pr-create-person-denied` | person_denial — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-repo-read-stale-account_link_revision` | binding_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-repo-read-stale-plugin_manifest_sha256` | binding_retirement — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-repo-read-stale-generation` | app_only — APP-only captured owner generation/job identity; not a renderer/model-supplied engine mutation. |
| `G-repo-read-stale-job_id` | app_only — APP-only captured owner generation/job identity; not a renderer/model-supplied engine mutation. |
| `G-repo-read-stale-workspace_repository` | binding_retirement — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-repo-read-cancelled` | interrupt_prepare — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-repo-read-deadline` | interrupt_prepare — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `H-list-human-browse-outside` | app_only — APP-only trusted human IPC entry; no engine route may select it. |
| `G-repo-read-all-mode-old-peer` | app_only — APP-only negotiation of stored all with an old engine; current engine cannot act as an old peer. |
| `G-rest-root-remote-get_file_contents` | remote_name — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-remote-search_repositories` | remote_name — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-remote-graphql` | remote_name — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-remote-github_api_read` | app_only — APP-only source provenance: same-name/same-schema remote impostor is indistinguishable on the engine wire. |
| `G-rest-root-runtime-schema_match` | runtime_refs — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-runtime-current` | runtime_refs — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-effective-schema-changed` | effective_schema — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-control-157` | discovery_control — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-control-158` | discovery_control — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-control-159` | discovery_control — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-control-160` | discovery_control — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-post-dispatch-revocation` | post_dispatch — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-list-list-partial-failure` | app_only — APP-only iteration/partial failure between exact repository metadata HTTP calls. |
| `G-rest-root-describe-revoked-after-verification` | post_dispatch — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-git-push-changed-local_branch` | app_only — APP-only isolated Git snapshot/recheck and canonical network target. |
| `G-git-push-changed-local_commit` | app_only — APP-only isolated Git snapshot/recheck and canonical network target. |
| `G-git-push-changed-origin` | app_only — APP-only isolated Git snapshot/recheck and canonical network target. |
| `G-list-account-change-after-proof` | app_only — APP-only live account proof/status await, before repository metadata port. |
| `G-rest-pr-create-write-post-dispatch-revocation` | post_dispatch — Guard-only: test replaces the private session policy at a real await. Production has no policy-update route; APP retirement/cancel/close/recreation must be tested separately. |
| `G-rest-root-describe-workspace` | discovery_control — ENGINE real producer; APP transport/account/credential counts remain separate. |
| `G-rest-root-describe-repeated` | discovery_control — ENGINE real producer; APP transport/account/credential counts remain separate. |
