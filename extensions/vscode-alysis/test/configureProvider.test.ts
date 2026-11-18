import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

const disposable = { dispose: () => undefined };
const registeredCommands = new Map<string, () => Promise<void>>();
const infos: string[] = [];
const warnings: string[] = [];
const errors: string[] = [];
let workspaceTrusted = true;
let quickPickLabel = "";
let quickPickLabels: string[] = [];
let inputValue: string | undefined = undefined;
const settingUpdates: Array<{ key: string; value: unknown; target: unknown }> = [];
const shownQuickPicks: Array<Array<{ label: string; description?: string }>> = [];

const vscodeStub = {
  ConfigurationTarget: { Global: "global" },
  commands: {
    registerCommand: (command: string, callback: () => Promise<void>) => {
      registeredCommands.set(command, callback);
      return disposable;
    },
    executeCommand: async () => undefined
  },
  workspace: {
    get isTrusted() {
      return workspaceTrusted;
    },
    getConfiguration: () => ({
      update: async (key: string, value: unknown, target: unknown) => {
        settingUpdates.push({ key, value, target });
      }
    })
  },
  window: {
    showQuickPick: async (items: Array<{ label: string; description?: string }>) => {
      shownQuickPicks.push(items);
      const label = quickPickLabels.length > 0 ? quickPickLabels.shift() ?? "" : quickPickLabel;
      return items.find((item) => item.label === label);
    },
    showInputBox: async () => inputValue,
    showInformationMessage: async (message: string) => {
      infos.push(message);
      return undefined;
    },
    showWarningMessage: async (message: string) => {
      warnings.push(message);
      return undefined;
    },
    showErrorMessage: async (message: string) => {
      errors.push(message);
      return undefined;
    }
  }
};

const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;
moduleLoader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, parent, isMain);
};

const { registerConfigureProviderCommand } =
  require("../src/commands/configureProvider") as typeof import("../src/commands/configureProvider");
const { AlysisSecretStore } =
  require("../src/secrets/secretStorage") as typeof import("../src/secrets/secretStorage");
moduleLoader._load = originalLoad;

test("Configure Provider offers no vendor-specific API key target", async () => {
  reset();
  quickPickLabels = ["Add or update API key"];
  inputValue = "sk-generic-provider-key";
  const stored: string[] = [];

  registerConfigureProviderCommand(context(), {
    setApiKey: async (value: string) => { stored.push(value); },
    deleteApiKey: async () => undefined
  } as any, bridgeMock() as any, config);
  await registeredCommands.get("alysis.configureProvider")?.();

  // The key flow prompts directly for the current connection: no second picker exists, and no
  // quick pick anywhere in the command names a hardcoded vendor or billing plan.
  assert.equal(shownQuickPicks.length, 1);
  for (const items of shownQuickPicks) {
    for (const item of items) {
      assert.doesNotMatch(`${item.label} ${item.description ?? ""}`, /xiaomi|mimo|pay.as.you.go/i);
    }
  }
  assert.deepEqual(stored, ["sk-generic-provider-key"]);
  assert.deepEqual(settingUpdates, [], "storing a key never rewrites provider, model, or baseUrl settings");
});

test("Configure Provider points at Models and Providers when no provider is configured", async () => {
  reset();
  quickPickLabels = ["Add or update API key"];
  inputValue = "sk-first-ever-key";
  const stored: string[] = [];

  registerConfigureProviderCommand(context(), {
    setApiKey: async (value: string) => { stored.push(value); },
    deleteApiKey: async () => undefined
  } as any, bridgeMock() as any, config);
  await registeredCommands.get("alysis.configureProvider")?.();

  // The key is kept (never dropped), and the user is sent to the generic catalog picker instead
  // of any hardcoded vendor setup.
  assert.deepEqual(stored, ["sk-first-ever-key"]);
  assert.deepEqual(settingUpdates, []);
  assert.match(infos[0] ?? "", /saved securely/);
  assert.match(infos[0] ?? "", /Models and Providers/);
});

