import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;

const prompts: Array<Record<string, unknown>> = [];
const warnings: string[] = [];
const errors: string[] = [];
const infos: string[] = [];
let inputBoxAnswer: string | undefined = "typed-key";
let modalAnswer: string | undefined = undefined;

const vscodeStub = {
  workspace: { isTrusted: true },
  window: {
    showInputBox: async (options: Record<string, unknown>) => {
      prompts.push(options);
      return inputBoxAnswer;
    },
    showWarningMessage: async (message: string) => {
      warnings.push(message);
      return modalAnswer;
    },
    showErrorMessage: async (message: string) => {
      errors.push(message);
      return undefined;
    },
    showInformationMessage: async (message: string) => {
      infos.push(message);
      return undefined;
    }
  }
};
moduleLoader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, parent, isMain);
};

const { ProviderCatalogController } =
  require("../src/providers/ProviderCatalogController") as typeof import("../src/providers/ProviderCatalogController");
const { AlysisSecretStore } =
  require("../src/secrets/secretStorage") as typeof import("../src/secrets/secretStorage");
moduleLoader._load = originalLoad;

class MemorySecretStorage {
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
}

interface BridgeCall {
  method: string;
  params: unknown;
}

function fakeBridge(overrides: Record<string, unknown> = {}): { bridge: any; calls: BridgeCall[] } {
  const calls: BridgeCall[] = [];
  const bridge = {
    calls,
    supportsMethod: () => true,
    ensureStarted: async () => undefined,
    profilePresets: async () => ({
      presets: [
        {
          key: "anthropic",
          label: "Anthropic Claude",
          protocol: "anthropic_messages",
          protocol_kind: "native",
          base_url: "https://api.anthropic.com",
          base_url_host: "api.anthropic.com",
          key_env_var: "ANTHROPIC_API_KEY",
          suggested_models: ["claude-opus-5", "claude-sonnet-5"],
          notes: "",
          setup_warning: ""
        },
        {
          key: "ollama",
          label: "Ollama",
          protocol: "openai_compat",
          protocol_kind: "compatibility",
          base_url: "http://localhost:11434/v1",
          base_url_host: "localhost:11434",
          key_env_var: null,
          suggested_models: [],
          notes: "",
          setup_warning: ""
        },
        {
          key: "custom",
          label: "Custom endpoint",
          protocol: "openai_compat",
          protocol_kind: "compatibility",
          base_url: "",
          base_url_host: "",
          key_env_var: null,
          suggested_models: [],
          notes: "",
          setup_warning: ""
        }
      ]
    }),
    profileList: async () => ({
      active_profile: "anthropic",
      profiles: [
        {
          name: "anthropic",
          base_url: "https://api.anthropic.com",
          base_url_host: "api.anthropic.com",
          default_model: "claude-sonnet-5",
          protocol: "anthropic_messages",
          key_env_var: "ANTHROPIC_API_KEY",
          api_key: { present: true, source: "stored:profile=anthropic" }
        }
      ]
    }),
    profilePreset: async (params: unknown) => {
      calls.push({ method: "profile.preset", params });
      return { changed: true, profile: {} };
    },
    profileUse: async (params: unknown) => {
      calls.push({ method: "profile.use", params });
      return { active_profile: "x", changed: true };
    },
    configSet: async (params: unknown) => {
      calls.push({ method: "config.set", params });
      return { key: "model", changed: true, config_path: "" };
    },
    ...overrides
  };
  return { bridge, calls };
}

test("configured provider switching uses the live activation path without a separate default mutation", async () => {
  const { bridge, calls } = fakeBridge();
  const activated: string[] = [];
  const catalog = new ProviderCatalogController(bridge, new AlysisSecretStore(new MemorySecretStorage()),
    config, () => undefined, () => true, undefined, async profile => { activated.push(profile); });
  await catalog.refresh();
  await catalog.useConnection("anthropic");
  assert.deepEqual(activated, ["anthropic"]);
  assert.equal(calls.some(call => call.method === "profile.use"), false);
});

