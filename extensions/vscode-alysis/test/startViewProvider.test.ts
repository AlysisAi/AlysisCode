import assert from "node:assert/strict";
import Module from "node:module";
import path from "node:path";
import test from "node:test";

const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;
const errors: string[] = [];
const reviewCommands = new Map<string, () => Promise<void>>();
const vscodeStub = {
  Uri: {
    joinPath: (base: { fsPath?: string } | undefined, ...parts: string[]) => ({
      fsPath: path.join(base?.fsPath ?? path.resolve(__dirname, "../.."), ...parts),
      toString: () => "asset"
    })
  },
  commands: {
    executeCommand: async () => undefined,
    registerCommand: (id: string, callback: () => Promise<void>) => {
      reviewCommands.set(id, callback);
      return { dispose: () => undefined };
    }
  },
  window: {
    showErrorMessage: async (message: string) => {
      errors.push(message);
      return undefined;
    },
    showOpenDialog: async () => undefined,
    showWarningMessage: async () => undefined
  },
  workspace: { asRelativePath: () => "file.ts" }
};
moduleLoader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, parent, isMain);
};

const { buildCommandCatalog, isStartViewMessage, isReadyToStart, buildStartViewState, buildSlashCommandCatalog, relativeTime } =
  require("../src/views/StartViewProvider") as typeof import("../src/views/StartViewProvider");
const { emptyModelsSurfaceState } =
  require("../src/providers/ProviderCatalogController") as typeof import("../src/providers/ProviderCatalogController");
const { registerReviewCommands } =
  require("../src/commands/reviewCommands") as typeof import("../src/commands/reviewCommands");
moduleLoader._load = originalLoad;

function readyModels() {
  return {
    ...emptyModelsSurfaceState(),
    supported: true,
    loaded: true,
    activeProfile: "anthropic",
    activeModel: "claude-sonnet-5",
    selectionStatus: "ready" as const,
    selectionReason: "Ready",
    connections: [{
      profile: "anthropic",
      host: "api.anthropic.com",
      baseUrl: "https://api.anthropic.com",
      protocol: "anthropic_messages",
      model: "claude-sonnet-5",
      models: ["claude-sonnet-5"],
      modelDescriptions: {},
      presetKey: "anthropic",
      protocolKind: "native",
      active: true,
      hasKey: true,
      keySource: "secret_storage",
      keyEnvVar: "ANTHROPIC_API_KEY",
      storedInVsCode: true,
      authProvider: "",
      reasoningEffort: "",
      selectionReady: false
    }]
  };
}

test("Start Here accepts only bounded tasks, safe modes, and known actions", () => {
  assert.equal(isStartViewMessage({ type: "ready" }), true);
  assert.equal(isStartViewMessage({ type: "task.submit", instruction: "Explain this project", mode: "readonly", requestId: "r-1" }), true);
  assert.equal(isStartViewMessage({ type: "task.submit", instruction: "Update the UI", mode: "review", requestId: "r-2" }), true);
  assert.equal(isStartViewMessage({ type: "task.submit", instruction: "Run the task", mode: "auto", requestId: "r-3" }), true);
  assert.equal(isStartViewMessage({ type: "task.submit", instruction: "Missing correlation", mode: "review" }), false);
  assert.equal(isStartViewMessage({ type: "task.submit", instruction: "   ", mode: "review" }), false);
  assert.equal(isStartViewMessage({ type: "task.submit", instruction: "x", mode: "fullaccess" }), false);
  assert.equal(isStartViewMessage({ type: "task.submit", instruction: "x".repeat(20001), mode: "review" }), false);
  assert.equal(isStartViewMessage({ type: "task.cancel" }), true);
  assert.equal(isStartViewMessage({ type: "approval", approvalId: "approval-1", decision: "allow_once" }), true);
  assert.equal(isStartViewMessage({ type: "approval", approvalId: "approval-1", decision: "always" }), false);
  assert.equal(isStartViewMessage({ type: "action", action: "new" }), true);
  assert.equal(isStartViewMessage({ type: "action", action: "recent" }), true);
  assert.equal(isStartViewMessage({ type: "action", action: "mcp" }), true);
  assert.equal(isStartViewMessage({ type: "action", action: "addFiles" }), true);
  assert.equal(isStartViewMessage({ type: "session", action: "resume", sessionId: "session-1" }), true);
  assert.equal(isStartViewMessage({ type: "session", action: "delete", sessionId: "session-1" }), false);
  assert.equal(isStartViewMessage({ type: "session", action: "resume", sessionId: "" }), false);
  assert.equal(isStartViewMessage({ type: "session", action: "resume", sessionId: "x".repeat(1025) }), false);
  assert.equal(isStartViewMessage({ type: "command", command: "alysis.showHistory" }), true);
  assert.equal(isStartViewMessage({ type: "command", command: "alysis.showForge" }), false);
  assert.equal(isStartViewMessage({ type: "command", command: "alysis.manage.remove" }), false);
  assert.equal(isStartViewMessage({ type: "command", command: "workbench.action.closeWindow" }), false);
  assert.equal(isStartViewMessage({ type: "cockpit", message: { type: "mode.set", mode: "review" } }), true);
  assert.equal(isStartViewMessage({ type: "cockpit", message: { type: "forge.executePreview", auto: true } }), false);
  assert.equal(isStartViewMessage({ type: "action", action: "setup" }), false);
  assert.equal(isStartViewMessage({ type: "action", action: "unknown" }), false);
});