test("Configure Provider saves the key for the active profile so the sidebar agrees", async () => {
  reset();
  quickPickLabels = ["Add or update API key"];
  inputValue = "sk-profile-key";
  const globalStored: string[] = [];
  const profileStored: Array<{ profile: string; value: string; envVar: string }> = [];
  const bridge = bridgeMock();
  (bridge as any).profileList = async () => ({
    active_profile: "anthropic",
    profiles: [
      { name: "anthropic", key_env_var: "ANTHROPIC_API_KEY" },
      { name: "openai", key_env_var: "OPENAI_API_KEY" }
    ]
  });

  registerConfigureProviderCommand(context(), {
    setApiKey: async (value: string) => { globalStored.push(value); },
    setProfileApiKey: async (profile: string, value: string, envVar: string) => {
      profileStored.push({ profile, value, envVar });
    },
    deleteApiKey: async () => undefined
  } as any, bridge as any, config);
  await registeredCommands.get("alysis.configureProvider")?.();

  // The CLI ignores the global ALYSIS_API_KEY for non-default profiles, so the key must land in
  // the same profile-scoped store the sidebar reads — not in the legacy global secret.
  assert.deepEqual(globalStored, [], "no global legacy secret is written when a profile is active");
  assert.deepEqual(profileStored, [
    { profile: "anthropic", value: "sk-profile-key", envVar: "ANTHROPIC_API_KEY" }
  ]);
  assert.match(infos[0] ?? "", /for anthropic/);
});

test("Configure Provider cancellation at the key prompt saves nothing", async () => {
  reset();
  quickPickLabels = ["Add or update API key"];
  inputValue = undefined;
  const stored: string[] = [];

  registerConfigureProviderCommand(context(), {
    setApiKey: async (value: string) => { stored.push(value); },
    deleteApiKey: async () => undefined
  } as any, bridgeMock() as any, config);
  await registeredCommands.get("alysis.configureProvider")?.();

  assert.deepEqual(stored, []);
  assert.deepEqual(settingUpdates, []);
});

test("removing a provider key removes it from storage and future bridge credentials", async () => {
  reset();
  quickPickLabel = "Remove saved API key";
  const secrets = memorySecrets();
  await secrets.setProfileApiKey("anthropic", "dummy-key", "ANTHROPIC_API_KEY");
  let refreshed = false;
  registerConfigureProviderCommand(context(), secrets, {
    ...bridgeMock(), isRunning: () => true
  }, config, async () => {
    assert.deepEqual(await secrets.listProviderCredentials(), []);
    refreshed = true;
  });
  await registeredCommands.get("alysis.configureProvider")?.();
  assert.equal(await secrets.getProfileApiKey("anthropic"), undefined);
  assert.deepEqual(await secrets.listProfilesWithKeys(), []);
  assert.equal(refreshed, true);
  assert.deepEqual(infos, ["Saved API key for anthropic removed."]);
});

test("removing one selected provider preserves other provider and legacy keys", async () => {
  reset();
  quickPickLabels = ["Remove saved API key", "anthropic"];
  const secrets = memorySecrets();
  await secrets.setProfileApiKey("anthropic", "dummy-a", "ANTHROPIC_API_KEY");
  await secrets.setProfileApiKey("openai", "dummy-b", "OPENAI_API_KEY");
  await secrets.setApiKey("dummy-legacy");
  registerConfigureProviderCommand(context(), secrets, bridgeMock(), config);
  await registeredCommands.get("alysis.configureProvider")?.();
  assert.equal(await secrets.getProfileApiKey("anthropic"), undefined);
  assert.equal(await secrets.getProfileApiKey("openai"), "dummy-b");
  assert.equal(await secrets.getApiKey(), "dummy-legacy");
  assert.deepEqual((await secrets.listProviderCredentials()).map((key) => key.profile), ["openai"]);
});