test("a rejected live provider activation does not change the saved default and releases the picker", async () => {
  const { bridge, calls } = fakeBridge();
  const catalog = new ProviderCatalogController(bridge, new AlysisSecretStore(new MemorySecretStorage()),
    config, () => undefined, () => true, undefined, async () => { throw new Error("session is busy"); });
  await catalog.refresh();
  await catalog.useConnection("anthropic");
  assert.equal(calls.length, 0);
  assert.equal(catalog.snapshot().activeProfile, "anthropic");
  assert.match(catalog.snapshot().error, /session is busy/);
  assert.equal(catalog.snapshot().busy, "");
});

test("connecting delivers the requested model and stored key to live activation", async () => {
  const { bridge, calls } = fakeBridge();
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  const activated: unknown[] = [];
  inputBoxAnswer = "qa-provider-key";
  modalAnswer = "Reconnect";
  const catalog = new ProviderCatalogController(bridge, secrets, config, () => undefined, () => true,
    undefined, async (profile, model) => {
      assert.ok((await secrets.listProfilesWithKeys()).includes(profile));
      activated.push({ profile, model });
    });
  await catalog.refresh();
  await catalog.connect({ presetKey: "anthropic", model: "claude-opus-5" });
  assert.deepEqual(activated, [{ profile: "anthropic", model: "claude-opus-5" }]);
  assert.deepEqual(calls.map(call => call.method), ["profile.preset"]);
  inputBoxAnswer = "typed-key";
  modalAnswer = undefined;
});

function config(): any {
  return { defaultModel: "claude-sonnet-5", provider: "", baseUrl: "" };
}

test("a local provider connects without inventing an API key", async () => {
  const { bridge, calls } = fakeBridge();
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  const activated: unknown[] = [];
  const promptStart = prompts.length;
  inputBoxAnswer = "";
  try {
    const catalog = new ProviderCatalogController(bridge, secrets, config, () => undefined, () => true,
      undefined, async (profile, model) => { activated.push({ profile, model }); });
    await catalog.refresh();
    await catalog.connect({ presetKey: "ollama", model: "local-model" });
    assert.deepEqual(activated, [{ profile: "ollama", model: "local-model" }]);
    assert.deepEqual(calls.map(call => call.method), ["profile.preset"]);
    assert.deepEqual(await secrets.listProviderCredentials(), []);
    assert.equal((prompts[promptStart].validateInput as (value: string) => unknown)(""), undefined);
  } finally {
    inputBoxAnswer = "typed-key";
  }
});

for (const host of ["localhost:11434", "127.0.0.1:11434", "::1", "[::1]:11434"]) {
  test(`switching to a configured local provider at ${host} does not require a key`, async () => {
    const { bridge } = fakeBridge({ profileList: async () => ({
      active_profile: "anthropic",
      profiles: [{ name: "local", base_url_host: host, protocol: "openai_compat",
        default_model: "local-model", api_key: { present: false } }]
    }) });
    const activated: string[] = [];
    const promptStart = prompts.length;
    const catalog = new ProviderCatalogController(bridge, new AlysisSecretStore(new MemorySecretStorage()),
      config, () => undefined, () => true, undefined, async profile => { activated.push(profile); });
    await catalog.refresh();
    await catalog.useConnection("local");
    assert.deepEqual(activated, ["local"]);
    assert.equal(prompts.length, promptStart);
  });
}

test("cancelling local setup leaves the previous provider untouched", async () => {
  const { bridge, calls } = fakeBridge();
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  inputBoxAnswer = undefined;
  try {
    const catalog = new ProviderCatalogController(bridge, secrets, config, () => undefined, () => true);
    await catalog.refresh();
    await catalog.connect({ presetKey: "ollama", model: "local-model" });
    assert.deepEqual(calls, []);
    assert.equal(catalog.snapshot().activeProfile, "anthropic");
  } finally {
    inputBoxAnswer = "typed-key";
  }
});

