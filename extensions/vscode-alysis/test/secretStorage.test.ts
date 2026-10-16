import assert from "node:assert/strict";
import test from "node:test";

import { ALYSIS_API_KEY_SECRET, SecretStorageLike, AlysisSecretStore } from "../src/secrets/secretStorage";

test("AlysisSecretStore stores, reads, and deletes API keys", async () => {
  const storage = new MemorySecretStorage();
  const secrets = new AlysisSecretStore(storage);

  await secrets.setApiKey("  test-key  ");
  assert.equal(await secrets.getApiKey(), "test-key");
  assert.equal(storage.keys()[0], ALYSIS_API_KEY_SECRET);

  await secrets.deleteApiKey();
  assert.equal(await secrets.getApiKey(), undefined);
});

test("AlysisSecretStore rejects empty API keys", async () => {
  const secrets = new AlysisSecretStore(new MemorySecretStorage());

  assert.throws(() => secrets.setApiKey("   "), /cannot be empty/);
});

test("per-provider keys are stored separately and survive switching providers", async () => {
  const storage = new MemorySecretStorage();
  const secrets = new AlysisSecretStore(storage);

  await secrets.setProfileApiKey("anthropic", " anthropic-key ", "ANTHROPIC_API_KEY");
  await secrets.setProfileApiKey("deepseek", "deepseek-key", "DEEPSEEK_API_KEY");

  assert.equal(await secrets.getProfileApiKey("anthropic"), "anthropic-key");
  assert.equal(await secrets.getProfileApiKey("deepseek"), "deepseek-key");
  assert.deepEqual((await secrets.listProfilesWithKeys()).sort(), ["anthropic", "deepseek"]);

  // Removing one provider's key must leave the other's intact — the single-key store made
  // connecting a second provider silently overwrite the first.
  await secrets.deleteProfileApiKey("deepseek");
  assert.equal(await secrets.getProfileApiKey("anthropic"), "anthropic-key");
  assert.equal(await secrets.getProfileApiKey("deepseek"), undefined);
  assert.deepEqual(await secrets.listProfilesWithKeys(), ["anthropic"]);
});

test("provider credentials preserve custom keys without a vendor environment alias", async () => {
  const secrets = new AlysisSecretStore(new MemorySecretStorage());

  await secrets.setProfileApiKey("anthropic", "anthropic-key", "anthropic_api_key");
  await secrets.setProfileApiKey("local", "local-key", "");

  assert.deepEqual(await secrets.listProviderCredentials(), [
    { profile: "anthropic", envVar: "ANTHROPIC_API_KEY", value: "anthropic-key" },
    { profile: "local", envVar: "", value: "local-key" }
  ]);
});

test("provider key storage rejects names that could escape the secret namespace", async () => {
  const secrets = new AlysisSecretStore(new MemorySecretStorage());

  await assert.rejects(() => secrets.setProfileApiKey("../other", "key", "X_API_KEY"), /not valid/);
  await assert.rejects(() => secrets.setProfileApiKey("has space", "key", "X_API_KEY"), /not valid/);
  await assert.rejects(() => secrets.setProfileApiKey("anthropic", "  ", "X_API_KEY"), /cannot be empty/);
});

test("an invalid env var name is dropped rather than forwarded to the CLI", async () => {
  const secrets = new AlysisSecretStore(new MemorySecretStorage());

  await secrets.setProfileApiKey("weird", "key", "not a var; export EVIL=1");

  assert.equal(await secrets.getProfileApiKey("weird"), "key");
  assert.deepEqual(await secrets.listProviderCredentials(), [{ profile: "weird", envVar: "", value: "key" }]);
});

test("resolveApiKey prefers the profile key and falls back to the legacy global key", async () => {
  const secrets = new AlysisSecretStore(new MemorySecretStorage());

  await secrets.setApiKey("legacy-key");
  assert.equal(await secrets.resolveApiKey("anthropic"), "legacy-key");

  await secrets.setProfileApiKey("anthropic", "scoped-key", "ANTHROPIC_API_KEY");
  assert.equal(await secrets.resolveApiKey("anthropic"), "scoped-key");
  assert.equal(await secrets.resolveApiKey("deepseek"), "legacy-key");
});

test("a corrupt profile index degrades to no stored providers instead of throwing", async () => {
  const storage = new MemorySecretStorage();
  await storage.store("alysis.apiKey.profiles", "{not json");
  const secrets = new AlysisSecretStore(storage);

  assert.deepEqual(await secrets.listProfilesWithKeys(), []);
  assert.deepEqual(await secrets.listProviderCredentials(), []);
});

class MemorySecretStorage implements SecretStorageLike {
  private readonly values = new Map<string, string>();

  public async get(key: string): Promise<string | undefined> {
    return this.values.get(key);
  }

  public async store(key: string, value: string): Promise<void> {
    this.values.set(key, value);
  }

  public async delete(key: string): Promise<void> {
    this.values.delete(key);
  }

  public keys(): string[] {
    return [...this.values.keys()];
  }
}