test("Cockpit Git changes dispatches to the same structured-review handler as the registered command", async () => {
  errors.length = 0;
  let receive!: (message: unknown) => void;
  const disposable = { dispose: () => undefined };
  const actions: string[] = [];
  let finish!: () => void;
  let started!: () => void;
  const running = new Promise<void>((resolve) => { started = resolve; });
  const reviewGitChanges = registerReviewCommands({ subscriptions: [] } as any, {
    executeAction: async (id) => {
      actions.push(id);
      started();
      await new Promise<void>((resolve) => { finish = resolve; });
      return {} as any;
    }
  });
  const { StartViewProvider } = require("../src/views/StartViewProvider") as typeof import("../src/views/StartViewProvider");
  const provider = new StartViewProvider(
    {} as any, () => ({} as any), () => ({} as any), () => ({} as any), () => true,
    () => ({} as any), () => ({ sessions: [], activeSessionId: undefined }),
    async () => false, async () => undefined, async () => undefined, async () => undefined,
    () => readyModels(), { worktreeAction: async (action: "new" | "move" | "review") => { if (action === "review") await reviewGitChanges(); } } as any
  );
  provider.resolveWebviewView({
    visible: true, show: () => undefined,
    webview: { options: {}, html: "", cspSource: "vscode-webview:", asWebviewUri: () => "asset",
      postMessage: async () => true, onDidReceiveMessage: (listener: typeof receive) => { receive = listener; return disposable; } },
    onDidChangeVisibility: () => disposable, onDidDispose: () => disposable
  } as any);
  try {
    receive({ type: "worktree", action: "review" });
    await running;
    await reviewCommands.get("alysis.reviewGitChanges")!();
    receive({ type: "worktree", action: "review" });
    assert.deepEqual(actions, ["code.review.start"]);
    assert.deepEqual(errors, []);
    finish();
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(isStartViewMessage({ type: "worktree", action: "review" }), true);
    assert.equal(isStartViewMessage({ type: "worktree", action: "code.review.start" }), false);
  } finally { provider.dispose(); }
});

test("Start Here gates session tools on an active chat session and omits unsupported actions", () => {
  const noSession = {
    sessionId: null,
    jobStatus: "idle",
    cockpit: { actionResults: [] }
  } as any;
  const commands = buildCommandCatalog(noSession, true, (actionId) => actionId !== "session.status");

  const sessionTools = commands.find((item) => item.command === "alysis.sessionActions");
  assert.equal(sessionTools?.available, false);
  assert.match(sessionTools?.unavailableReason ?? "", /Start or resume a task/);
  assert.equal(commands.some((item) => item.id === "session.status"), false);
  const sessionAction = commands.find((item) => item.id === "session.modelInfo");
  assert.equal(sessionAction?.available, false);
});