test("authenticated custom endpoints retain a forwardable profile key", async () => {
  const { bridge } = fakeBridge();
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  inputBoxAnswer = "custom-endpoint-key";
  const promptStart = prompts.length;
  try {
    const catalog = new ProviderCatalogController(bridge, secrets, config, () => undefined, () => true,
      undefined, async () => {
        assert.deepEqual(await secrets.listProviderCredentials(), [
          { profile: "custom", envVar: "", value: "custom-endpoint-key" }
        ]);
      });
    await catalog.refresh();
    await catalog.connect({ presetKey: "custom", model: "custom-model", baseUrl: "https://models.example.com/v1" });
    assert.equal(catalog.snapshot().error, "");
    assert.equal((prompts[promptStart].validateInput as (value: string) => unknown)(""), "API key is required.");
  } finally {
    inputBoxAnswer = "typed-key";
  }
});

test("forgetting a provider key refreshes live credentials after removing it from storage", async () => {
  const { bridge } = fakeBridge({ isRunning: () => true });
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  await secrets.setProfileApiKey("anthropic", "dummy-key", "ANTHROPIC_API_KEY");
  let refreshed = false;
  const catalog = new ProviderCatalogController(bridge, secrets, config, () => undefined, () => true,
    async () => {
      assert.deepEqual(await secrets.listProviderCredentials(), []);
      refreshed = true;
    });
  modalAnswer = "Remove key";
  try {
    await catalog.forgetKey("anthropic");
    assert.equal(refreshed, true);
    assert.equal(await secrets.getProfileApiKey("anthropic"), undefined);
  } finally { modalAnswer = undefined; }
});

test("forgetting a provider key warns when an active bridge cannot drop the old credential yet", async () => {
  const { bridge } = fakeBridge({ isRunning: () => true });
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  await secrets.setProfileApiKey("anthropic", "dummy-key", "ANTHROPIC_API_KEY");
  const catalog = new ProviderCatalogController(bridge, secrets, config, () => undefined, () => true,
    async () => { throw new Error("active job"); });
  modalAnswer = "Remove key";
  try {
    await catalog.forgetKey("anthropic");
    assert.equal(await secrets.getProfileApiKey("anthropic"), undefined);
    assert.match(warnings.at(-1) ?? "", /removed.*restart the connection/);
    assert.equal(catalog.snapshot().error, "");
  } finally { modalAnswer = undefined; }
});

function controller(bridge: any, secrets: any, trusted = true): any {
  return new ProviderCatalogController(bridge, secrets, config, () => undefined, () => trusted);
}

test("the catalog sorts native providers first and flags local and base-URL-required entries", async () => {
  const { bridge } = fakeBridge();
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));

  await catalog.refresh();
  const state = catalog.snapshot();

  assert.equal(state.supported, true);
  assert.deepEqual(state.providers.map((entry: any) => entry.key), ["anthropic", "ollama", "custom"]);
  assert.equal(state.providers[0].recommended, true);
  assert.equal(state.providers[0].protocolLabel, "Native API");
  assert.equal(state.providers[1].local, true, "a loopback host is detected structurally");
  assert.equal(state.providers[2].needsBaseUrl, true);
  assert.equal(state.providers[0].connected, true);
  assert.equal(state.providers[1].connected, false);
  assert.equal(state.connections[0].profile, "anthropic");
  assert.equal(state.connections[0].active, true);
  assert.equal(state.connections[0].hasKey, true);
});

test("provider mutations are single-flight and a blocked click cannot clear the owner's busy state", async () => {
  infos.length = 0;
  let releaseConfigSet!: () => void;
  const configSetReleased = new Promise<void>((resolve) => {
    releaseConfigSet = resolve;
  });
  let configSetStarted!: () => void;
  const configSetCalled = new Promise<void>((resolve) => {
    configSetStarted = resolve;
  });
  const { bridge, calls } = fakeBridge();
  bridge.configSet = async (params: unknown) => {
    calls.push({ method: "config.set", params });
    configSetStarted();
    await configSetReleased;
    return { key: "model", changed: true, config_path: "" };
  };
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));
  await catalog.refresh();

  const first = catalog.setModel("claude-opus-5");
  await configSetCalled;
  assert.equal(catalog.snapshot().busy, "claude-opus-5");

  await Promise.all([
    catalog.useConnection("anthropic"),
    catalog.setModel("claude-sonnet-5")
  ]);
  assert.deepEqual(calls.map((call) => call.method), ["config.set"]);
  assert.equal(catalog.snapshot().busy, "claude-opus-5", "blocked operations do not clear the active mutation's busy state");
  assert.equal(
    infos.filter((message) => message.includes("already changing the active model")).length,
    2,
    "every blocked click explains itself — a silent ignored click is indistinguishable from a dead button"
  );

  releaseConfigSet();
  await first;
  assert.equal(catalog.snapshot().busy, "");

  await catalog.useConnection("anthropic");
  assert.deepEqual(calls.map((call) => call.method), ["config.set", "profile.use"]);
});

