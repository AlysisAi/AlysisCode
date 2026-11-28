# Credentials

Alysis Code supports an explicit persisted API-key store separate from `config.json`.

API key resolution order:

1. Per-command override (`--api-key`, `--api-key-env`, `--api-key-stdin`)
2. `ALYSIS_API_KEY`
3. Persisted credentials (`alysis config set-api-key`)
4. `OPENAI_API_KEY`

Notes:

- Persisted credentials are stored in the user config directory at `credentials.json`.
- The main `config.json` still stores non-secret settings such as `model` and `base_url`.
- `alysis config show` reports whether an API key is available and which source won.
- `alysis setup` can save an API key into the local credentials store.

## OAuth token storage

MCP HTTP OAuth tokens use a separate user-scope store at `mcp_oauth_tokens.json` in the same config
directory. That file is an AES-GCM encrypted envelope containing `version`, `key_source`, `nonce`,
and `ciphertext`; it does not contain plaintext access tokens or refresh tokens. The AES master key
source is chosen in this order:

Hosts that need a stable filesystem key can set
`ALYSIS_OAUTH_KEY_SOURCE=filesystem-random`. Other explicit values are rejected.
This override uses the same random-key storage described above; it does not
protect against a process that can read both the vault and its key file.

When this override is selected, an accessible macOS keyring envelope is migrated
to the filesystem key. If the old key cannot be read, the store refuses the
operation rather than replacing an unreadable vault. Migration and key rotation
are atomic, so a failed rewrite preserves the last readable store.

`ALYSIS_OAUTH_KEY_SOURCE=filesystem-random` (legacy spelling `ALYSIS_OAUTH_KEY_SOURCE`) is the
only supported explicit override. Any other present value is rejected rather than silently selecting
another backend. A filesystem master key keeps tokens out of plaintext and prevents an
envelope-only copy from being decrypted, but it does not defend against a process that can read
both the vault and its adjacent user-only key file.

Envelope version 2 introduced the random filesystem key. Version 1 `weak-derived-fallback`
envelopes remain readable and are re-encrypted automatically with the current key source after a
successful decrypt; the legacy deterministic derivation is never used for new credential writes.
Legacy plaintext `mcp_oauth_tokens.json` files are rewritten as an encrypted envelope on first
successful read. An accessible macOS keyring envelope is likewise migrated to the filesystem source
once when that override is set. If the old key source is unavailable, Alysis Code fails
closed instead of replacing the unreadable vault. Migration and key rotation are atomic; failed rewrites do not replace the last
readable store.

AI-subscription access and refresh tokens use `provider_auth_tokens.json`. This is
separate from `config.json`, API-key credentials, and MCP token records, but uses
the same AES-GCM envelope/key-source machinery described above. Any legacy
plaintext provider-token payload is migrated immediately on a successful read.
Provider adapters refresh and rotate their own records and may attach them only
to their exact allowlisted destinations. `alysis auth logout <connection>`
removes the local record even when remote revocation is temporarily unavailable.

Useful commands:

```bash
alysis config set-api-key
alysis config clear-api-key
alysis config show
```
