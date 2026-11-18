import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

import { CockpitRuntimeState } from "../src/chat/CockpitRuntimeState";
import type { CockpitState } from "../src/chat/CockpitState";
import { ProtocolClientError } from "../src/client/AlysisBridgeClient";
import { PROTOCOL_VERSION, ProtocolEventEnvelope } from "../src/client/AlysisProtocol";

let workspaceTrusted = true;
const warnings: string[] = [];
const executedCommands: string[] = [];

class StubCancellationTokenSource {
  private listeners: Array<() => void> = [];
  public cancelled = false;
  public disposed = false;
  public readonly token = {
    isCancellationRequested: false,
    onCancellationRequested: (listener: () => void) => {
      this.listeners.push(listener);
      return { dispose: () => undefined };
    }
  };

  public cancel(): void {
    this.cancelled = true;
    this.token.isCancellationRequested = true;
    for (const listener of this.listeners) {
      listener();
    }
  }

  public dispose(): void {
    this.disposed = true;
    this.listeners = [];
  }
}

const cancellationSources: StubCancellationTokenSource[] = [];

const vscodeStub = {
  workspace: {
    get isTrusted() {
      return workspaceTrusted;
    },
    workspaceFolders: [{ uri: { scheme: "file", authority: "", fsPath: "/workspace/project" } }],
    getWorkspaceFolder: () => vscodeStub.workspace.workspaceFolders[0],
    textDocuments: [] as unknown[],
    openTextDocument: async () => ({}),
    asRelativePath: (uri: { fsPath?: string }) => uri.fsPath ?? ""
  },
  window: {
    activeTextEditor: undefined,
    showWarningMessage: async (message: string) => {
      warnings.push(message);
      return undefined;
    },
    showInformationMessage: async () => undefined,
    showErrorMessage: async () => undefined,
    showTextDocument: async () => undefined
  },
  commands: {
    executeCommand: async (command: string) => {
      executedCommands.push(command);
    }
  },
  env: { clipboard: { readText: async () => "" } },
  ViewColumn: { Beside: 2 },
  Uri: { joinPath: (...parts: unknown[]) => ({ parts }) },
  CancellationTokenSource: class extends StubCancellationTokenSource {
    public constructor() {
      super();
      cancellationSources.push(this);
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
const { ChatController } = require("../src/chat/ChatController") as typeof import("../src/chat/ChatController");
moduleLoader._load = originalLoad;

test("readiness reaches the published state and locks the composer when the engine is down", async () => {
  const runtime = new CockpitRuntimeState();
  const controller = controllerFor(runtime);
  controller.setProviderReadiness(() => ({ status: "ready", reason: "ready" }));
  const states: CockpitState[] = [];
  controller.onDidChangeState((state) => states.push(state));

  runtime.setCliHealth("unreachable", "Alysis Code CLI could not be launched.");
  controller.refreshCockpit();
  await delay(40);

  const published = states.at(-1);
  assert.ok(published, "a state was published");
  assert.equal(published.readiness.ok, false);
  assert.deepEqual(published.readiness.blockers.map((blocker) => blocker.id), ["runtime_missing"]);
  assert.equal(published.readiness.blockers[0].actions[0].command, "alysis.locateCli");

  runtime.setCliHealth("ok", "Alysis Code IDE bridge is connected.");
  controller.refreshCockpit();
  await delay(40);
  assert.equal(states.at(-1)?.readiness.ok, true);
  await controller.shutdown({ shutdownBridge: false });
});

test("provider readiness alone can never unlock a task when the CLI is missing", async () => {
  const runtime = new CockpitRuntimeState();
  const controller = controllerFor(runtime);
  controller.setProviderReadiness(() => ({ status: "ready", reason: "ready" }));
  runtime.setCliHealth("unreachable", null);

  assert.equal(controller.primaryCockpitState().readiness.ok, false);
  await controller.shutdown({ shutdownBridge: false });
});

test("an unwired provider gate publishes as not-ready instead of an accidental green light", async () => {
  const controller = controllerFor();

  const readiness = controller.primaryCockpitState().readiness;
  assert.equal(readiness.ok, false);
  assert.deepEqual(readiness.blockers.map((blocker) => blocker.id), ["provider_loading"]);
  await controller.shutdown({ shutdownBridge: false });
});

test("streaming deltas coalesce to frame cadence and the turn's final state is never dropped", async () => {
  const bridge = bridgeMock();
  const controller = controllerFor(undefined, bridge);
  const states: CockpitState[] = [];
  controller.onDidChangeState((state) => states.push(state));
  await controller.testSubmit("stream please");
  await delay(40);
  states.length = 0;

  for (let index = 0; index < 40; index += 1) {
    bridge.emitEvent(event({ sequence: index + 10, type: "message_delta", payload: { text: `chunk ${index}` } }));
  }
  // Every delta published synchronously before; now the burst costs at most one rebuild.
  assert.equal(states.length, 0, "a streaming burst does not publish per token");
  await delay(40);
  assert.equal(states.length, 1, "the burst flushes once at frame cadence");

  states.length = 0;
  bridge.emitEvent(event({ sequence: 100, type: "message_end", payload: {} }));
  // A terminal event flushes synchronously: the final state cannot sit in a pending timer.
  assert.equal(states.length, 1, "the end of a turn publishes immediately");
  const finalItems = states[0].items.filter((item) => item.kind === "assistant");
  assert.equal(finalItems.at(-1)?.text.includes("chunk 39"), true);
  await controller.shutdown({ shutdownBridge: false });
});

test("an error is classified into an actionable card and flushed without delay", async () => {
  const bridge = bridgeMock();
  bridge.sendChat = async () => {
    throw Object.assign(new Error("Request failed with status 401: invalid api key"), { code: "auth_error" });
  };
  const output: string[] = [];
  const controller = controllerFor(undefined, bridge, output);
  const states: CockpitState[] = [];
  controller.onDidChangeState((state) => states.push(state));

  await controller.testSubmit("do the thing");

  const errorItem = controller.conversationState().items.find((item) => item.kind === "error");
  assert.ok(errorItem, "an error card was added");
  assert.equal(errorItem.error?.kind, "auth_invalid");
  assert.equal(errorItem.title, "Your API key was rejected");
  assert.equal(errorItem.actions?.[0]?.command, "alysis.configureProvider");
  assert.doesNotMatch(errorItem.title ?? "", /401|auth_error/);
  assert.doesNotMatch(errorItem.detail ?? "", /401|auth_error/);
  // Diagnostics are preserved: the raw text still reaches the output channel and the item body.
  assert.equal(output.some((line) => line.includes("401")), true);
  assert.match(errorItem.text, /401/);
  assert.ok(states.length > 0, "the error flushed a state publish");
  await controller.shutdown({ shutdownBridge: false });
});

test("a cancellation is rendered as a notice, not as a failure card", async () => {
  const bridge = bridgeMock();
  bridge.sendChat = async () => {
    throw Object.assign(new Error("The run was cancelled."), { code: "cancelled" });
  };
  const controller = controllerFor(undefined, bridge);

  await controller.testSubmit("stop this");

  const items = controller.conversationState().items;
  assert.equal(items.some((item) => item.kind === "error"), false);
  assert.equal(items.some((item) => item.kind === "notice" && /stopped this task/i.test(item.text)), true);
  await controller.shutdown({ shutdownBridge: false });
});

test("verification runs with a cancellable signal and reports when it is skipped", async () => {
  const bridge = bridgeMock();
  const requests: Array<{ settleMs?: number; timeoutMs?: number; signal?: AbortSignal }> = [];
  const verification = {
    captureBaseline: () => ({ capturedAt: "now" }),
    verifyAfterChanges: async (_baseline: unknown, request?: { settleMs?: number; timeoutMs?: number; signal?: AbortSignal }) => {
      requests.push(request ?? {});
      return {
        status: "passed",
        blocksCompletion: false,
        reason: "no new diagnostics",
        introducedCounts: { error: 0, warning: 0, information: 0, hint: 0 },
        clearedAttachedCount: 0,
        requiredAttachedCount: 0
      };
    }
  };
  const controller = controllerFor(undefined, bridge, undefined, verification);
  cancellationSources.length = 0;

  await controller.testSubmit("verify me");
  await delay(20);

  assert.equal(requests.length, 1, "verification ran once");
  assert.ok(requests[0].signal, "a cancellation-backed signal was passed");
  assert.equal(requests[0].signal?.aborted, false);
  assert.equal(requests[0].settleMs, 350);
  assert.equal(requests[0].timeoutMs, 15_000);
  assert.equal(cancellationSources.length, 1);
  assert.equal(cancellationSources[0].disposed, true, "the token source is disposed after the run");
  await controller.shutdown({ shutdownBridge: false });
});

test("an untrusted folder says verification was skipped instead of silently dropping it", async () => {
  workspaceTrusted = false;
  const bridge = bridgeMock();
  const controller = controllerFor(undefined, bridge, undefined, {
    captureBaseline: () => ({ capturedAt: "now" }),
    verifyAfterChanges: async () => ({
      status: "passed",
      blocksCompletion: false,
      reason: "",
      introducedCounts: { error: 0, warning: 0, information: 0, hint: 0 },
      clearedAttachedCount: 0,
      requiredAttachedCount: 0
    })
  });

  await controller.testSubmit("verify me");

  assert.equal(
    controller.conversationState().items.some(
      (item) => item.kind === "notice" && /will not verify this change/i.test(item.text)
    ),
    true
  );
  workspaceTrusted = true;
  await controller.shutdown({ shutdownBridge: false });
});

test("a cockpit submit never tries to open a second chat surface", async () => {
  const bridge = bridgeMock();
  const controller = controllerFor(undefined, bridge);
  const reveals: string[] = [];
  controller.setPrimaryChatView({
    reveal: async () => {
      reveals.push("reveal");
    },
    prefill: async () => undefined
  });

  await controller.handlePrimaryCockpitAction({ type: "submit", text: "hello", requestId: "request-1" });

  assert.deepEqual(reveals, [], "submitting from the sidebar does not re-reveal or spawn a panel");
  assert.equal(
    controller.conversationState().items.some((item) => item.kind === "user" && item.text === "hello"),
    true
  );
  await controller.shutdown({ shutdownBridge: false });
});

test("the persona list loads when a session starts and carries the cockpit surface", async () => {
  const bridge = personaBridgeMock();
  const controller = controllerFor(undefined, bridge);
  await controller.ensureLiveSession();
  await delay(40);

  const personas = controller.primaryCockpitState().personas;
  assert.equal(personas.supported, true);
  assert.equal(personas.enabled, true);
  assert.equal(personas.active, "code");
  assert.equal(personas.activeSource, "user");
  assert.equal(personas.available.length, 2);
  assert.equal(personas.available[1].name, "architect");
  assert.equal(personas.available[1].defaultExecMode, "review");
  assert.equal(personas.available[1].writeScoped, true, "allow_write_globs projects to a boolean only");
  assert.equal(
    "allowWriteGlobs" in (personas.available[1] as unknown as Record<string, unknown>),
    false,
    "raw globs never reach the surface"
  );
  await controller.shutdown({ shutdownBridge: false });
});

test("personas stay gated with the standard capability affordance on an older CLI", async () => {
  const bridge = bridgeMock();
  (bridge as any).supportsMethod = (method: string) => !method.startsWith("session.persona");
  const controller = controllerFor(undefined, bridge);
  await controller.ensureLiveSession();
  await delay(40);

  const personas = controller.primaryCockpitState().personas;
  assert.equal(personas.supported, false);
  assert.match(personas.reason ?? "", /Needs a newer Alysis Code CLI/);
  assert.deepEqual(personas.available, []);
  await controller.shutdown({ shutdownBridge: false });
});

test("persona_changed updates the active persona and mode display and publishes immediately", async () => {
  const bridge = personaBridgeMock();
  const controller = controllerFor(undefined, bridge);
  await controller.ensureLiveSession();
  await delay(40);
  const states: CockpitState[] = [];
  controller.onDidChangeState((state) => states.push(state));

  bridge.emitEvent(event({
    sequence: 50,
    job_id: null,
    type: "persona_changed",
    payload: { persona: "architect", effective_mode: "review", source: "model" }
  }));

  assert.ok(states.length >= 1, "a persona change flushes without waiting for the coalescing window");
  const latest = states.at(-1);
  assert.equal(latest?.personas.active, "architect");
  assert.equal(latest?.personas.activeSource, "model");
  assert.equal(latest?.mode, "review", "the effective mode display follows the clamp");
  assert.equal(
    latest?.items.some((item) => item.kind === "notice" && /architect/.test(item.text)),
    true,
    "a switch this controller did not initiate is narrated"
  );
  await controller.shutdown({ shutdownBridge: false });
});

test("setting a persona applies the clamped mode and narrates the outcome", async () => {
  const bridge = personaBridgeMock();
  const controller = controllerFor(undefined, bridge);
  await controller.ensureLiveSession();
  await delay(40);

  await controller.setPersona("architect");
  await delay(40);

  const state = controller.primaryCockpitState();
  assert.equal(state.personas.active, "architect");
  assert.equal(state.mode, "review");
  assert.equal(
    state.items.some((item) => item.kind === "notice" && item.text === "Persona set: architect (review)."),
    true
  );
  assert.deepEqual(bridge.personaSetCalls, [{ session_id: "chat-session", persona: "architect" }]);
  await controller.shutdown({ shutdownBridge: false });
});

test("persona_change_busy surfaces a notice, never an error card", async () => {
  const bridge = personaBridgeMock();
  bridge.sessionPersonaSet = async () => {
    throw new ProtocolClientError("persona_change_busy", "a task is running");
  };
  const controller = controllerFor(undefined, bridge);
  await controller.ensureLiveSession();
  await delay(40);

  await controller.setPersona("architect");

  const items = controller.conversationState().items;
  assert.equal(
    items.some((item) => item.kind === "notice" && item.text === "Finish or stop the current task first."),
    true
  );
  assert.equal(items.some((item) => item.kind === "error"), false, "busy is guidance, not a failure");
  await controller.shutdown({ shutdownBridge: false });
});

test("a running task blocks persona changes locally instead of queueing them", async () => {
  const bridge = personaBridgeMock();
  bridge.sendChat = async () => ({ session_id: "chat-session", job_id: "chat-job", status: "started" });
  const controller = controllerFor(undefined, bridge);
  await controller.testSubmit("start a task");

  assert.equal(controller.hasActiveJob(), true, "the submitted job is still foreground");
  await controller.setPersona("architect");

  assert.deepEqual(bridge.personaSetCalls, [], "no persona change is queued behind the running task");
  assert.equal(
    controller.conversationState().items.some(
      (item) => item.kind === "notice" && item.text === "Finish or stop the current task first."
    ),
    true
  );
  await controller.shutdown({ shutdownBridge: false });
});

function personaBridgeMock(): ReturnType<typeof bridgeMock> & {
  personaSetCalls: Array<{ session_id: string; persona: string }>;
  sessionPersonasList: (params: { session_id: string }) => Promise<Record<string, unknown>>;
  sessionPersonaSet: (params: { session_id: string; persona: string }) => Promise<Record<string, unknown>>;
} {
  const bridge = bridgeMock() as ReturnType<typeof bridgeMock> & {
    personaSetCalls: Array<{ session_id: string; persona: string }>;
    sessionPersonasList: (params: { session_id: string }) => Promise<Record<string, unknown>>;
    sessionPersonaSet: (params: { session_id: string; persona: string }) => Promise<Record<string, unknown>>;
  };
  let active = "code";
  bridge.personaSetCalls = [];
  bridge.sessionPersonasList = async () => ({
    enabled: true,
    active,
    active_source: "user",
    personas: [
      {
        name: "code",
        description: "Full implementation persona.",
        default_exec_mode: "",
        model_role: "coding",
        source_scope: "builtin",
        allow_write_globs: []
      },
      {
        name: "architect",
        description: "Planning persona; writes markdown only.",
        default_exec_mode: "review",
        model_role: "planner",
        source_scope: "builtin",
        allow_write_globs: ["**/*.md"]
      }
    ]
  });
  bridge.sessionPersonaSet = async (params: { session_id: string; persona: string }) => {
    bridge.personaSetCalls.push(params);
    active = params.persona;
    return {
      persona: params.persona,
      effective_mode: params.persona === "architect" ? "review" : "readonly",
      model_role: params.persona === "architect" ? "planner" : "coding",
      changed: true
    };
  };
  return bridge;
}

function controllerFor(
  runtime?: CockpitRuntimeState,
  bridge: ReturnType<typeof bridgeMock> = bridgeMock(),
  output: string[] = [],
  diagnosticsVerification?: unknown
): InstanceType<typeof ChatController> {
  const controller = new ChatController(
    bridge as never,
    () => config(),
    { getApiKey: async () => "secret" } as never,
    {
      setError: () => undefined,
      setActiveRun: () => undefined,
      setBridgeOk: () => undefined,
      setIdle: () => undefined,
      setApprovalNeeded: () => undefined
    } as never,
    { setSessions: () => undefined, setActiveSession: () => undefined } as never,
    { appendLine: (line: string) => output.push(line) } as never,
    runtime,
    undefined,
    undefined,
    diagnosticsVerification as never
  );
  controller.setCockpitProvider(() => cockpitProvider(runtime ? { runtime: runtime.snapshot() } : {}));
  return controller;
}

function config(): never {
  return {
    cliPath: "alysis",
    defaultMode: "readonly",
    defaultModel: "",
    baseUrl: "",
    provider: "",
    transport: "stdio",
    sandboxProfile: "default",
    showStatusBar: true,
    autoStartBridge: false,
    security: {
      isWorkspaceTrusted: true,
      ignoredWorkspaceSettings: [],
      workspaceRoots: ["/workspace/project"],
      cliPath: {
        value: "alysis",
        source: "default",
        trusted: true,
        executionAllowed: true,
        apiKeyForwardingAllowed: true
      }
    }
  } as never;
}

function cockpitProvider(overrides: Record<string, unknown>): never {
  return {
    status: {
      mode: "readonly",
      model: "default",
      provider: "default",
      sandbox: "default",
      workspaceTrusted,
      bridgeStatus: { state: "idle", text: "Alysis Code: idle", tooltip: "Alysis Code is ready", visible: true },
      cliTrusted: true,
      cliReason: null,
      providerProfile: { supported: false, activeProfile: "", profiles: [] },
      sessionModel: {
        supported: false,
        switchSupported: false,
        sessionId: null,
        model: "",
        provider: "",
        profile: null,
        source: "",
        error: null
      },
      runtime: new CockpitRuntimeState().snapshot(),
      ...(overrides.runtime ? { runtime: overrides.runtime } : {})
    },
    browser: { supported: false, reason: null, browsers: [], active: null, preview: null, busy: false },
    actionResults: []
  } as never;
}

function bridgeMock() {
  const listeners = new Map<string | symbol, Array<(...args: unknown[]) => void>>();
  const bridge = {
    on: (name: string | symbol, listener: (...args: unknown[]) => void) => {
      const current = listeners.get(name) ?? [];
      current.push(listener);
      listeners.set(name, current);
    },
    off: (name: string | symbol, listener: (...args: unknown[]) => void) => {
      listeners.set(name, (listeners.get(name) ?? []).filter((candidate) => candidate !== listener));
    },
    emitEvent: (value: ProtocolEventEnvelope) => {
      for (const listener of listeners.get("event") ?? []) {
        listener(value);
      }
    },
    supportsMethod: () => true,
    supportsEvent: () => false,
    supportsFeature: () => true,
    ensureStarted: async () => undefined,
    createSession: async () => ({ session_id: "chat-session", workspace_root: "/workspace/project", mode: "readonly" }),
    sendChat: async () => ({ session_id: "chat-session", job_id: "chat-job", status: "completed" }),
    startRun: async () => ({ session_id: "chat-session", job_id: "chat-job", status: "completed" }),
    cancelSession: async () => ({ status: "closed" }),
    respondApproval: async () => ({ allow: false, allow_for_session: false, allow_for_session_warning: null }),
    jobStatus: async () => ({ job_id: "chat-job", session_id: "chat-session", status: "completed" }),
    sessionList: async () => ({ sessions: [] }),
    getEvents: async () => ({ events: [], truncated: false }),
    artifactList: async () => ({ artifacts: [], truncated: false }),
    artifactRead: async () => ({}),
    shutdown: async () => undefined
  };
  return bridge;
}

function event(overrides: Partial<ProtocolEventEnvelope>): ProtocolEventEnvelope {
  return {
    protocol_version: PROTOCOL_VERSION,
    session_id: "chat-session",
    run_id: null,
    job_id: "chat-job",
    sequence: 1,
    timestamp: "2026-05-19T10:00:00.000Z",
    type: "message_delta",
    payload: {},
    ...overrides
  } as ProtocolEventEnvelope;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