test("a delayed refresh cannot overwrite the model state committed by a newer mutation reload", async () => {
  let backendModel = "claude-sonnet-5";
  let profileListCalls = 0;
  let staleRefreshEntered!: () => void;
  const staleRefreshStarted = new Promise<void>((resolve) => {
    staleRefreshEntered = resolve;
  });
  let releaseStaleRefresh!: () => void;
  const staleRefreshGate = new Promise<void>((resolve) => {
    releaseStaleRefresh = resolve;
  });
  const { bridge, calls } = fakeBridge();
  bridge.profileList = async () => {
    profileListCalls += 1;
    const capturedModel = backendModel;
    if (profileListCalls === 2) {
      staleRefreshEntered();
      await staleRefreshGate;
    }
    return {
      active_profile: "anthropic",
      profiles: [
        {
          name: "anthropic",
          base_url: "https://api.anthropic.com",
          base_url_host: "api.anthropic.com",
          default_model: capturedModel,
          protocol: "anthropic_messages",
          key_env_var: "ANTHROPIC_API_KEY",
          api_key: { present: true, source: "stored:profile=anthropic" }
        }
      ]
    };
  };
  bridge.configSet = async (params: any) => {
    calls.push({ method: "config.set", params });
    backendModel = params.value;
    return { key: "model", changed: true, config_path: "" };
  };
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));
  await catalog.refresh();

  const staleRefresh = catalog.refresh();
  await staleRefreshStarted;
  await catalog.setModel("claude-opus-5");
  assert.equal(catalog.snapshot().activeModel, "claude-opus-5");

  releaseStaleRefresh();
  await staleRefresh;
  assert.equal(backendModel, "claude-opus-5");
  assert.equal(
    catalog.snapshot().activeModel,
    "claude-opus-5",
    "the earlier read is discarded after the mutation's newer load commits"
  );
  assert.equal(profileListCalls, 3);
});

test("connecting a provider asks for the key first, then creates, activates, stores, and sets the model", async () => {
  prompts.length = 0;
  inputBoxAnswer = "sk-provider-key";
  // "anthropic" is already configured in the fixture, so the reconnect confirmation now happens
  // upfront — before the user types anything.
  modalAnswer = "Reconnect";
  const { bridge, calls } = fakeBridge();
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  const catalog = controller(bridge, secrets);
  await catalog.refresh();

  await catalog.connect({ presetKey: "anthropic", model: "claude-opus-5" });

  assert.deepEqual(calls.map((call) => call.method), ["profile.preset", "profile.use", "config.set"]);
  assert.equal((calls[0].params as any).preset_key, "anthropic");
  assert.equal((calls[0].params as any).name, "anthropic");
  assert.equal((calls[0].params as any).yes, true, "the upfront confirmation authorizes the overwrite");
  assert.equal((calls[1].params as any).name, "anthropic");
  // "model" is the settable key that updates the global default and the profile default together.
  assert.equal((calls[2].params as any).key, "model");
  assert.equal((calls[2].params as any).value, "claude-opus-5");

  // The key is stored against the profile with its declared env var, never sent over the protocol.
  assert.equal(await secrets.getProfileApiKey("anthropic"), "sk-provider-key");
  assert.deepEqual(await secrets.listProviderCredentials(), [
    { profile: "anthropic", envVar: "ANTHROPIC_API_KEY", value: "sk-provider-key" }
  ]);
  const sentParams = JSON.stringify(calls.map((call) => call.params));
  assert.equal(sentParams.includes("sk-provider-key"), false, "no bridge call carries the key");
  assert.equal(prompts[0].password, true, "the key prompt masks input");
  modalAnswer = undefined;
});

