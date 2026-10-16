import assert from "node:assert/strict";
import test from "node:test";

import type { AlysisConfig } from "../src/client/CliDiscovery";
import { ManagedCliError, type InstalledRuntime } from "../src/runtime/ManagedCliRuntime";
import {
  ManagedRuntimeCoordinator,
  type ManagedRuntimeStore
} from "../src/runtime/ManagedRuntimeCoordinator";

const ACTIVE: InstalledRuntime = {
  artifactVersion: "runtime-2",
  cliVersion: "2.0.0",
  protocolVersion: "1",
  target: "linux-x64",
  executablePath: "/global/alysis/managed-cli/versions/runtime-2/linux-x64/alysis",
  sha256: "a".repeat(64),
  size: 123,
  signatureVerified: true
};

const FALLBACK: InstalledRuntime = {
  ...ACTIVE,
  artifactVersion: "runtime-1",
  cliVersion: "1.9.0",
  executablePath: "/global/alysis/managed-cli/versions/runtime-1/linux-x64/alysis",
  sha256: "b".repeat(64)
};

test("uses a validated managed runtime when cliPath is empty", async () => {
  const store = fakeStore();
  const coordinator = new ManagedRuntimeCoordinator(store);
  await coordinator.refresh(baseConfig());

  const config = coordinator.apply(baseConfig());

  assert.equal(config.cliPath, ACTIVE.executablePath);
  assert.equal(config.security?.cliPath.resolvedExecutablePath, ACTIVE.executablePath);
  assert.equal(config.security?.cliPath.executionAllowed, true);
  assert.equal(config.runtimeSelection?.origin, "managed");
  assert.equal(config.runtimeSelection?.production, true);
  assert.deepEqual(coordinator.evidence(), {
    origin: "managed",
    production: true,
    artifactVersion: "runtime-2",
    cliVersion: "2.0.0",
    protocolVersion: "1",
    target: "linux-x64",
    sha256: "a".repeat(64),
    releaseSignatureVerified: true
  });
  assert.equal("executablePath" in coordinator.evidence(), false);
  assert.equal(store.getActiveCalls, 1);
});

test("runtime evidence includes immutable release identity only when installed metadata has it", async () => {
  const releasedRuntime: InstalledRuntime = {
    ...ACTIVE,
    release: {
      tag: "v2.0.0",
      sourceRepository: "https://github.com/AlysisAi/AlysisCode",
      sourceCommit: "c".repeat(40)
    }
  };
  const coordinator = new ManagedRuntimeCoordinator(
    fakeStore({ activeRuntime: releasedRuntime })
  );
  await coordinator.refresh(baseConfig());

  assert.deepEqual(coordinator.evidence(), {
    origin: "managed",
    production: true,
    artifactVersion: "runtime-2",
    cliVersion: "2.0.0",
    protocolVersion: "1",
    target: "linux-x64",
    sha256: "a".repeat(64),
    releaseTag: "v2.0.0",
    sourceRepository: "https://github.com/AlysisAi/AlysisCode",
    sourceCommit: "c".repeat(40),
    releaseSignatureVerified: true
  });
});

test("an explicit cliPath is retained and visibly classified as a non-production override", async () => {
  const store = fakeStore();
  const coordinator = new ManagedRuntimeCoordinator(store);
  const explicit = baseConfig("/work/dev/alysis");
  await coordinator.refresh(explicit);

  const config = coordinator.apply(explicit);

  assert.equal(config.cliPath, "/work/dev/alysis");
  assert.equal(config.runtimeSelection?.origin, "development-override");
  assert.equal(config.runtimeSelection?.production, false);
  assert.match(config.runtimeSelection?.message ?? "", /not managed/i);
  assert.equal(store.getActiveCalls, 0);
});

test("a packaged extension fails closed and actionable when no managed artifact exists", async () => {
  const store = fakeStore({ activeError: new ManagedCliError("NO_ACTIVE_RUNTIME", "missing") });
  const coordinator = new ManagedRuntimeCoordinator(store);
  await coordinator.refresh(baseConfig());

  const config = coordinator.apply(baseConfig());

  assert.equal(config.runtimeSelection?.origin, "unavailable");
  assert.equal(coordinator.evidence().origin, "unavailable");
  assert.equal(coordinator.evidence().production, false);
  assert.equal(config.security?.cliPath.executionAllowed, false);
  assert.equal(config.security?.cliPath.apiKeyForwardingAllowed, false);
  assert.match(config.security?.cliPath.reason ?? "", /No signed managed Alysis Code CLI runtime/i);
});

test("a packaged extension installs its verified platform bundle on first activation", async () => {
  const store = fakeStore({ activeError: new ManagedCliError("NO_ACTIVE_RUNTIME", "missing") });
  let reconciliationCalls = 0;
  const coordinator = new ManagedRuntimeCoordinator(store, {
    reconcileBundled: async () => {
      reconciliationCalls += 1;
      return ACTIVE;
    }
  });

  await coordinator.refresh(baseConfig());

  assert.equal(reconciliationCalls, 1);
  assert.equal(store.getActiveCalls, 0);
  assert.equal(coordinator.apply(baseConfig()).runtimeSelection?.origin, "managed");
  assert.equal(coordinator.apply(baseConfig()).runtimeSelection?.artifactVersion, "runtime-2");
});

