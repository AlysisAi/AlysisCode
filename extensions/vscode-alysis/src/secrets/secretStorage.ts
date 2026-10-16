export const ALYSIS_API_KEY_SECRET = "alysis.apiKey";
export const ALYSIS_PROFILE_KEY_INDEX_SECRET = "alysis.apiKey.profiles";
export const ALYSIS_PROFILE_KEY_PREFIX = "alysis.apiKey.profile:";

// A profile name is a CLI config identifier, never free text from a provider response. Keep the
// charset tight so a name can never smuggle a different secret key out of the namespace.
const PROFILE_NAME_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}$/i;
// Environment variable names are forwarded verbatim into the spawned CLI's environment, so the
// charset is restricted to what a POSIX/Windows env var may legally be.
const ENV_VAR_PATTERN = /^[A-Z_][A-Z0-9_]{0,63}$/i;
const MAX_INDEXED_PROFILES = 64;

export interface SecretStorageLike {
  get(key: string): PromiseLike<string | undefined>;
  store(key: string, value: string): PromiseLike<void>;
  delete(key: string): PromiseLike<void>;
}

/** One stored provider credential, resolved for forwarding into the bridge process environment. */
export interface ProviderCredential {
  profile: string;
  envVar: string;
  value: string;
}

export function isStorableProfileName(value: string): boolean {
  return PROFILE_NAME_PATTERN.test(value.trim());
}

export function isStorableEnvVarName(value: string): boolean {
  return ENV_VAR_PATTERN.test(value.trim());
}

// Per-provider credentials, stored one secret per profile plus a small index recording which env
// var each profile's key must be exported as.
//
// The index is required because VS Code SecretStorage cannot enumerate its own keys, and the env
// var mapping is required because the CLI's resolve_api_key() prefers *profile-scoped* sources:
// for any profile other than "default"/"openai" it ignores ALYSIS_API_KEY entirely and will
// only accept a key from its own credential store or from the profile's declared api_key_env.
// Storing just one global key therefore leaves most providers reading as unconfigured.
//
// The legacy single-key secret is kept and still honored so existing installs keep working.
export class AlysisSecretStore {
  public constructor(
    private readonly storage: SecretStorageLike,
    private readonly secretKey = ALYSIS_API_KEY_SECRET
  ) {}

  public getApiKey(): PromiseLike<string | undefined> {
    return this.storage.get(this.secretKey);
  }

  public setApiKey(value: string): PromiseLike<void> {
    const trimmed = value.trim();
    if (trimmed.length === 0) {
      throw new Error("API key cannot be empty.");
    }
    return this.storage.store(this.secretKey, trimmed);
  }

  public deleteApiKey(): PromiseLike<void> {
    return this.storage.delete(this.secretKey);
  }

  public async getProfileApiKey(profile: string): Promise<string | undefined> {
    const name = normalizeProfileName(profile);
    if (!name) {
      return undefined;
    }
    const stored = await this.storage.get(profileSecretKey(name));
    return stored && stored.trim().length > 0 ? stored : undefined;
  }

  /**
   * Store a provider key for one profile. `envVar` is the variable the CLI profile declares as its
   * api_key_env; an empty value still forwards the key through the profile-scoped IDE override.
   */
  public async setProfileApiKey(profile: string, value: string, envVar = ""): Promise<void> {
    const name = normalizeProfileName(profile);
    if (!name) {
      throw new Error("Provider profile name is not valid.");
    }
    const trimmed = value.trim();
    if (trimmed.length === 0) {
      throw new Error("API key cannot be empty.");
    }
    const normalizedEnv = normalizeEnvVarName(envVar);
    const index = await this.readIndex();
    if (!(name in index) && Object.keys(index).length >= MAX_INDEXED_PROFILES) {
      throw new Error("Too many stored provider keys. Remove one before adding another.");
    }
    await this.storage.store(profileSecretKey(name), trimmed);
    index[name] = normalizedEnv;
    await this.writeIndex(index);
  }

  public async deleteProfileApiKey(profile: string): Promise<void> {
    const name = normalizeProfileName(profile);
    if (!name) {
      return;
    }
    await this.storage.delete(profileSecretKey(name));
    const index = await this.readIndex();
    if (name in index) {
      delete index[name];
      await this.writeIndex(index);
    }
  }

  /** Profile names that currently have a stored key, in index order. */
  public async listProfilesWithKeys(): Promise<string[]> {
    return Object.keys(await this.readIndex());
  }

  /**
   * Every stored provider credential, for forwarding into the bridge's profile-scoped overrides.
   *
   * All of them are forwarded, not just the active profile's: the CLI resolves the key for whichever
   * profile is active at request time, and the active profile can change (profile.use) without
   * restarting the bridge. Forwarding only the profile that was active at spawn time would make a
   * switched-to provider read as unconfigured until the next restart. A missing or invalid vendor
   * env name omits only that alias; custom providers still receive their own scoped key. Unreadable
   * or empty keys are skipped rather than forwarded blank.
   */
  public async listProviderCredentials(): Promise<ProviderCredential[]> {
    const index = await this.readIndex();
    const credentials: ProviderCredential[] = [];
    for (const [profile, envVar] of Object.entries(index)) {
      const value = await this.getProfileApiKey(profile);
      if (!value) {
        continue;
      }
      credentials.push({ profile, envVar, value: value.trim() });
    }
    return credentials;
  }

  /**
   * The key to use for a profile: its own stored key first, then the legacy global key. Mirrors the
   * CLI's precedence so the extension's "connected" badge cannot disagree with what the agent reads.
   */
  public async resolveApiKey(profile?: string): Promise<string | undefined> {
    const scoped = profile ? await this.getProfileApiKey(profile) : undefined;
    if (scoped) {
      return scoped;
    }
    const legacy = await this.getApiKey();
    return legacy && legacy.trim().length > 0 ? legacy : undefined;
  }

  private async readIndex(): Promise<Record<string, string>> {
    const raw = await this.storage.get(ALYSIS_PROFILE_KEY_INDEX_SECRET);
    if (!raw) {
      return {};
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return {};
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const index: Record<string, string> = {};
    for (const [profile, envVar] of Object.entries(parsed as Record<string, unknown>)) {
      const name = normalizeProfileName(profile);
      if (!name || Object.keys(index).length >= MAX_INDEXED_PROFILES) {
        continue;
      }
      index[name] = typeof envVar === "string" ? normalizeEnvVarName(envVar) : "";
    }
    return index;
  }

  private writeIndex(index: Record<string, string>): PromiseLike<void> {
    if (Object.keys(index).length === 0) {
      return this.storage.delete(ALYSIS_PROFILE_KEY_INDEX_SECRET);
    }
    return this.storage.store(ALYSIS_PROFILE_KEY_INDEX_SECRET, JSON.stringify(index));
  }
}

function normalizeProfileName(value: string): string {
  const trimmed = String(value ?? "").trim();
  return isStorableProfileName(trimmed) ? trimmed : "";
}

function normalizeEnvVarName(value: string): string {
  const trimmed = String(value ?? "").trim();
  return isStorableEnvVarName(trimmed) ? trimmed.toUpperCase() : "";
}

function profileSecretKey(profile: string): string {
  return `${ALYSIS_PROFILE_KEY_PREFIX}${profile}`;
}