test("cancelling the key prompt on a fresh connect changes nothing at all", async () => {
  prompts.length = 0;
  infos.length = 0;
  inputBoxAnswer = undefined;
  const { bridge, calls } = fakeBridge();
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  const catalog = controller(bridge, secrets);
  await catalog.refresh();

  await catalog.connect({ presetKey: "ollama", model: "llama4" });

  assert.deepEqual(calls, [], "no profile is created or switched on a cancelled key prompt");
  assert.equal(await secrets.getProfileApiKey("ollama"), undefined);
  assert.match(infos.at(-1) ?? "", /nothing was changed/i);
  assert.equal(catalog.snapshot().busy, "", "the surface is not left busy");
  inputBoxAnswer = "typed-key";
});

test("cancelling the key prompt on a reconnect keeps the existing key", async () => {
  modalAnswer = "Reconnect";
  const { bridge, calls } = fakeBridge();
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  await secrets.setProfileApiKey("anthropic", "existing-key", "ANTHROPIC_API_KEY");
  const catalog = controller(bridge, secrets);
  await catalog.refresh();

  inputBoxAnswer = undefined;
  await catalog.connect({ presetKey: "anthropic", model: "claude-sonnet-5" });

  assert.equal(await secrets.getProfileApiKey("anthropic"), "existing-key");
  assert.equal(
    calls.filter((call) => call.method === "profile.use").length,
    1,
    "keeping the stored key still completes the reconnect"
  );
  inputBoxAnswer = "typed-key";
  modalAnswer = undefined;
});

test("declining the upfront reconnect confirmation costs nothing — not even a key prompt", async () => {
  warnings.length = 0;
  prompts.length = 0;
  modalAnswer = undefined;
  const { bridge, calls } = fakeBridge();
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));
  await catalog.refresh();

  await catalog.connect({ presetKey: "anthropic", model: "claude-sonnet-5" });

  assert.deepEqual(calls, [], "declining stops the connect before any bridge call");
  assert.equal(prompts.length, 0, "the user is never asked to type a key they will not need");
  assert.equal(warnings.length, 1);
});

test("a CLI-side confirmation request is still honored for a profile the catalog did not know", async () => {
  warnings.length = 0;
  modalAnswer = undefined;
  const { bridge, calls } = fakeBridge({
    profilePreset: async (params: any) => {
      calls.push({ method: "profile.preset", params });
      return params.yes
        ? { changed: true, profile: {} }
        : { changed: false, action: { kind: "requires_confirmation", method: "profile.preset", reason: "overwrite" } };
    }
  });
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));
  await catalog.refresh();

  // "ollama" is not listed as connected, so there is no upfront modal; the CLI's
  // requires_confirmation answer drives the fallback confirmation instead.
  await catalog.connect({ presetKey: "ollama", model: "llama4" });
  assert.equal(calls.filter((call) => call.method === "profile.use").length, 0, "declining stops the connect");
  assert.equal(warnings.length, 1);

  modalAnswer = "Reconnect";
  await catalog.connect({ presetKey: "ollama", model: "llama4" });
  assert.equal(calls.some((call) => call.method === "profile.preset" && (call.params as any).yes === true), true);
  assert.equal(calls.some((call) => call.method === "profile.use"), true);
  modalAnswer = undefined;
});