test("a failed or tampered platform bundle remains fail-closed", async () => {
  const store = fakeStore({ activeError: new ManagedCliError("NO_ACTIVE_RUNTIME", "missing") });
  const coordinator = new ManagedRuntimeCoordinator(store, {
    reconcileBundled: async () => {
      throw new ManagedCliError("SIGNATURE_INVALID", "tampered bundle");
    }
  });

  await coordinator.refresh(baseConfig());

  const config = coordinator.apply(baseConfig());
  assert.equal(config.runtimeSelection?.origin, "unavailable");
  assert.equal(config.security?.cliPath.executionAllowed, false);
  assert.match(config.security?.cliPath.reason ?? "", /SIGNATURE_INVALID/);
});

test("a newer verified bundle is reconciled before an incompatible installed runtime is read", async () => {
  const store = fakeStore({ activeError: new ManagedCliError("INCOMPATIBLE_PROTOCOL", "old runtime") });
  const coordinator = new ManagedRuntimeCoordinator(store, {
    reconcileBundled: async () => ACTIVE
  });

  await coordinator.refresh(baseConfig());

  assert.equal(store.getActiveCalls, 0);
  assert.equal(coordinator.apply(baseConfig()).runtimeSelection?.artifactVersion, "runtime-2");
});

test("failed bundle reconciliation preserves a currently valid signed runtime", async () => {
  const reports: string[] = [];
  const store = fakeStore();
  const coordinator = new ManagedRuntimeCoordinator(store, {
    reconcileBundled: async () => {
      throw new ManagedCliError("SIGNATURE_INVALID", "tampered upgrade");
    },
    report: (message) => reports.push(message)
  });

  await coordinator.refresh(baseConfig());

  assert.equal(store.getActiveCalls, 1);
  assert.equal(coordinator.apply(baseConfig()).runtimeSelection?.artifactVersion, "runtime-2");
  assert.ok(reports.some((message) => /reconciliation was skipped.*tampered upgrade/i.test(message)));
});

test("an older packaged bundle cannot replace a newer current runtime", async () => {
  const store = fakeStore();
  const coordinator = new ManagedRuntimeCoordinator(store, {
    reconcileBundled: async () => {
      throw new ManagedCliError("DOWNGRADE_BLOCKED", "older bundle");
    }
  });

  await coordinator.refresh(baseConfig());

  assert.equal(store.getActiveCalls, 1);
  assert.equal(coordinator.apply(baseConfig()).runtimeSelection?.artifactVersion, "runtime-2");
});

test("an Extension Development Host can use a labelled PATH fallback", async () => {
  const store = fakeStore({ activeError: new ManagedCliError("NO_ACTIVE_RUNTIME", "missing") });
  const coordinator = new ManagedRuntimeCoordinator(store, { allowDevelopmentPathFallback: true });
  await coordinator.refresh(baseConfig());

  const config = coordinator.apply(baseConfig());

  assert.equal(config.runtimeSelection?.origin, "development-path");
  assert.equal(config.runtimeSelection?.production, false);
  assert.equal(config.security?.cliPath.executionAllowed, true);
});

test("invalid current metadata recovers with last-known-good before exposure", async () => {
  const store = fakeStore({
    activeError: new ManagedCliError("EXECUTABLE_INVALID", "tampered"),
    rollbackRuntime: FALLBACK
  });
  const coordinator = new ManagedRuntimeCoordinator(store);
  await coordinator.refresh(baseConfig());

  assert.equal(coordinator.apply(baseConfig()).runtimeSelection?.artifactVersion, "runtime-1");
  assert.equal(store.rollbackCalls, 1);
});

test("live health rollback is attempted once and never toggles repeatedly", async () => {
  const store = fakeStore({ rollbackRuntime: FALLBACK });
  const coordinator = new ManagedRuntimeCoordinator(store);
  await coordinator.refresh(baseConfig());

  assert.equal(await coordinator.recoverAfterHealthFailure(), true);
  assert.equal(await coordinator.recoverAfterHealthFailure(), false);
  assert.equal(store.rollbackCalls, 1);
  assert.equal(coordinator.apply(baseConfig()).runtimeSelection?.artifactVersion, "runtime-1");
});

test("concurrent refreshes are single-flighted so one process never contends on its own install lock", async () => {
  const store = fakeStore({ getActiveDelayMs: 5 });
  let reconciliationCalls = 0;
  const coordinator = new ManagedRuntimeCoordinator(store, {
    reconcileBundled: async () => {
      reconciliationCalls += 1;
      throw new ManagedCliError("BUNDLED_RUNTIME_MISSING", "no bundle in this build");
    }
  });

  await Promise.all([
    coordinator.refresh(baseConfig()),
    coordinator.refresh(baseConfig()),
    coordinator.refresh(baseConfig())
  ]);

  assert.equal(store.getActiveCalls, 1, "overlapping callers share one validation");
  assert.equal(reconciliationCalls, 1);
  assert.equal(coordinator.apply(baseConfig()).runtimeSelection?.origin, "managed");
});