test("Start Here catches rejected async actions and releases the composer", async () => {
  errors.length = 0;
  let receive: ((message: unknown) => void) | undefined;
  const posted: unknown[] = [];
  const disposable = { dispose: () => undefined };
  const view = {
    visible: true,
    show: () => undefined,
    webview: {
      options: {},
      html: "",
      cspSource: "vscode-webview:",
      asWebviewUri: () => "vscode-webview://asset",
      postMessage: async (message: unknown) => {
        posted.push(message);
        return true;
      },
      onDidReceiveMessage: (listener: (message: unknown) => void) => {
        receive = listener;
        return disposable;
      }
    },
    onDidChangeVisibility: () => disposable,
    onDidDispose: () => disposable
  };
  const { StartViewProvider } = require("../src/views/StartViewProvider") as typeof import("../src/views/StartViewProvider");
  const provider = new StartViewProvider(
    {} as any,
    () => ({} as any),
    () => ({} as any),
    () => ({} as any),
    () => true,
    () => ({} as any),
    () => ({ sessions: [], activeSessionId: undefined }),
    async () => {
      throw new Error("backend failed");
    },
    async () => undefined,
    async () => undefined,
    async () => undefined,
    () => readyModels()
  );
  provider.resolveWebviewView(view as any);

  receive?.({ type: "task.submit", instruction: "inspect", mode: "readonly", requestId: "failed-request" });
  await new Promise((resolve) => setTimeout(resolve, 10));

  assert.deepEqual(posted, [{ type: "task.result", started: false, requestId: "failed-request" }]);
  assert.deepEqual(errors, ["Alysis Code could not complete that action: backend failed"]);
  provider.dispose();
});