test("existing connections borrow the matching preset's models so switching is a pick, not typing", async () => {
  const { bridge } = fakeBridge({
    profilePresets: async () => ({
      presets: [
        {
          key: "anthropic", label: "Anthropic Claude", protocol: "anthropic_messages", protocol_kind: "native",
          base_url: "https://api.anthropic.com", base_url_host: "api.anthropic.com", key_env_var: "ANTHROPIC_API_KEY",
          suggested_models: ["claude-opus-5", "claude-sonnet-5"],
          suggested_model_descriptions: { "claude-opus-5": "Deepest reasoning", "claude-sonnet-5": "Balanced" },
          notes: "", setup_warning: ""
        },
        {
          key: "openai", label: "OpenAI", protocol: "openai_compat", protocol_kind: "compatibility",
          base_url: "https://api.openai.com/v1", base_url_host: "api.openai.com", key_env_var: "OPENAI_API_KEY",
          suggested_models: ["gpt-6-astra", "gpt-5.6-terra"], notes: "", setup_warning: ""
        },
        {
          key: "openai-responses", label: "OpenAI Responses", protocol: "openai_responses", protocol_kind: "native",
          base_url: "https://api.openai.com/v1", base_url_host: "api.openai.com", key_env_var: "OPENAI_API_KEY",
          suggested_models: ["gpt-6-astra", "gpt-5.6-terra", "gpt-5.6-sol"], notes: "", setup_warning: ""
        }
      ]
    }),
    profileList: async () => ({
      active_profile: "openai-responses",
      profiles: [
        // 1. Created through the catalog: the profile name IS the preset key.
        {
          name: "anthropic", base_url: "https://api.anthropic.com", base_url_host: "api.anthropic.com",
          default_model: "claude-sonnet-5", protocol: "anthropic_messages", api_key: { present: true, source: "stored:profile=anthropic" }
        },
        // 2. The CLI's own `default` profile: no preset is called "default", so match protocol + host.
        {
          name: "default", base_url: "https://api.openai.com/v1", base_url_host: "API.OpenAI.com",
          default_model: "", protocol: "openai_compat", api_key: { present: true, source: "stored:profile=default" }
        },
        // 3. Same host as `openai` but a different wire protocol: the name match must win, and the
        //    protocol must never be cross-matched to the compat preset.
        {
          name: "openai-responses", base_url: "https://api.openai.com/v1", base_url_host: "api.openai.com",
          default_model: "gpt-5.6-terra", protocol: "openai_responses", api_key: { present: true, source: "stored:profile=openai-responses" }
        },
        // 4. A custom gateway: nothing in the catalog knows its models, so offer only what is configured.
        {
          name: "my-gateway", base_url: "https://llm.internal.example/v1", base_url_host: "llm.internal.example",
          default_model: "internal-coder", protocol: "openai_compat", api_key: { present: true, source: "stored:profile=my-gateway" }
        }
      ]
    })
  });
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));
  await catalog.refresh();
  const byName = new Map<string, any>(catalog.snapshot().connections.map((entry: any) => [entry.profile, entry]));

  const anthropic = byName.get("anthropic");
  assert.equal(anthropic.presetKey, "anthropic");
  assert.deepEqual(anthropic.models, ["claude-sonnet-5", "claude-opus-5"], "configured model first, then the preset's suggestions");
  assert.equal(anthropic.modelDescriptions["claude-opus-5"], "Deepest reasoning", "the picker can explain each suggestion");

  const fallback = byName.get("default");
  assert.equal(fallback.presetKey, "openai", "matched by protocol + host, case-insensitively");
  assert.deepEqual(fallback.models, ["gpt-6-astra", "gpt-5.6-terra"], "no configured model, so the suggestions alone");
  assert.equal(fallback.protocolKind, "compatibility");

  const responses = byName.get("openai-responses");
  assert.equal(responses.presetKey, "openai-responses", "name match beats the same-host compat preset");
  assert.deepEqual(responses.models, ["gpt-5.6-terra", "gpt-6-astra", "gpt-5.6-sol"]);
  assert.equal(responses.protocolKind, "native", "protocol kind comes from the preset when profile.list omits it");

  const gateway = byName.get("my-gateway");
  assert.equal(gateway.presetKey, "");
  assert.deepEqual(gateway.models, ["internal-coder"]);
  assert.deepEqual(gateway.modelDescriptions, {});

  // The snapshot is a copy: mutating it must not leak into the controller's state.
  anthropic.models.push("mutated");
  anthropic.modelDescriptions["claude-opus-5"] = "mutated";
  const fresh = catalog.snapshot().connections.find((entry: any) => entry.profile === "anthropic");
  assert.deepEqual(fresh.models, ["claude-sonnet-5", "claude-opus-5"]);
  assert.equal(fresh.modelDescriptions["claude-opus-5"], "Deepest reasoning");
});