test("an unchanged pointer short-circuits revalidation, a changed one does not", async () => {
  const store = fakeStore();
  const coordinator = new ManagedRuntimeCoordinator(store);

  await coordinator.refresh(baseConfig());
  await coordinator.refresh(baseConfig());
  assert.equal(store.getActiveCalls, 1, "pointer size + mtime unchanged since the last validation");
  assert.equal(store.pointerFingerprintCalls > 0, true);

  store.fingerprint = "4096:1700000001";
  await coordinator.refresh(baseConfig());
  assert.equal(store.getActiveCalls, 2, "a changed pointer is always revalidated");

  // A developer override invalidates the memo, so returning to the managed runtime revalidates.
  await coordinator.refresh(baseConfig("/work/dev/alysis"));
  await coordinator.refresh(baseConfig());
  assert.equal(store.getActiveCalls, 3);
});

test("a build that ships no bundle reports the actionable install message, not a validation failure", async () => {
  const store = fakeStore({ activeError: new ManagedCliError("NO_ACTIVE_RUNTIME", "missing") });
  const coordinator = new ManagedRuntimeCoordinator(store, {
    reconcileBundled: async () => {
      throw new ManagedCliError("BUNDLED_RUNTIME_MISSING", "This build does not ship a bundled managed Alysis Code CLI.");
    }
  });

  await coordinator.refresh(baseConfig());

  const config = coordinator.apply(baseConfig());
  const reason = config.security?.cliPath.reason ?? "";
  assert.doesNotMatch(reason, /BUNDLED_RUNTIME_INVALID|failed validation/);
  assert.match(reason, /No signed managed Alysis Code CLI runtime/i);
  assert.match(reason, /pipx install alysis-code/);
  assert.match(reason, /alysis\.locateCli/);
  assert.match(reason, /https:\/\/github\.com\/AlysisAi\/AlysisCode#install/);
  assert.equal(coordinator.evidence().code, "BUNDLED_RUNTIME_MISSING");
});

test("a bundle that is present but invalid still outranks the pointer failure", async () => {
  const store = fakeStore({ activeError: new ManagedCliError("NO_ACTIVE_RUNTIME", "missing") });
  const coordinator = new ManagedRuntimeCoordinator(store, {
    reconcileBundled: async () => {
      throw new ManagedCliError("BUNDLED_RUNTIME_INVALID", "bundled manifest is corrupt");
    }
  });

  await coordinator.refresh(baseConfig());

  assert.match(coordinator.apply(baseConfig()).security?.cliPath.reason ?? "", /BUNDLED_RUNTIME_INVALID/);
  assert.equal(coordinator.evidence().code, "BUNDLED_RUNTIME_INVALID");
});

interface FakeStore extends ManagedRuntimeStore {
  getActiveCalls: number;
  rollbackCalls: number;
  pointerFingerprintCalls: number;
  fingerprint: string;
}

function fakeStore(options: {
  activeError?: Error;
  activeRuntime?: InstalledRuntime;
  rollbackRuntime?: InstalledRuntime;
  getActiveDelayMs?: number;
} = {}): FakeStore {
  return {
    getActiveCalls: 0,
    rollbackCalls: 0,
    pointerFingerprintCalls: 0,
    fingerprint: "4096:1700000000",
    async pointerFingerprint() {
      this.pointerFingerprintCalls += 1;
      return this.fingerprint;
    },
    async getActive() {
      this.getActiveCalls += 1;
      if (options.getActiveDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.getActiveDelayMs));
      }
      if (options.activeError) {
        throw options.activeError;
      }
      return options.activeRuntime ?? ACTIVE;
    },
    async rollback() {
      this.rollbackCalls += 1;
      if (!options.rollbackRuntime) {
        throw new ManagedCliError("NO_ROLLBACK_RUNTIME", "missing");
      }
      return options.rollbackRuntime;
    },
    async cleanupOldVersions() {
      return [];
    }
  };
}

function baseConfig(cliPath = ""): AlysisConfig {
  return {
    cliPath,
    defaultMode: "review",
    defaultModel: "",
    baseUrl: "",
    provider: "",
    transport: "stdio",
    sandboxProfile: "default",
    forgeExecuteNoLog: false,
    showStatusBar: true,
    autoStartBridge: false,
    enableForge: true,
    security: {
      isWorkspaceTrusted: true,
      ignoredWorkspaceSettings: [],
      workspaceRoots: [],
      cliPath: {
        value: cliPath || "alysis",
        resolvedExecutablePath: cliPath || "alysis",
        source: cliPath ? "global" : "default",
        trusted: true,
        executionAllowed: true,
        apiKeyForwardingAllowed: true
      }
    }
  };
}
