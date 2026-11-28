# ChatGPT plan usage in Alysis Code

Alysis is a local native agent. Its ChatGPT connection uses the documented Sign in with ChatGPT flow for locally hosted/open-source agents. API-key profiles remain a separate billing and authentication path.

Use **Connect → ChatGPT subscription → Continue with ChatGPT**. The system browser requests identity and direct plan-use permission. The engine starts a loopback listener on `127.0.0.1` before opening the browser, uses fresh PKCE/state/nonce and exchanges the callback code with the issued app client ID. It verifies the signed ID token against the issuer's published JWKS, including issuer, audience, expiry, nonce and subject, before activating credentials. The current validator accepts RS256 signing keys; an unknown algorithm fails closed.

Registration/host hints and credentials stay in the encrypted provider store. Switching accounts preserves the selected account until a replacement is validated and has plan permission. Sign-out clears all active tokens and retains only registration identity for reconnect. Failed or declined authentication does not silently switch to API-key billing. Legacy Codex credentials must be reauthorized for Alysis; their fixed-client identity cannot be reused for this route.

Models come from the account's public `/v1/models` catalog (entries with `visibility: list`). That answer lists only part of what a plan can use on this route, so the engine also offers each reviewed ChatGPT model (`model_catalog/chatgpt_codex_subscription_snapshot.json`) the answer does not mention, marked "access checked when used". This applies only when the answer already lists a reviewed full-tier (non-Luna) model; hidden entries stay hidden, failed discovery adds nothing, and a Luna-only catalog or one the review does not cover is not broadened. Inference uses public `/v1/responses`, with `store:false`, `stream:true`, full input history and local function tools in the `alysis` namespace. Alysis omits preview-unsupported parameters and rejects unsupported hosted tools. A completed text delta is not success; the Responses client requires the terminal completion event.

## Reasoning effort

The model picker exposes Automatic and the capabilities advertised for the selected model/account. An explicitly empty capability list disables overrides. When metadata is absent, only exact current OpenAI model IDs with documented choices receive fallback options. Unknown providers/models do not inherit another model's choices. `ultra` is not a Responses effort.

Each chat persists its choice and captures it in its operation/retry checkpoint. The app passes `ALYSIS_RUN_REASONING_EFFORT` (and the legacy spelling) only to that run's child process. The engine validates the actual selected model/profile, emits `reasoning.effort` for an explicit supported value and omits effort for Automatic. This does not rewrite saved global defaults. Read-only capability discovery is `alysis auth reasoning MODEL --profile PROFILE`.

## Validation boundary

Offline tests cover signed identity rejection, callback state, registration reuse, encrypted persistence, account preservation, refresh/CAS/account routing, plan-scope removal, catalog capabilities, stateless tool replay and actual mocked HTTP effort payloads. Desktop tests cover command isolation, persistence and lifecycle cleanup. These do not establish current plan eligibility, real browser consent or real provider inference. Those require attended sign-in in an isolated preview and a successful model request.

Official references, checked 2026-10-01:

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [Reasoning/model choices](https://developers.openai.com/api/docs/guides/deployment-checklist)