test("switching to a keyless provider asks for its key before the switch happens", async () => {
  prompts.length = 0;
  inputBoxAnswer = "switched-in-key";
  const { bridge, calls } = fakeBridge({
    profileList: async () => ({
      active_profile: "anthropic",
      profiles: [
        {
          name: "anthropic", base_url: "https://api.anthropic.com", base_url_host: "api.anthropic.com",
          default_model: "claude-sonnet-5", protocol: "anthropic_messages",
          key_env_var: "ANTHROPIC_API_KEY", api_key: { present: true, source: "stored:profile=anthropic" }
        },
        {
          name: "openai", base_url: "https://api.openai.com/v1", base_url_host: "api.openai.com",
          default_model: "gpt-5", protocol: "openai_compat",
          key_env_var: "OPENAI_API_KEY", api_key: { present: false, source: "missing" }
        }
      ]
    })
  });
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  const catalog = controller(bridge, secrets);
  await catalog.refresh();

  await catalog.useConnection("openai");

  assert.equal(calls.filter((call) => call.method === "profile.use").length, 1);
  assert.equal(prompts.length, 1, "the keyless target prompts once");
  assert.equal(await secrets.getProfileApiKey("openai"), "switched-in-key");

  // Switching back to a provider that already has a key must not re-prompt.
  prompts.length = 0;
  await catalog.useConnection("anthropic");
  assert.equal(prompts.length, 0);
});

test("cancelling the key prompt during a switch keeps the current provider", async () => {
  prompts.length = 0;
  infos.length = 0;
  inputBoxAnswer = undefined;
  const { bridge, calls } = fakeBridge({
    profileList: async () => ({
      active_profile: "anthropic",
      profiles: [
        {
          name: "anthropic", base_url: "https://api.anthropic.com", base_url_host: "api.anthropic.com",
          default_model: "claude-sonnet-5", protocol: "anthropic_messages",
          key_env_var: "ANTHROPIC_API_KEY", api_key: { present: true, source: "stored:profile=anthropic" }
        },
        {
          name: "openai", base_url: "https://api.openai.com/v1", base_url_host: "api.openai.com",
          default_model: "gpt-5", protocol: "openai_compat",
          key_env_var: "OPENAI_API_KEY", api_key: { present: false, source: "missing" }
        }
      ]
    })
  });
  const secrets = new AlysisSecretStore(new MemorySecretStorage());
  const catalog = controller(bridge, secrets);
  await catalog.refresh();

  await catalog.useConnection("openai");

  assert.equal(
    calls.filter((call) => call.method === "profile.use").length,
    0,
    "the switch never happens without the key"
  );
  assert.equal(await secrets.getProfileApiKey("openai"), undefined);
  assert.match(infos.at(-1) ?? "", /still using your current provider/i);
  inputBoxAnswer = "typed-key";
});

test("a connect live-checks the fresh key when the CLI supports doctor.providers.live", async () => {
  warnings.length = 0;
  infos.length = 0;
  inputBoxAnswer = "sk-bad-key";
  const order: string[] = [];
  let liveResult: Record<string, unknown> = {
    ok: false,
    validation: { status: "failed", message: "Provider rejected the API key (HTTP 401)." }
  };
  const { bridge, calls } = fakeBridge({
    doctorProvidersLive: async (params: unknown) => {
      calls.push({ method: "doctor.providers.live", params });
      order.push("live");
      return liveResult;
    }
  });
  const catalog = new ProviderCatalogController(
    bridge,
    new AlysisSecretStore(new MemorySecretStorage()),
    config,
    () => undefined,
    () => true,
    async () => {
      order.push("ensureCredentials");
    }
  );
  await catalog.refresh();

  await catalog.connect({ presetKey: "ollama", model: "llama4" });

  const liveCall = calls.find((call) => call.method === "doctor.providers.live");
  assert.ok(liveCall, "the live check runs after a fresh key is stored");
  assert.equal((liveCall?.params as any).allow_live, true, "the check is explicit-intent only");
  assert.deepEqual(order, ["ensureCredentials", "live"], "credentials reach the bridge before the check");
  assert.match(warnings.at(-1) ?? "", /key check failed/i);
  assert.match(warnings.at(-1) ?? "", /HTTP 401/);

  // A passing check reports an unambiguous success instead of a warning.
  warnings.length = 0;
  liveResult = { ok: true, validation: { status: "passed", message: "Live provider check passed." } };
  inputBoxAnswer = "sk-good-key";
  await catalog.connect({ presetKey: "ollama", model: "llama4" });
  assert.equal(warnings.length, 0);
  assert.match(infos.at(-1) ?? "", /connected and working/i);
  inputBoxAnswer = "typed-key";
});

