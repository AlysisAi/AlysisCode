# Verification grammar and local failure codes

`computer_verify` retains two closed argument forms: `{"operation":"propose","predicate":...}` and exactly `{"operation":"evaluate"}`. Evaluation cannot include a predicate, even `null`. A proposal cannot supply expectations, task IDs, proof flags, or extra fields. The provider schema now uses explicit alternatives preserved by the subscription schema sanitizer, replacing an `if`/`then` condition that the sanitizer discarded. Local and host validation remain unchanged. Runtime length and string-safety checks remain authoritative where provider schema lowering omits constraints.

The protected turn controller captures only a finite code from the current tool result before sensitive-result redaction. If that same step reaches terminal host failure after bounded recovery, it emits the code below as a nonrecoverable surface error immediately before the existing generic host-failure event. The IDE adds its actual session/job binding; the app admits only that exact active binding. Messages, arguments, predicates, page content, and arbitrary error values are not copied into this diagnostic.

| Tool/result rejection | Terminal diagnostic |
| --- | --- |
| verify / invalid_verification_proposal | computer_use_verification_arguments_invalid |
| verify / invalid_host_response | computer_use_verification_response_invalid |
| state, action, wait / invalid_host_response | computer_use_observation_response_invalid |
| state, action, wait / invalid_host_workflow | computer_use_workflow_response_invalid |
| exhausted fresh-observation guard | computer_use_fresh_observation_required |

Unknown errors remain generic. A successfully recovered step emits no local failure code, and its code cannot contaminate a later error. Success, cancellation, and approval refusal keep their existing behavior. A diagnostic does not certify completion, infer no effect, or authorize a retry.

The observed installed N09 failure has no retained tool trace, so its exact cause is unproven. Valid app verification responses are compatible with the engine's version/status checks. The prior advertised schema did permit a predicate on evaluate even though runtime validation rejected it; this fixes that concrete inconsistency without claiming it caused N09. Mock-provider turn tests cover local failure, recovery, privacy, and unknown fallback; JSON Schema tests exercise the production sanitizer against the local validator. These tests do not qualify live model behavior.
