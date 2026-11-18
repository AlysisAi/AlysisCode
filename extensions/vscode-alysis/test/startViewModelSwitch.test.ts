import assert from "node:assert/strict";
import Module from "node:module";
import path from "node:path";
import test from "node:test";

const disposable = { dispose: () => undefined };
const commands = new Map<string, (...args: any[]) => unknown>();
const warnings: string[] = [];
const errors: string[] = [];
const vscodeStub = {
  Uri: {
    joinPath: (base: { fsPath?: string } | undefined, ...parts: string[]) => ({
      fsPath: path.join(base?.fsPath ?? path.resolve(__dirname, "../.."), ...parts),
      toString: () => "asset"
    })
  },
  workspace: { isTrusted: true },
  commands: {
    registerCommand: (name: string, handler: (...args: any[]) => unknown) => {
      commands.set(name, handler);
      return disposable;
    },
    executeCommand: async (name: string, ...args: unknown[]) => {
      const handler = commands.get(name);
      assert.ok(handler, `Unexpected command: ${name}`);
      return handler(...args);
    }
  },
  window: {
    showErrorMessage: async (message: string) => { errors.push(message); },
    showWarningMessage: async (message: string) => { warnings.push(message); },
    showInformationMessage: async () => undefined
  }
};
const loader = Module as unknown as { _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown };
const originalLoad = loader._load;
loader._load = function (request, parent, isMain) {
  return request === "vscode" ? vscodeStub : originalLoad.call(this, request, parent, isMain);
};
const { StartViewProvider } = require("../src/views/StartViewProvider") as typeof import("../src/views/StartViewProvider");
const { ProviderCatalogController } = require("../src/providers/ProviderCatalogController") as typeof import("../src/providers/ProviderCatalogController");
const { registerPickModelCommand } = require("../src/commands/pickModel") as typeof import("../src/commands/pickModel");
loader._load = originalLoad;

async function scenario(options: { active?: boolean; rejectDefault?: boolean; rejectSession?: boolean } = {}) {
  warnings.length = 0;
  errors.length = 0;
  commands.clear();
  let defaultModel = "model-a";
  const session = { id: "existing-conversation", model: "model-a", messages: ["Keep the repository context"] };
  const sessionChanges: string[] = [];
  const catalog = new ProviderCatalogController({
    ensureStarted: async () => undefined,
    supportsMethod: () => true,
    profilePresets: async () => ({ presets: [] }),
    profileList: async () => ({ active_profile: "test", profiles: [{
      name: "test", default_model: defaultModel, base_url: "http://127.0.0.1:9/v1",
      protocol: "openai_compat", api_key: { present: true, source: "fixture" }
    }] }),
    configSet: async ({ value }: { value: string }) => {
      if (options.rejectDefault) throw new Error("Default model rejected");
      defaultModel = value;
      return { changed: true };
    }
  } as any, { listProfilesWithKeys: async () => [] } as any,
  () => ({ defaultModel: "model-a", provider: "", baseUrl: "" } as any), () => undefined, () => true);
  await catalog.refresh();
  registerPickModelCommand({ subscriptions: [] } as any, {
    catalog,
    hasActiveSession: () => options.active !== false,
    setSessionModel: async model => {
      sessionChanges.push(model);
      if (options.rejectSession) throw new Error("Live switch rejected");
      session.model = model;
      return "";
    }
  });
  let receive!: (message: unknown) => void;
  const view = new StartViewProvider(
    {} as any, () => ({} as any), () => ({} as any), () => ({} as any), () => true,
    () => ({} as any), () => ({ sessions: [], activeSessionId: undefined }),
    async () => false, async () => undefined, async () => undefined, async () => undefined,
    () => catalog.snapshot()
  );
  view.resolveWebviewView({
    visible: true, show: () => undefined,
    webview: { options: {}, html: "", cspSource: "vscode-webview:", asWebviewUri: () => "asset",
      postMessage: async () => true,
      onDidReceiveMessage: (listener: typeof receive) => { receive = listener; return disposable; } },
    onDidChangeVisibility: () => disposable, onDidDispose: () => disposable
  } as any);
  try {
    receive({ type: "model.set", model: "model-b" });
    await new Promise<void>(resolve => setImmediate(resolve));
    return { defaultModel, session, sessionChanges, catalog: catalog.snapshot(), warnings: [...warnings], errors: [...errors] };
  } finally {
    view.dispose();
  }
}

test("composer model selection updates the existing conversation as well as the saved default", async () => {
  const result = await scenario();
  assert.equal(result.defaultModel, "model-b");
  assert.equal(result.catalog.activeModel, "model-b");
  assert.deepEqual(result.session, { id: "existing-conversation", model: "model-b", messages: ["Keep the repository context"] });
  assert.deepEqual(result.sessionChanges, ["model-b"]);
  assert.deepEqual(result.errors, []);
  assert.deepEqual(result.warnings, []);
});

test("composer model selection without a conversation changes only the next-session default", async () => {
  const result = await scenario({ active: false });
  assert.equal(result.defaultModel, "model-b");
  assert.deepEqual(result.sessionChanges, []);
  assert.deepEqual(result.errors, []);
});

test("a rejected default-model change does not change the live conversation", async () => {
  const result = await scenario({ rejectDefault: true });
  assert.equal(result.defaultModel, "model-a");
  assert.equal(result.session.model, "model-a");
  assert.deepEqual(result.sessionChanges, []);
  assert.equal(result.errors.length, 1);
  assert.match(result.errors[0], /Default model rejected/);
});

test("a rejected live switch warns that only the next-session default changed", async () => {
  const result = await scenario({ rejectSession: true });
  assert.equal(result.defaultModel, "model-b");
  assert.equal(result.session.model, "model-a");
  assert.deepEqual(result.sessionChanges, ["model-b"]);
  assert.equal(result.warnings.length, 1);
  assert.match(result.warnings[0], /running session kept its previous model/);
});