test("without the credential restart hook the live check is skipped, never faked", async () => {
  infos.length = 0;
  inputBoxAnswer = "sk-some-key";
  const { bridge, calls } = fakeBridge({
    doctorProvidersLive: async (params: unknown) => {
      calls.push({ method: "doctor.providers.live", params });
      return { ok: true, validation: {} };
    }
  });
  // controller() passes no ensureCredentials callback.
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));
  await catalog.refresh();

  await catalog.connect({ presetKey: "ollama", model: "llama4" });

  assert.equal(
    calls.some((call) => call.method === "doctor.providers.live"),
    false,
    "a check that cannot see the new key is not run"
  );
  assert.doesNotMatch(infos.at(-1) ?? "", /working/i, "unverified connects do not claim verification");
  inputBoxAnswer = "typed-key";
});

test("an untrusted workspace cannot connect or switch providers", async () => {
  warnings.length = 0;
  const { bridge, calls } = fakeBridge();
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()), false);
  await catalog.refresh();

  await catalog.connect({ presetKey: "anthropic" });
  await catalog.useConnection("anthropic");
  await catalog.setModel("claude-opus-5");

  assert.deepEqual(calls, []);
  assert.equal(warnings.length, 3);
});

test("a provider needing a base URL is not connected without one", async () => {
  errors.length = 0;
  const { bridge, calls } = fakeBridge();
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));
  await catalog.refresh();

  await catalog.connect({ presetKey: "custom", model: "some-model" });
  assert.equal(calls.length, 0);
  assert.match(errors.at(-1) ?? "", /needs a base URL/);

  await catalog.connect({ presetKey: "custom", model: "some-model", baseUrl: "https://api.example/v1" });
  assert.equal((calls[0]?.params as any).base_url, "https://api.example/v1");
});

test("a bridge without the profile methods reports a reason instead of faking controls", async () => {
  const { bridge } = fakeBridge({
    supportsMethod: () => false,
    currentHealth: () => ({ capabilities: { methods: [] } })
  });
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));

  await catalog.refresh();
  const state = catalog.snapshot();

  assert.equal(state.supported, false);
  assert.deepEqual(state.providers, []);
  assert.match(state.reason, /cannot list providers/);
});

test("capabilities are read after the bridge starts, not before it has any", async () => {
  // Regression: capabilities come from the initialize handshake, so supportsMethod() is false for
  // everything until a bridge is up. Checking before ensureStarted made every cold open claim the
  // CLI could not list providers, with the real catalog one call away.
  const order: string[] = [];
  let started = false;
  const { bridge } = fakeBridge({
    ensureStarted: async () => {
      order.push("ensureStarted");
      started = true;
    },
    supportsMethod: () => {
      order.push("supportsMethod");
      return started;
    },
    currentHealth: () => (started ? { capabilities: { methods: [] } } : undefined)
  });
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));

  await catalog.refresh();
  const state = catalog.snapshot();

  assert.equal(order[0], "ensureStarted", "the bridge must be started before capabilities are read");
  assert.equal(state.supported, true);
  assert.equal(state.providers.length, 3);
  assert.equal(state.reason, "");
});

test("an unknown-capability bridge attempts the calls instead of guessing the CLI is too old", async () => {
  // No health at all (handshake produced nothing): a false negative here would hide a working
  // catalog, so the surface tries the call and lets a real failure speak for itself.
  const { bridge } = fakeBridge({
    supportsMethod: () => false,
    currentHealth: () => undefined
  });
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));

  await catalog.refresh();
  const state = catalog.snapshot();

  assert.equal(state.supported, true);
  assert.equal(state.providers.length, 3);
});

test("a value that looks like a credential is refused as a model name", async () => {
  errors.length = 0;
  const { bridge, calls } = fakeBridge();
  const catalog = controller(bridge, new AlysisSecretStore(new MemorySecretStorage()));
  await catalog.refresh();

  await catalog.setModel("sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789");

  assert.deepEqual(calls, []);
  assert.match(errors.at(-1) ?? "", /looks like a credential/);
});