test("pending images are read on restore, image actions, and turn changes; stale reads cannot overwrite", async () => {
  const { StartViewProvider } = require("../src/views/StartViewProvider") as typeof import("../src/views/StartViewProvider");
  const runtime: any = {
    cliHealth: { status: "ok" }, cliOrigin: { status: "verified" }, bridgeProcess: { status: "ready" },
    bridgeProtocol: { status: "ok" }, sandboxDoctor: { status: "ok" }, runtimeSelection: { origin: "managed" },
    compatibility: { protocol: { current: "1", compatible: true, direction: "aligned" }, features: {} }, events: []
  };
  const cockpit: any = { sessionId: "images-1", mode: "review", jobStatus: "idle", items: [],
    cockpit: { actionResults: [] } };
  const reads: Array<{ sessionId: string; resolve(value: unknown): void }> = [];
  const posted: any[] = [];
  let receive: ((message: unknown) => void) | undefined;
  const disposable = { dispose: () => undefined };
  const view: any = { visible: true, show: () => undefined,
    webview: { cspSource: "test:", asWebviewUri: () => "test:asset", postMessage: async (message: unknown) => { posted.push(message); return true; },
      onDidReceiveMessage: (listener: (message: unknown) => void) => { receive = listener; return disposable; } },
    onDidChangeVisibility: () => disposable, onDidDispose: () => disposable };
  const provider = new StartViewProvider({} as any, () => runtime,
    () => ({ defaultMode: "review", provider: "", baseUrl: "", defaultModel: "" } as any),
    () => ({ supported: true, activeProfile: "", profiles: [] } as any), () => true, () => cockpit,
    () => ({ sessions: [], activeSessionId: undefined }), async () => true,
    async () => undefined, async () => undefined, async () => undefined, readyModels,
    { getImages: (sessionId: string) => new Promise((resolve) => reads.push({ sessionId, resolve })) } as any);
  provider.resolveWebviewView(view);
  receive?.({ type: "ready" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(reads.length, 1, "opening an existing task restores its real pending basket");
  reads[0].resolve({ session_id: "images-1", images: [{ path: "/repo/screenshots/kept.png" }] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(posted.filter((item) => item.type === "image.basket").at(-1)?.names, ["kept.png"]);
  provider.update();
  assert.equal(reads.length, 1, "ordinary transcript updates do not poll the basket");
  cockpit.cockpit.actionResults.push({ id: "image-add", actionId: "session.images.add", status: "ok" });
  provider.update();
  assert.equal(reads.length, 2, "native and slash image actions invalidate the basket");
  cockpit.jobStatus = "running";
  provider.update();
  reads[2].resolve({ session_id: "images-1", images: [] });
  await new Promise((resolve) => setImmediate(resolve));
  reads[1].resolve({ session_id: "images-1", images: [{ path: "/repo/stale.png" }] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(posted.filter((item) => item.type === "image.basket").at(-1)?.names, [], "late reads cannot restore consumed images");
  cockpit.sessionId = "images-2";
  provider.update();
  reads[3].resolve({ session_id: "images-1", images: [{ path: "/repo/wrong-task.png" }] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(posted.filter((item) => item.type === "image.basket").length, 2);
  provider.dispose();
});

test("Models surface messages are bounded and reject unknown shapes", () => {
  assert.equal(isStartViewMessage({ type: "models.refresh" }), true);
  assert.equal(isStartViewMessage({ type: "provider.connect", presetKey: "anthropic" }), false);
  assert.equal(
    isStartViewMessage({ type: "provider.connect", presetKey: "anthropic", model: "claude-opus-5", baseUrl: "" }),
    true
  );
  assert.equal(isStartViewMessage({ type: "provider.connect", presetKey: "" }), false);
  assert.equal(isStartViewMessage({ type: "provider.connect", presetKey: "x".repeat(97) }), false);
  assert.equal(isStartViewMessage({ type: "provider.connect", presetKey: "anthropic", model: 42 }), false);
  assert.equal(
    isStartViewMessage({ type: "provider.connect", presetKey: "anthropic", baseUrl: "x".repeat(2049) }),
    false
  );
  assert.equal(isStartViewMessage({ type: "provider.use", profile: "anthropic" }), true);
  assert.equal(isStartViewMessage({ type: "provider.use", profile: "  " }), false);
  assert.equal(isStartViewMessage({ type: "provider.key", profile: "anthropic", keyAction: "update" }), true);
  assert.equal(isStartViewMessage({ type: "provider.key", profile: "anthropic", keyAction: "forget" }), true);
  assert.equal(isStartViewMessage({ type: "provider.key", profile: "anthropic", keyAction: "read" }), false);
  assert.equal(isStartViewMessage({ type: "model.set", model: "claude-opus-5" }), true);
  assert.equal(isStartViewMessage({ type: "model.set", model: "x".repeat(201) }), false);
  assert.equal(isStartViewMessage({ type: "mention.search", query: "app", token: 3 }), true);
  assert.equal(isStartViewMessage({ type: "mention.search", query: "", token: 1 }), true);
  assert.equal(isStartViewMessage({ type: "mention.search", query: "x".repeat(201), token: 1 }), false);
  assert.equal(isStartViewMessage({ type: "mention.search", query: "app", token: "1" }), false);
  assert.equal(isStartViewMessage({ type: "mention.search", query: "app" }), false);
  assert.equal(isStartViewMessage({ type: "provider.forget", profile: "anthropic" }), false);
});

test("cockpit editor, clipboard, link, and image messages stay bounded and allowlisted", () => {
  assert.equal(isStartViewMessage({ type: "command", command: "alysis.locateCli" }), true);
  assert.equal(isStartViewMessage({ type: "command", command: "workbench.trust.manage" }), true);
  assert.equal(isStartViewMessage({ type: "command", command: "workbench.action.terminal.new" }), false);
  assert.equal(
    isStartViewMessage({ type: "command", command: "alysis.backend.session.images.add", args: ["/repo/a.png"] }),
    true
  );
  assert.equal(isStartViewMessage({ type: "command", command: "alysis.runDoctor", args: [{ evil: true }] }), false);
  assert.equal(isStartViewMessage({ type: "command", command: "alysis.runDoctor", args: ["a", "b", "c", "d", "e"] }), false);

  assert.equal(isStartViewMessage({ type: "clipboard.copy", text: "const a = 1;" }), true);
  assert.equal(isStartViewMessage({ type: "clipboard.copy", text: "" }), false);
  assert.equal(isStartViewMessage({ type: "editor.insert", text: "x".repeat(100_001) }), false);
  assert.equal(isStartViewMessage({ type: "editor.applyCode", text: "code", language: "ts" }), true);

  assert.equal(isStartViewMessage({ type: "open.external", url: "https://example.com/docs" }), true);
  assert.equal(isStartViewMessage({ type: "open.external", url: "http://example.com" }), false);
  assert.equal(isStartViewMessage({ type: "open.external", url: "command:workbench.action.quit" }), false);
  assert.equal(isStartViewMessage({ type: "open.file", path: "src/app.ts:42" }), true);
  assert.equal(isStartViewMessage({ type: "open.file", path: "src/app\0.ts" }), false);

  assert.equal(isStartViewMessage({ type: "image.attach", uris: ["file:///repo/shot.png"] }), true);
  assert.equal(isStartViewMessage({ type: "image.attach", uris: [] }), false);
  assert.equal(isStartViewMessage({ type: "image.paste", name: "s.png", mime: "image/png", data: "iVBORw0KGgo=" }), true);
  assert.equal(isStartViewMessage({ type: "image.paste", name: "s.svg", mime: "image/svg+xml", data: "PHN2Zz4=" }), false);
  assert.equal(isStartViewMessage({ type: "image.paste", name: "s.png", mime: "image/png", data: "not base64!" }), false);
});

test("the posted state carries the host readiness model and gates the composer on it", () => {
  const runtime = (overrides: Record<string, unknown> = {}): any => ({
    cliHealth: { status: "ok", message: null },
    cliOrigin: { status: "verified", message: null },
    bridgeProcess: { status: "ready", message: null },
    bridgeProtocol: { status: "ok", message: null },
    sandboxDoctor: { status: "ok", message: null },
    runtimeSelection: { origin: "managed", message: null },
    compatibility: { protocol: { current: "1", compatible: true, direction: "aligned" }, features: {} },
    events: [],
    ...overrides
  });
  const cockpit: any = {
    sessionId: null,
    jobStatus: "idle",
    mode: null,
    items: [],
    lastAcceptedTaskRequestId: null,
    cockpit: { actionResults: [] }
  };
  const config: any = { defaultMode: "review", provider: "", baseUrl: "", defaultModel: "" };
  const profiles: any = { supported: true, activeProfile: "", profiles: [] };

  const blocked = buildStartViewState(
    runtime({ cliHealth: { status: "unreachable", message: null } }),
    config,
    profiles,
    true,
    cockpit,
    { sessions: [], activeSessionId: undefined },
    { ...emptyModelsSurfaceState(), loaded: true, supported: true }
  );
  assert.equal(blocked.ready, false, "a missing engine locks the composer even with a provider selected");
  assert.equal(blocked.readiness.ok, false);
  assert.deepEqual(blocked.readiness.blockers.map((blocker) => blocker.id), ["runtime_missing", "provider_incomplete"]);
  assert.match(blocked.readyReason, /Locate the CLI/);
  for (const blocker of blocked.readiness.blockers) {
    assert.ok(blocker.title.length <= 60, `${blocker.id} title stays short`);
    assert.ok(blocker.detail.length > 0, `${blocker.id} explains itself`);
    assert.ok(blocker.actions.length > 0, `${blocker.id} offers a way out`);
  }

  const healthy = buildStartViewState(
    runtime(),
    { ...config, defaultModel: "claude-opus-5" },
    profiles,
    true,
    cockpit,
    { sessions: [], activeSessionId: undefined },
    readyModels()
  );
  assert.equal(healthy.ready, true);
  assert.equal(healthy.readiness.ok, true);
  assert.deepEqual(healthy.readiness.blockers, [], "a healthy setup shows no strip");

  // Before the first session the profile snapshot store is empty; the header must still name the
  // active connection from the models surface rather than the generic "Provider" placeholder.
  assert.equal(healthy.providerName, "anthropic");
  const unnamed = buildStartViewState(
    runtime(),
    { ...config, defaultModel: "claude-opus-5" },
    profiles,
    true,
    cockpit,
    { sessions: [], activeSessionId: undefined },
    { ...readyModels(), activeProfile: "" }
  );
  assert.equal(unnamed.providerName, "Provider");
});

test("a classified transcript failure reaches the webview as a typed, actionable card", () => {
  const { ChatTranscript } = require("../src/chat/ChatTranscript") as typeof import("../src/chat/ChatTranscript");
  const transcript = new ChatTranscript();
  transcript.addError("Request failed with status 401: invalid api key");
  const chatState = transcript.state();

  const state = buildStartViewState(
    {
      cliHealth: { status: "ok", message: null },
      cliOrigin: { status: "verified", message: null },
      bridgeProcess: { status: "ready", message: null },
      bridgeProtocol: { status: "ok", message: null },
      sandboxDoctor: { status: "ok", message: null },
      runtimeSelection: { origin: "managed", message: null },
      compatibility: { protocol: { current: "1", compatible: true, direction: "aligned" }, features: {} },
      events: []
    } as any,
    { defaultMode: "review", provider: "", baseUrl: "", defaultModel: "claude-opus-5" } as any,
    { supported: true, activeProfile: "", profiles: [] } as any,
    true,
    { ...chatState, cockpit: { actionResults: [] }, lastAcceptedTaskRequestId: null } as any,
    { sessions: [], activeSessionId: undefined },
    readyModels()
  );

  const item = state.conversation.items.at(-1);
  assert.equal(item?.kind, "error");
  assert.equal(item?.errorKind, "auth_invalid");
  assert.ok(item?.errorTitle && item.errorTitle.length > 0, "the card heading is the plain-language title");
  assert.doesNotMatch(item?.errorTitle ?? "", /401|status/i, "no protocol codes reach the heading");
  assert.ok((item?.errorDetail ?? "").length > 0);
  assert.equal(item?.errorActions[0]?.command, "alysis.configureProvider");
  assert.equal(item?.errorActions[0]?.primary, true);
});

test("the start surface fails closed until provider selection readiness is complete", () => {
  const blank = {
    defaultModel: "",
    provider: "",
    baseUrl: ""
  } as unknown as Parameters<typeof isReadyToStart>[0];
  const noProfiles = { supported: true, activeProfile: "", profiles: [] };

  assert.equal(
    isReadyToStart(blank, noProfiles, emptyModelsSurfaceState()),
    false,
    "an unchecked CLI cannot safely accept a send"
  );
  assert.equal(
    isReadyToStart(blank, noProfiles, { ...emptyModelsSurfaceState(), loaded: true }),
    false,
    "the setup gate appears only after a completed refresh confirms there is no connection"
  );

  // Any single piece of evidence is enough — the gate must never hide the composer from someone
  // whose CLI is already configured outside the extension.
  assert.equal(
    isReadyToStart({ ...blank, defaultModel: "claude-opus-5" } as typeof blank, noProfiles, emptyModelsSurfaceState()),
    false
  );
  assert.equal(
    isReadyToStart({ ...blank, baseUrl: "https://api.example/v1" } as typeof blank, noProfiles, emptyModelsSurfaceState()),
    false
  );
  assert.equal(
    isReadyToStart(blank, { supported: true, activeProfile: "anthropic", profiles: [] }, emptyModelsSurfaceState()),
    false
  );
  assert.equal(
    isReadyToStart(blank, noProfiles, {
      ...emptyModelsSurfaceState(),
      loaded: true,
      supported: true,
      activeProfile: "chatgpt-codex",
      connections: [{
        profile: "chatgpt-codex",
        active: true,
        model: "gpt-5.4",
        authProvider: "chatgpt-codex",
        reasoningEffort: "",
        selectionReady: false
      }] as never
    }),
    false,
    "a subscription model without its required effort is incomplete"
  );
  assert.equal(isReadyToStart(blank, noProfiles, {
    ...emptyModelsSurfaceState(),
    loaded: true,
    supported: true,
    activeProfile: "chatgpt-codex",
    connections: [{
      profile: "chatgpt-codex",
      active: true,
      model: "gpt-5.4",
      authProvider: "chatgpt-codex",
      reasoningEffort: "high",
      selectionReady: true
    }] as never
  }), true);
  // An inactive connection is not evidence on its own after provider discovery completes.
  assert.equal(
    isReadyToStart(blank, noProfiles, {
      ...emptyModelsSurfaceState(),
      loaded: true,
      connections: [{ profile: "anthropic", active: false, hasKey: true }] as never
    }),
    false
  );
});

test("the persona surface reports active permissions and describes inactive persona constraints", () => {
  const runtime: any = {
    cliHealth: { status: "ok", message: null },
    cliOrigin: { status: "verified", message: null },
    bridgeProcess: { status: "ready", message: null },
    bridgeProtocol: { status: "ok", message: null },
    sandboxDoctor: { status: "ok", message: null },
    runtimeSelection: { origin: "managed", message: null },
    compatibility: { protocol: { current: "1", compatible: true, direction: "aligned" }, features: {} },
    events: []
  };
  const config: any = { defaultMode: "review", provider: "", baseUrl: "", defaultModel: "claude-opus-5" };
  const profiles: any = { supported: true, activeProfile: "", profiles: [] };
  const personas = {
    supported: true,
    reason: null,
    enabled: true,
    active: "architect",
    activeSource: "user",
    available: [
      { name: "code", description: "Implements changes.", defaultExecMode: "", modelRole: "coding", sourceScope: "builtin", writeScoped: false },
      { name: "architect", description: "Writes markdown only.", defaultExecMode: "review", modelRole: "planner", sourceScope: "builtin", writeScoped: true },
      { name: "ask", description: "Read-only Q&A.", defaultExecMode: "readonly", modelRole: "", sourceScope: "builtin", writeScoped: false }
    ]
  };
  const cockpitFor = (mode: string | null): any => ({
    sessionId: mode ? "session-1" : null,
    jobStatus: "idle",
    mode,
    items: [],
    lastAcceptedTaskRequestId: null,
    personas,
    cockpit: { actionResults: [] }
  });

  const auto = buildStartViewState(
    runtime, config, profiles, true, cockpitFor("auto"),
    { sessions: [], activeSessionId: undefined }, readyModels()
  );
  assert.equal(auto.personas.supported, true);
  assert.equal(auto.personas.active, "architect");
  assert.deepEqual(
    auto.personas.options.map((option) => [option.name, option.effectiveMode, option.writeScoped, option.active]),
    [
      ["code", "auto", false, false],
      // Active permissions come from the host even if they differ from a persona default.
      ["architect", "auto", true, true],
      ["ask", "readonly", false, false]
    ]
  );

  const readonly = buildStartViewState(
    runtime, config, profiles, true, cockpitFor("readonly"),
    { sessions: [], activeSessionId: undefined }, readyModels()
  );
  // ...and never raises: every persona in a readonly session stays readonly.
  assert.deepEqual(
    readonly.personas.options.map((option) => option.effectiveMode),
    ["readonly", "readonly", "readonly"]
  );

  // Raw write globs must never ride the state post into the webview.
  for (const option of auto.personas.options) {
    assert.equal("allowWriteGlobs" in (option as unknown as Record<string, unknown>), false);
    assert.equal("allow_write_globs" in (option as unknown as Record<string, unknown>), false);
  }

  // An older host projection without the surface fails closed to a hidden control.
  const legacy = buildStartViewState(
    runtime, config, profiles, true,
    { sessionId: null, jobStatus: "idle", mode: null, items: [], lastAcceptedTaskRequestId: null, cockpit: { actionResults: [] } } as any,
    { sessions: [], activeSessionId: undefined }, readyModels()
  );
  assert.equal(legacy.personas.supported, false);
  assert.deepEqual(legacy.personas.options, []);
});

test("the start view validator accepts persona.set through the cockpit envelope and stays closed", () => {
  assert.equal(
    isStartViewMessage({ type: "cockpit", message: { type: "persona.set", name: "architect" } }),
    true
  );
  assert.equal(
    isStartViewMessage({ type: "cockpit", message: { type: "persona.set", name: "architect", mode: "auto" } }),
    false,
    "extra keys are rejected"
  );
  assert.equal(
    isStartViewMessage({ type: "cockpit", message: { type: "persona.set", name: "../evil" } }),
    false,
    "names are validated host-side too"
  );
  assert.equal(isStartViewMessage({ type: "cockpit", message: { type: "persona.set" } }), false);
});

test("recent tasks are titled by their first prompt and dated relatively, current task first", () => {
  const runtime: any = {
    cliHealth: { status: "ok", message: null },
    cliOrigin: { status: "verified", message: null },
    bridgeProcess: { status: "ready", message: null },
    bridgeProtocol: { status: "ok", message: null },
    sandboxDoctor: { status: "ok", message: null },
    runtimeSelection: { origin: "managed", message: null },
    compatibility: { protocol: { current: "1", compatible: true, direction: "aligned" }, features: {} },
    events: []
  };
  const cockpit: any = {
    sessionId: "s-new",
    jobStatus: "idle",
    mode: "review",
    items: [],
    lastAcceptedTaskRequestId: null,
    cockpit: { actionResults: [] }
  };
  const config: any = { defaultMode: "review", provider: "", baseUrl: "", defaultModel: "claude-opus-5" };
  const profiles: any = { supported: true, activeProfile: "", profiles: [] };
  const now = Date.now();
  const state = buildStartViewState(
    runtime,
    config,
    profiles,
    true,
    cockpit,
    {
      sessions: [
        { session_id: "s-old", workspace_root: "/w", mode: "auto", closed: false, last_job: { job_id: "j1", session_id: "s-old", status: "completed", completed_at: new Date(now - 3 * 3_600_000).toISOString() } },
        { session_id: "s-new", workspace_root: "/w", mode: "review", closed: false, last_job: { job_id: "j2", session_id: "s-new", status: "completed", completed_at: new Date(now - 60_000).toISOString() } },
        { session_id: "s-untitled", workspace_root: "/w", mode: "readonly", closed: true }
      ],
      activeSessionId: "s-new",
      titles: { "s-old": "Explain how the bridge restarts", "s-new": "Create SCRATCH.md at the repo root" }
    },
    readyModels()
  );

  assert.deepEqual(
    state.recentTasks.map((task) => [task.title, task.detail, task.current]),
    [
      ["Create SCRATCH.md at the repo root", "Review changes · Completed · 1m ago", true],
      ["Explain how the bridge restarts", "Auto-approve · Completed · 3h ago", false],
      ["Untitled task", "Read-only · Closed", false]
    ]
  );
  assert.equal(JSON.stringify(state.recentTasks).includes("Task s-"), false, "raw session ids never show as titles");
});

test("relativeTime reads like a person wrote it", () => {
  const now = Date.parse("2026-09-06T12:00:00Z");
  assert.equal(relativeTime(now - 10_000, now), "just now");
  assert.equal(relativeTime(now - 5 * 60_000, now), "5m ago");
  assert.equal(relativeTime(now - 2 * 3_600_000, now), "2h ago");
  assert.equal(relativeTime(now - 26 * 3_600_000, now), "yesterday");
  assert.equal(relativeTime(now - 3 * 86_400_000, now), "3d ago");
});

test("the slash catalog lists real slash commands with argument hints, gated by context", () => {
  const cockpit: any = {
    sessionId: null,
    jobStatus: "idle",
    mode: null,
    items: [],
    lastAcceptedTaskRequestId: null,
    cockpit: { actionResults: [] }
  };
  const rows = buildSlashCommandCatalog(cockpit, true, () => true);
  const byCommand = new Map(rows.map((row) => [row.command, row]));
  assert.equal(byCommand.get("/model")?.takesArgs, true);
  assert.equal(byCommand.get("/model")?.usage, "/model [name]");
  assert.equal(byCommand.get("/doctor")?.takesArgs, false);
  assert.equal(byCommand.has("/cancel"), false, "nothing is running, so /cancel is not offered");
  assert.equal(byCommand.has("/resume"), true);
  assert.equal(new Set(rows.map((row) => row.command.toLowerCase())).size, rows.length, "no duplicate spellings");

  const untrusted = buildSlashCommandCatalog(cockpit, false, () => true);
  assert.equal(untrusted.some((row) => row.command === "/resume"), false, "/resume needs Workspace Trust");
});
