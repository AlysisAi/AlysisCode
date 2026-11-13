import assert from "node:assert/strict";
import test from "node:test";

import { ALYSIS_API_KEY_SECRET, SecretStorageLike } from "../src/secrets/secretStorage";
import { probeSecretStorageRoundTrip } from "../src/secrets/secretStorageProbe";

test("storage probe isolates invocations and leaves the real credential untouched", async () => {
  const values = new Map([[ALYSIS_API_KEY_SECRET, "user-credential-fixture"]]);
  const probeKeys: string[] = [];
  const storage: SecretStorageLike = {
    get: async (key) => values.get(key),
    store: async (key, value) => { probeKeys.push(key); values.set(key, value); },
    delete: async (key) => { values.delete(key); }
  };
  for (let invocation = 0; invocation < 2; invocation += 1) {
    assert.deepEqual(await probeSecretStorageRoundTrip(storage), { stored: true, cleared: true });
  }
  assert.equal(new Set(probeKeys).size, 2);
  assert.equal(probeKeys.includes(ALYSIS_API_KEY_SECRET), false);
  assert.deepEqual([...values], [[ALYSIS_API_KEY_SECRET, "user-credential-fixture"]]);
});

test("storage probe waits for observed writes and deletions", async () => {
  const values = new Map<string, string>();
  const pending: Array<() => void> = [];
  let pauses = 0;
  const storage: SecretStorageLike = {
    get: async (key) => values.get(key),
    store: async (key, value) => { pending.push(() => { values.set(key, value); }); },
    delete: async (key) => { pending.push(() => { values.delete(key); }); }
  };
  const result = await probeSecretStorageRoundTrip(storage, async () => {
    pauses += 1;
    pending.shift()?.();
  });
  assert.deepEqual(result, { stored: true, cleared: true });
  assert.equal(pauses, 2);
  assert.equal(values.size, 0);
});

test("storage probe fails when deletion never becomes visible and keeps values out of errors", async () => {
  let retained: string | undefined;
  let pauses = 0;
  const storage: SecretStorageLike = {
    get: async () => retained,
    store: async (_key, value) => { retained = value; },
    delete: async () => undefined
  };
  await assert.rejects(
    probeSecretStorageRoundTrip(storage, async () => { pauses += 1; }),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /cleared=false/);
      assert.equal(error.message.includes(retained!), false);
      return true;
    }
  );
  assert.ok(pauses > 0 && pauses < 100, "an unavailable storage backend must fail in bounded work");
});

test("storage probe cleans up its key when a backend read rejects", async () => {
  const values = new Map<string, string>();
  const storage: SecretStorageLike = {
    get: async () => { throw new Error("fixture backend unavailable"); },
    store: async (key, value) => { values.set(key, value); },
    delete: async (key) => { values.delete(key); }
  };
  await assert.rejects(probeSecretStorageRoundTrip(storage), /fixture backend unavailable/);
  assert.equal(values.size, 0);
});
