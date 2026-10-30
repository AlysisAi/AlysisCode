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
is stored in the OS keychain via `keyring` when possible. On Windows without keyring, Alysis Code uses
DPAPI for the local master key. On macOS and Linux without a working keychain, Alysis Code generates a
cryptographically random 256-bit master key in a per-store sibling `.key` file. Both the encrypted
token envelope and filesystem key are written atomically with owner-only permissions (`0600`) where
POSIX mode bits are supported. Filesystem-key storage protects against disclosure of the token file
alone, but it is not equivalent to an OS keychain: a process that can read all files as the same user
can read the key as well.

Hosts that need a stable filesystem key can set
`ALYSIS_OAUTH_KEY_SOURCE=filesystem-random`. Other explicit values are rejected.
This override uses the same random-key storage described above; it does not
protect against a process that can read both the vault and its key file.

When this override is selected, an accessible macOS keyring envelope is migrated
to the filesystem key. If the old key cannot be read, the store refuses the
operation rather than replacing an unreadable vault. Migration and key rotation
are atomic, so a failed rewrite preserves the last readable store.

Envelope version 2 introduced the random filesystem key. Version 1
`weak-derived-fallback` envelopes remain readable and are re-encrypted automatically with the best
available version 2 key source after a successful decrypt. The legacy deterministic derivation is
never used for new credential writes.

Legacy plaintext `mcp_oauth_tokens.json` files remain readable for one release as a migration bridge.
On first successful read, Alysis Code rewrites the token store as an encrypted envelope. If that
encrypted rewrite fails, the legacy plaintext file is left intact so the user is not locked out.

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