test("legacy key removal works offline without starting the bridge", async () => {
  reset();
  quickPickLabel = "Remove saved API key";
  const secrets = memorySecrets();
  await secrets.setApiKey("dummy-legacy");
  const bridge = bridgeMock();
  let refreshed = false;
  registerConfigureProviderCommand(context(), secrets, { ...bridge, isRunning: () => false }, config,
    async () => { refreshed = true; });
  await registeredCommands.get("alysis.configureProvider")?.();
  assert.equal(await secrets.getApiKey(), undefined);
  assert.deepEqual(bridge.ensureStartedOptions, []);
  assert.equal(refreshed, false);
  assert.deepEqual(infos, ["Legacy API key removed."]);
});

test("cancelling the saved key picker keeps all credentials", async () => {
  reset();
  quickPickLabels = ["Remove saved API key", ""];
  const secrets = memorySecrets();
  await secrets.setProfileApiKey("anthropic", "dummy-a", "ANTHROPIC_API_KEY");
  await secrets.setApiKey("dummy-legacy");
  registerConfigureProviderCommand(context(), secrets, bridgeMock(), config);
  await registeredCommands.get("alysis.configureProvider")?.();
  assert.equal(await secrets.getProfileApiKey("anthropic"), "dummy-a");
  assert.equal(await secrets.getApiKey(), "dummy-legacy");
  assert.deepEqual(infos, []);
});

test("key removal reports when an active connection still retains the previous credential", async () => {
  reset();
  quickPickLabel = "Remove saved API key";
  const secrets = memorySecrets();
  await secrets.setProfileApiKey("anthropic", "dummy-key", "ANTHROPIC_API_KEY");
  registerConfigureProviderCommand(context(), secrets, {
    ...bridgeMock(), isRunning: () => true
  }, config, async () => { throw new Error("bridge_profile_restart_blocked"); });
  await registeredCommands.get("alysis.configureProvider")?.();
  assert.equal(await secrets.getProfileApiKey("anthropic"), undefined);
  assert.match(warnings[0] ?? "", /removed.*restart the connection/);
  assert.deepEqual(infos, []);
});

test("key storage failures never report successful removal", async () => {
  reset();
  quickPickLabel = "Remove saved API key";
  const secrets = new AlysisSecretStore({
    get: async (key) => key === "alysis.apiKey" ? "dummy-key" : undefined,
    store: async () => undefined,
    delete: async () => { throw new Error("SecretStorage unavailable"); }
  });
  registerConfigureProviderCommand(context(), secrets, bridgeMock(), config);
  await registeredCommands.get("alysis.configureProvider")?.();
  assert.match(errors[0] ?? "", /Could not remove.*SecretStorage unavailable/);
  assert.deepEqual(infos, []);
});

test("removal with no saved credentials explains that nothing was removed", async () => {
  reset();
  quickPickLabel = "Remove saved API key";
  registerConfigureProviderCommand(context(), memorySecrets(), bridgeMock(), config);
  await registeredCommands.get("alysis.configureProvider")?.();
  assert.deepEqual(infos, ["This extension has no saved API keys."]);
});

function memorySecrets(): InstanceType<typeof AlysisSecretStore> {
  const values = new Map<string, string>();
  return new AlysisSecretStore({
    get: async (key) => values.get(key),
    store: async (key, value) => { values.set(key, value); },
    delete: async (key) => { values.delete(key); }
  });
}

function reset(): void {
  registeredCommands.clear();
  infos.length = 0;
  warnings.length = 0;
  errors.length = 0;
  workspaceTrusted = true;
  quickPickLabel = "";
  quickPickLabels = [];
  inputValue = undefined;
  settingUpdates.length = 0;
  shownQuickPicks.length = 0;
}

function context(): any {
  return { subscriptions: [] };
}

function config(): any {
  return {
    cliPath: "alysis",
    defaultMode: "review",
    defaultModel: "gpt-5",
    baseUrl: "",
    provider: "",
    transport: "stdio",
    sandboxProfile: "default",
    showStatusBar: true,
    autoStartBridge: false,
    security: { isWorkspaceTrusted: workspaceTrusted }
  };
}

function bridgeMock(): any {
  const bridge = {
    ensureStartedOptions: [] as unknown[],
    ensureStarted: async (_config: unknown, options?: unknown) => {
      bridge.ensureStartedOptions.push(options ?? {});
    }
  };
  return bridge;
}
