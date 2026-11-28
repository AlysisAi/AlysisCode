# Connect an AI subscription

Alysis Code can use a provider API key or sign in through a supported subscription.
Both use the native agent, tools and session history. The connection determines
authentication, available models and provider request settings.

## First-run setup

Start Alysis Code in your workspace:

```bash
cd /path/to/project
alysis
```

Choose **Use an AI subscription**, select the connection and complete browser
sign-in. Then open **`/config` → Default Model** to choose a model and reasoning
effort. Setup saves that pair in the active profile.

If sign-in is skipped or fails, the TUI still opens for configuration and help.
Model requests remain blocked until the account is connected and a model is
selected. Existing API-key profiles remain saved.

## ChatGPT subscription

Use **Connect → ChatGPT subscription → Continue with ChatGPT** in the desktop app,
or use the CLI:

```bash
alysis auth login openai-codex
alysis config menu
```

The `openai-codex` connection ID and `chatgpt-codex` profile name are retained for
compatibility. The current adapter uses browser sign-in for Alysis Code. Its
`--device-code` option is unsupported. Under WSL, the browser callback must reach
the engine's loopback listener; check localhost forwarding if it cannot return.

The browser lets you choose an account and grant plan-use permission. Alysis
checks the callback and account identity before replacing an existing connection.
If plan use is declined or unavailable, choose an API-key connection or complete
sign-in again. See OpenAI's [registration and sign-in guide](https://developers.openai.com/siwc/token-sharing-open-source/sign-in).

Old fixed-client credentials require a new Alysis sign-in. Do not copy tokens from
another application into Alysis configuration.

Inside the TUI, `/login` opens the connection picker. Account commands are:

```bash
alysis auth list
alysis auth status openai-codex
alysis auth login openai-codex --switch-account
alysis auth logout openai-codex
```

## Choose a model and reasoning effort

Use **`/config` → Default Model** to select the subscription model and its effort
together. The picker reflects the connected account and the selected model's
capabilities. Automatic leaves effort selection to the provider; an explicit
choice must be supported for that model.

Raw `model`, `llm_reasoning_effort` and `base_url` edits are rejected while a
subscription profile is active. Changing model access recreates the session so
clients from different connections are not mixed. API-key profiles retain their
separate configuration and billing path.

ChatGPT plan requests use `https://api.openai.com/v1/responses`, with streaming
enabled and `store:false`. They do not use the old ChatGPT `backend-api` URL.
The client waits for the terminal completion event before accepting a successful
response. See OpenAI's [models and inference guide](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).

Model availability depends on the selected account and current provider access.
A model-picker entry is not a guarantee that the account can run it.

## Credentials and diagnostics

Subscription credentials live in the encrypted provider vault, separate from
`config.json`. Authorization headers are restricted to the adapter's allowed
destinations. Signing out clears active tokens while retaining registration
identity for reconnecting.

The vault uses the OS keyring when available, with DPAPI on Windows or a private
key-file fallback when needed. To inspect the current context without changing
credentials, run:

```bash
alysis auth status openai-codex --json
alysis auth list --json
alysis doctor auth --json
```

Status exit code 0 means the command ran; read `authenticated` in the JSON to
determine whether the account is connected. The auth doctor reports resolved
storage paths and keyring/fallback availability, which can differ between a
terminal and a GUI-launched process. Never commit credential stores or include
tokens in logs or prompts.

## Use the native agent

After sign-in and model selection, use the normal commands:

```bash
alysis run --mode readonly "Explain this repository."
alysis run --mode review "Review the current diff."
alysis chat --mode auto
```

Subscription-backed sessions retain the normal tool and extension surface.
Unavailable role-model overrides fall back to the selected account model.

## Add another subscription adapter

Setup and `/config` discover connections through the provider-auth registry.
Each provider needs a trusted adapter for sign-in, refresh, allowed destinations,
model discovery and request format. Adding a profile alone cannot connect a
subscription that the provider does not support for external applications.
