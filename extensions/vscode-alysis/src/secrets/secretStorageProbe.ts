import { randomUUID } from "node:crypto";

import { AlysisSecretStore, SecretStorageLike } from "./secretStorage";

/** Test-host probe: isolate each invocation and require confirmed storage readback. */
export async function probeSecretStorageRoundTrip(
  storage: SecretStorageLike,
  pause: () => Promise<void> = () => new Promise((resolve) => setTimeout(resolve, 50))
): Promise<{ stored: boolean; cleared: boolean }> {
  const secrets = new AlysisSecretStore(storage, `alysis.test.apiKey.${randomUUID()}`);
  const value = randomUUID();
  let lastRead: string | undefined;
  const observe = async (expected: string | undefined): Promise<boolean> => {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      lastRead = await secrets.getApiKey();
      if (lastRead === expected) return true;
      if (attempt < 39) await pause();
    }
    return false;
  };

  try {
    await secrets.setApiKey(value);
    const stored = await observe(value);
    await secrets.deleteApiKey();
    const cleared = await observe(undefined);
    if (!stored || !cleared) {
      const readbackState = lastRead === value ? "retained-probe" : lastRead === "" ? "empty-string" : typeof lastRead;
      throw new Error(
        `SecretStorage probe failed (stored=${stored}, cleared=${cleared}, readback=${readbackState}).`
      );
    }
    return { stored, cleared };
  } finally {
    // A rejected read or write must not strand even this disposable test key.
    await secrets.deleteApiKey();
  }
}
