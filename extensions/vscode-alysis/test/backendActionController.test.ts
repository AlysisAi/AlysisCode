import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import Module from "node:module";
import { resolve } from "node:path";
import test from "node:test";

import { ActionResultStore } from "../src/backend/ActionResultStore";
import { BACKEND_ACTIONS } from "../src/backend/BackendActionMetadata";
import { SLASH_COMMANDS, parseSlashCommand } from "../src/slash/SlashCommandRegistry";

const disposable = { dispose: () => undefined };
const registeredCommands = new Map<string, (...args: unknown[]) => unknown>();
const nativeCommands: string[] = [];
const infos: string[] = [];
const warnings: string[] = [];
const outputLines: string[] = [];
let quickPickItems: Array<{ label: string; description?: string }> = [];
let quickPickLabel: string | undefined;
let inputValues: string[] = [];
let openDialogValues: Array<Array<{ fsPath: string }> | undefined> = [];
let openedExternalUris: string[] = [];
let workspaceTrusted = true;
let workspaceFolders: Array<{ uri: { fsPath: string; scheme?: string; authority?: string } }> | undefined = [
  { uri: { fsPath: "/workspace/project", scheme: "file", authority: "" } }
];
let textDocuments: any[] = [];
let activeEditorUri: { fsPath: string; scheme?: string; authority?: string } | undefined;
let remoteName: string | undefined;
let warningHandler: ((message: string, items: string[]) => Promise<string | undefined> | string | undefined) | undefined;
let quickPickHandler: ((items: any[]) => Promise<any>) | undefined;
let inputHandler: (() => Promise<string | undefined>) | undefined;

const vscodeStub = {
  ProgressLocation: { Notification: 15 },
  Uri: {
    parse: (value: string) => ({
      scheme: new URL(value).protocol.replace(":", ""),
      toString: () => value
    })
  },
  env: {
    openExternal: async (uri: { toString(): string }) => {
      openedExternalUris.push(uri.toString());
      return true;
    }
  },
  commands: {
    executeCommand: async (command: string) => { nativeCommands.push(command); },
    registerCommand: (command: string, callback: (...args: unknown[]) => unknown) => {
      registeredCommands.set(command, callback);
      return disposable;
    }
  },
  workspace: {
    get isTrusted() {
      return workspaceTrusted;
    },
    get workspaceFolders() {
      return workspaceFolders;
    },
    get textDocuments() {
      return textDocuments;
    },
    getWorkspaceFolder: (uri: { fsPath: string }) =>
      workspaceFolders?.find((folder) => uri.fsPath === folder.uri.fsPath || uri.fsPath.startsWith(`${folder.uri.fsPath}/`))
  },
  window: {
    get activeTextEditor() {
      return activeEditorUri ? { document: { uri: activeEditorUri } } : undefined;
    },
    showQuickPick: async (items: Array<{ label: string; description?: string }>) => {
      if (quickPickHandler) return quickPickHandler(items);
      quickPickItems = items.map((item) => ({ label: item.label, description: item.description }));
      if (quickPickLabel) {
        return items.find((item) => item.label === quickPickLabel);
      }
      return items[0];
    },
    showInputBox: async () => inputHandler ? inputHandler() : inputValues.shift(),
    showOpenDialog: async () => openDialogValues.shift(),
    withProgress: async (
      _options: unknown,
      task: (
        progress: { report(): void },
        token: {
          isCancellationRequested: boolean;
          onCancellationRequested(callback: () => void): { dispose(): void };
        }
      ) => Promise<unknown>
    ) => task(
      { report: () => undefined },
      { isCancellationRequested: false, onCancellationRequested: () => disposable }
    ),
    showInformationMessage: async (message: string) => {
      infos.push(message);
      return undefined;
    },
    showWarningMessage: async (message: string, _options?: unknown, ...items: string[]) => {
      warnings.push(message);
      if (warningHandler) {
        return warningHandler(message, items);
      }
      return items[0];
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

const { BackendActionController } =
  require("../src/backend/BackendActionController") as typeof import("../src/backend/BackendActionController");
const { registerReviewCommands } =
  require("../src/commands/reviewCommands") as typeof import("../src/commands/reviewCommands");
moduleLoader._load = originalLoad;

test("backend action registry entries declare capability and trust metadata", () => {
  assert.ok(BACKEND_ACTIONS.length > 40);
  for (const action of BACKEND_ACTIONS) {
    assert.ok(action.commandId.startsWith("alysis.backend."), action.id);
    assert.ok(action.requiredMethods.length > 0, action.id);
    assert.equal(typeof action.mutates, "boolean", action.id);
    assert.equal(typeof action.mayMutate, "boolean", action.id);
    assert.equal(typeof action.mutationDeterminedByParams, "boolean", action.id);
    assert.equal(typeof action.mutationRequiresWorkspaceTrust, "boolean", action.id);
    assert.equal(typeof action.workspaceTrustRequired, "boolean", action.id);
    assert.equal(typeof action.workspaceRequired, "boolean", action.id);
    assert.equal(typeof action.requiresActiveSession, "boolean", action.id);
    assert.ok(action.parameterStrategy.length > 0, action.id);
  }
});

test("backend action metadata is covered by the IDE protocol method contract", () => {
  const contract = loadProtocolContract();

  for (const action of BACKEND_ACTIONS) {
    for (const method of action.requiredMethods) {
      assert.ok(contract[method], `${action.id} requires undocumented method ${method}`);
    }
    if (action.mutates && !action.workspaceTrustRequired) {
      assert.ok(
        action.workspaceTrustExemptionRationale?.trim(),
        `${action.id} mutates without Workspace Trust and needs a rationale`
      );
    }
    if (action.mayMutate && !action.mutationRequiresWorkspaceTrust) {
      assert.ok(
        action.workspaceTrustExemptionRationale?.trim(),
        `${action.id} may mutate without Workspace Trust and needs a rationale`
      );
    }
    if (!action.mutates && action.mayMutate) {
      assert.equal(
        action.mutationDeterminedByParams,
        true,
        `${action.id} can mutate dynamically but does not declare parameter-based mutation`
      );
    }
    const requiredMethodCanMutate = action.requiredMethods.some((method) => contract[method]?.mutates === true);
    if (requiredMethodCanMutate && !action.mutates) {
      assert.equal(action.mayMutate, true, `${action.id} references a mutating method but is not mayMutate`);
      assert.equal(
        action.mutationDeterminedByParams,
        true,
        `${action.id} references a mutating method but lacks dynamic mutation metadata`
      );
    }
  }
});

test("backend action slash aliases are unique and route to valid backend actions", () => {
  const seen = new Map<string, string>();
  for (const action of BACKEND_ACTIONS) {
    for (const alias of action.slashAliases) {
      const previous = seen.get(alias);
      assert.equal(previous, undefined, `${alias} is shared by ${previous} and ${action.id}`);
      seen.set(alias, action.id);
      const parsed = parseSlashCommand(`${alias} demo`);
      if (parsed.id === "backendAction") {
        assert.equal(parsed.backendActionId, action.id, alias);
      } else {
        // A core slash command may deliberately supersede a backend alias when it owns the richer
        // flow — /permissions's trust gate, /model's provider-and-model picker. That is only legitimate
        // if a core command actually claims the spelling; anything else means the alias silently
        // stopped routing anywhere.
        const core = SLASH_COMMANDS.find(
          (command) => command.command.toLowerCase() === alias.toLowerCase()
        );
        assert.ok(core, `${alias} routed to ${parsed.id} but no core command claims that spelling`);
        assert.equal(parsed.id, core.id, alias);
      }
    }
  }
});

test("backend action controller hides unsupported actions from grouped QuickPick", async () => {
  reset();
  const bridge = bridgeMock(["config.get"]);
  const controller = controllerFor(bridge);
  quickPickLabel = "Show Config";

  const result = await controller.runGroup("profiles");

  assert.equal(result?.action.id, "config.get");
  assert.deepEqual(bridge.calls.map((call: { method: string }) => call.method), ["config.get"]);
});

test("backend action controller honors explicit unsupported capability metadata", async () => {
  reset();
  const bridge = bridgeMock(
    ["config.get"],
    { "management.methods.config.get.supported": false }
  );
  const controller = controllerFor(bridge, { sessionId: "session-1" }, async () => undefined);

  const result = await controller.runGroup("profiles");

  assert.equal(result, undefined);
  assert.deepEqual(bridge.calls, []);
  assert.match(warnings[0], /not available with the connected Alysis Code CLI/);
});

test("high-risk lifecycle actions stay capability-gated or absent", () => {
  reset();
  const mcpLogin = BACKEND_ACTIONS.find((action) => action.id === "mcp.auth.login.start");
  assert.ok(mcpLogin);
  const bridge = bridgeMock(
    ["mcp.auth.login.start"],
    { "management.methods.mcp.auth.login.start.supported": false }
  );
  const controller = controllerFor(bridge);

  assert.equal(controller.isActionSupported(mcpLogin), false);
  assert.equal(BACKEND_ACTIONS.some((action) => action.id === "hooks.watch"), false);
  assert.equal(BACKEND_ACTIONS.some((action) => action.id.startsWith("hooks.watch.")), false);
  assert.equal(BACKEND_ACTIONS.some((action) => action.id.startsWith("mcp.auth.login.") && action.id !== "mcp.auth.login.start"), false);
});

test("MCP OAuth login opens only the bridge-provided HTTPS URL and polls to completion", async () => {
  reset();
  const bridge = bridgeMock([
    "mcp.auth.login.start",
    "mcp.auth.login.status",
    "mcp.auth.login.cancel"
  ]);
  bridge.mcpAuthLoginStart = async (params: unknown) => record(
    bridge,
    "mcp.auth.login.start",
    params,
    {
      flow_id: "flow-1",
      server_id: "demo",
      kind: "authorization_code",
      state: "pending",
      created_at: 1,
      updated_at: 1,
      expires_at: 301,
      terminal_at: null,
      error_code: null,
      browser_url: "https://auth.example/authorize?state=public-state",
      supported: true,
      will_block: false,
      browser_opened_by_bridge: false
    }
  );
  bridge.mcpAuthLoginStatus = async (params: unknown) => record(
    bridge,
    "mcp.auth.login.status",
    params,
    {
      flow_id: "flow-1",
      server_id: "demo",
      kind: "authorization_code",
      state: "completed",
      created_at: 1,
      updated_at: 2,
      expires_at: 301,
      terminal_at: 2,
      error_code: null
    }
  );
  bridge.mcpAuthLoginCancel = async (params: unknown) => record(
    bridge,
    "mcp.auth.login.cancel",
    params,
    { flow_id: "flow-1", server_id: "demo", state: "cancelled" }
  );
  const controller = controllerFor(bridge);

  const outcome = await controller.executeAction("mcp.auth.login.start", "demo");

  assert.equal((outcome.result as { state: string }).state, "completed");
  assert.deepEqual(openedExternalUris, ["https://auth.example/authorize?state=public-state"]);
  assert.deepEqual(
    bridge.calls.map((call: { method: string }) => call.method),
    ["mcp.auth.login.start", "mcp.auth.login.status"]
  );
  assert.equal(
    (bridge.calls[0].params as { workspace_trusted: boolean }).workspace_trusted,
    true
  );
});

test("MCP OAuth login fails closed on Remote-SSH before starting a bridge flow", async () => {
  reset();
  remoteName = "ssh-remote";
  const bridge = bridgeMock([
    "mcp.auth.login.start",
    "mcp.auth.login.status",
    "mcp.auth.login.cancel"
  ]);
  let credentialBridgeCalls = 0;
  const controller = controllerFor(bridge, undefined, async () => {
    credentialBridgeCalls += 1;
  });
  const action = BACKEND_ACTIONS.find((candidate) => candidate.id === "mcp.auth.login.start");
  assert.ok(action);

  assert.equal(controller.isActionSupported(action), false);
  await assert.rejects(
    controller.executeAction("mcp.auth.login.start", "demo"),
    (error: any) => error?.code === "mcp_oauth_remote_unavailable"
      && /loopback callback runs remotely/i.test(String(error.message))
  );
  assert.equal(credentialBridgeCalls, 0);
  assert.deepEqual(bridge.calls, []);
  assert.deepEqual(openedExternalUris, []);
});

test("profile command uses the guarded live-provider route", async () => {
  reset();
  const bridge = bridgeMock(["profile.use"]);
  const controller = controllerFor(bridge);
  const activated: string[] = [];
  (controller as any).deps.useProfile = async (name: string) => {
    activated.push(name);
    return { active_profile: name, changed: true };
  };
  await controller.executeAction("profile.use", "demo");
  assert.deepEqual(activated, ["demo"]);
  assert.equal(bridge.calls.some((call: { method: string }) => call.method === "profile.use"), false);
});

test("backend action controller blocks mutating actions in untrusted workspaces", async () => {
  reset();
  workspaceTrusted = false;
  const bridge = bridgeMock(["profile.use"]);
  const controller = controllerFor(bridge);

  await assert.rejects(
    controller.executeAction("profile.use", "demo"),
    /requires Workspace Trust/
  );
  assert.deepEqual(bridge.calls, []);
});

test("workspace-mutating backend actions fail before confirmation or invocation when an owned file is dirty", async () => {
  reset();
  textDocuments = [dirtyDocument("/workspace/project/src/dirty.ts")];
  const bridge = bridgeMock(["checkpoint.revert"]);
  const controller = controllerFor(bridge);

  await assert.rejects(
    controller.executeAction("checkpoint.revert", "checkpoint-1"),
    /Save or revert 1 unsaved workspace file before using Revert to Checkpoint/
  );
  assert.deepEqual(bridge.calls, []);
  assert.deepEqual(warnings, []);
});

test("workspace-mutating backend actions recheck dirty buffers after approval and before invocation", async () => {
  reset();
  const bridge = bridgeMock(["checkpoint.revert"]);
  const controller = controllerFor(bridge);
  warningHandler = (_message, items) => {
    textDocuments = [dirtyDocument("/workspace/project/src/became-dirty.ts")];
    return items[0];
  };

  await assert.rejects(
    controller.executeAction("checkpoint.revert", "checkpoint-1"),
    /Save or revert 1 unsaved workspace file before using Revert to Checkpoint/
  );
  assert.equal(warnings.length, 1);
  assert.deepEqual(bridge.calls, []);
});

test("parameter-selected mutations confirm under the gated action they actually reach", async () => {
  for (const scenario of [
    { actionId: "session.subagents.status", args: "off", method: "session.subagents.setEnabled", title: "Toggle Subagents" },
    { actionId: "session.trace.status", args: "clear", method: "session.trace.clear", title: "Clear Trace Events" }
  ]) {
    reset();
    const bridge = bridgeMock([scenario.actionId, scenario.method]);
    const controller = controllerFor(bridge, { sessionId: "session-1" });
    warningHandler = () => undefined;

    const cancelled = await controller.executeAction(scenario.actionId, scenario.args);
    assert.equal((cancelled.result as { cancelled?: boolean }).cancelled, true, scenario.actionId);
    assert.deepEqual(bridge.calls, [], scenario.actionId);
    assert.equal(warnings.some((message) => message.includes(scenario.title)), true, scenario.actionId);

    warningHandler = (_message, items) => items[0];
    await controller.executeAction(scenario.actionId, scenario.args);
    assert.deepEqual(bridge.calls.map((call: any) => call.method), [scenario.method], scenario.actionId);
  }
});

test("read-only forms of the same actions never prompt for confirmation", async () => {
  for (const scenario of [
    { actionId: "session.subagents.status", method: "session.subagents.status" },
    { actionId: "session.trace.status", method: "session.trace.status" }
  ]) {
    reset();
    const bridge = bridgeMock([scenario.actionId]);
    const controller = controllerFor(bridge, { sessionId: "session-1" });

    await controller.executeAction(scenario.actionId, "status");

    assert.deepEqual(warnings, [], scenario.actionId);
    assert.deepEqual(bridge.calls.map((call: any) => call.method), [scenario.method], scenario.actionId);
  }
});

test("workspace-scoped path parameters are validated before they reach the bridge", async () => {
  reset();
  const bridge = bridgeMock(["session.setActiveWorkdir", "session.images.add"]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await assert.rejects(
    controller.executeAction("session.setActiveWorkdir", "../../../etc"),
    /must stay inside the open workspace folder/
  );
  await assert.rejects(
    controller.executeAction("session.images.add", "/etc/passwd.png"),
    /must stay inside the open workspace folder/
  );
  assert.deepEqual(bridge.calls, []);

  await controller.executeAction("session.setActiveWorkdir", "src/app");
  await controller.executeAction("session.images.add", "/workspace/project/docs/shot.png");
  assert.deepEqual(bridge.calls.map((call: any) => call.method), [
    "session.setActiveWorkdir",
    "session.images.add"
  ]);
});

test("changing the active workdir requires Workspace Trust", async () => {
  reset();
  workspaceTrusted = false;
  const bridge = bridgeMock(["session.setActiveWorkdir"]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await assert.rejects(
    controller.executeAction("session.setActiveWorkdir", "src/app"),
    /requires Workspace Trust/
  );
  assert.deepEqual(bridge.calls, []);
});

test("FE-12: session mutations (compact/clear/resume) are blocked in untrusted workspaces", async () => {
  for (const actionId of ["session.compact", "session.clear", "session.resume"]) {
    reset();
    workspaceTrusted = false;
    const bridge = bridgeMock([actionId]);
    const controller = controllerFor(bridge, { sessionId: "session-1" });
    await assert.rejects(controller.executeAction(actionId, "session-1"), /requires Workspace Trust/, actionId);
    assert.deepEqual(bridge.calls, [], actionId);
  }
});

test("FE-13: the newly-wired gap actions are cataloged with the right safety flags", () => {
  const byId = (id: string) => BACKEND_ACTIONS.find((action) => action.id === id);
  // Read-only diagnostics: no mutation/trust/confirm.
  for (const id of ["update.check", "sandbox.doctor", "doctor.bundle", "doctor.providers"]) {
    const action = byId(id);
    assert.ok(action, `${id} cataloged`);
    assert.equal(action?.mutates, false, id);
    assert.equal(action?.workspaceTrustRequired, false, id);
  }
  // Mutations: trust + confirm.
  for (const id of ["config.set", "sandbox.setup", "sandbox.pull", "mcp.auth.login.start"]) {
    const action = byId(id);
    assert.ok(action, `${id} cataloged`);
    assert.equal(action?.mutates, true, id);
    assert.equal(action?.workspaceTrustRequired, true, id);
    assert.equal(action?.confirmationRequired, true, id);
  }
});

test("FE-13: gap actions gate on the bridge advertising their method (present->enabled, absent->gated)", () => {
  reset();
  const byId = (id: string) => BACKEND_ACTIONS.find((action) => action.id === id)!;
  // config.set requires both config.set and config.schema (it validates against the schema first).
  const advertised = controllerFor(bridgeMock(["doctor.bundle", "config.set", "config.schema", "update.check"]));
  assert.equal(advertised.isActionSupported(byId("doctor.bundle")), true);
  assert.equal(advertised.isActionSupported(byId("config.set")), true);
  assert.equal(advertised.isActionSupported(byId("update.check")), true);
  assert.equal(advertised.isActionSupported(byId("sandbox.pull")), false, "not advertised -> gated");
  const none = controllerFor(bridgeMock([]));
  assert.equal(none.isActionSupported(byId("update.check")), false);
});

test("FE-13: gap mutations are blocked in untrusted workspaces", async () => {
  for (const id of ["config.set", "sandbox.setup", "sandbox.pull", "mcp.auth.login.start"]) {
    reset();
    workspaceTrusted = false;
    const bridge = bridgeMock([id]);
    const controller = controllerFor(bridge);
    await assert.rejects(controller.executeAction(id, "x"), /requires Workspace Trust/, id);
    assert.deepEqual(bridge.calls, [], id);
  }
});

test("native queue and checkpoint workflows route bounded typed requests", async () => {
  reset();
  const bridge = bridgeMock([
    "chat.queue.list",
    "chat.queue.get",
    "chat.queue.delete",
    "checkpoint.list",
    "checkpoint.diff",
    "checkpoint.revert",
    "checkpoint.redo",
    "checkpoint.branch"
  ]);
  const controller = controllerFor(bridge);

  await controller.executeAction("chat.queue.list");
  await controller.executeAction("chat.queue.get", "prompt-1");
  await controller.executeAction("chat.queue.delete", "prompt-2");
  await controller.executeAction("checkpoint.list");
  await controller.executeAction("checkpoint.diff", "checkpoint-1");
  await controller.executeAction("checkpoint.revert", "checkpoint-1");
  await controller.executeAction("checkpoint.redo");
  await controller.executeAction("checkpoint.branch", "checkpoint-1 feature/recovery");

  assert.deepEqual(
    bridge.calls.map((call: { method: string }) => call.method),
    [
      "chat.queue.list",
      "chat.queue.get",
      "chat.queue.delete",
      "checkpoint.list",
      "checkpoint.diff",
      "checkpoint.revert",
      "checkpoint.redo",
      "checkpoint.branch"
    ]
  );
  assert.deepEqual(bridge.calls[4].params, {
    session_id: "session-1",
    checkpoint_id: "checkpoint-1",
    max_bytes: 128_000
  });
  assert.deepEqual(bridge.calls[5].params, {
    session_id: "session-1",
    checkpoint_id: "checkpoint-1",
    workspace_trusted: true,
    confirm: true
  });
  assert.deepEqual(bridge.calls[7].params, {
    session_id: "session-1",
    checkpoint_id: "checkpoint-1",
    name: "feature/recovery"
  });
});

test("safe queue cancellation and temporary-grant revocation remain available after trust loss", async () => {
  reset();
  workspaceTrusted = false;
  const bridge = bridgeMock(["chat.queue.delete", "permission.session.revoke"]);
  const controller = controllerFor(bridge);

  await controller.executeAction("chat.queue.delete", "prompt-1");
  await controller.executeAction("permission.session.revoke", "grant-1");

  assert.deepEqual(
    bridge.calls.map((call: { method: string }) => call.method),
    ["chat.queue.delete", "permission.session.revoke"]
  );
});

test("native permission workflows require scoped rules and never present command patterns", async () => {
  reset();
  const bridge = bridgeMock([
    "permission.rules.list",
    "permission.rules.grant",
    "permission.rules.revoke",
    "permission.evaluate",
    "permission.session.list",
    "permission.session.revoke"
  ]);
  bridge.permissionRulesList = async () => record(
    bridge,
    "permission.rules.list",
    {},
    [{ id: "rule-1", effect: "allow", source: "user", has_command_pattern: true }]
  );
  const controller = controllerFor(bridge);

  await assert.rejects(
    controller.executeAction("permission.rules.grant", "allow"),
    /must have an explicit scope|explicit scope/
  );
  await assert.rejects(
    controller.executeAction(
      "permission.rules.grant",
      "allow command Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
    ),
    /never accept secrets|credential/
  );
  assert.deepEqual(bridge.calls, []);

  await controller.executeAction("permission.rules.list");
  await controller.executeAction("permission.rules.grant", "ask tool shell_run");
  await controller.executeAction("permission.evaluate", "shell_run path src/index.ts");
  await controller.executeAction("permission.session.list");
  await controller.executeAction("permission.session.revoke", "grant-1");
  await controller.executeAction("permission.rules.revoke", "rule-1");

  assert.deepEqual(bridge.calls[1].params, {
    effect: "ask",
    tool_pattern: "shell_run",
    confirm: true
  });
  assert.deepEqual(bridge.calls[2].params, {
    tool_name: "shell_run",
    paths: ["src/index.ts"],
    command: undefined,
    workspace: "/workspace/project"
  });
  const output = outputLines.join("\n");
  assert.match(output, /command pattern \(hidden\)/i);
  assert.doesNotMatch(output, /Authorization|abcdefghijklmnop/);
});

test("native history and generic review workflows produce bounded redacted summaries and compact cards", async () => {
  reset();
  const canary = "sk-review-secret-abcdefghijklmnop";
  const bridge = bridgeMock(["session.search", "code.review.start", "code.review.result", "checkpoint.diff"]);
  bridge.sessionSearch = async (sessionId: string, query: string, options: Record<string, unknown>) => record(
    bridge,
    "session.search",
    { session_id: sessionId, query, ...options },
    {
      session_id: sessionId,
      workspace_root: "/workspace/project",
      query,
      results: [{ session_id: "past-1", event_type: "assistant_message", snippet: `token ${canary}` }],
      truncated: false,
      redacted: true
    }
  );
  bridge.codeReviewResult = async (jobId: string) => record(
    bridge,
    "code.review.result",
    { job_id: jobId },
    {
      session_id: "session-1",
      job_id: jobId,
      status: "completed",
      scope: "working_tree",
      summary: { verdict: "request_changes", overview: "One issue", truncated: false },
      findings: [{
        severity: "high",
        title: "Unsafe logging",
        explanation: `Leaked ${canary}`,
        evidence: `Authorization: Bearer ${canary}`,
        suggested_fix: "Remove the log line.",
        path: "src/app.ts",
        line_start: 12,
        line_end: 12,
        confidence: "high"
      }]
    }
  );
  bridge.checkpointDiff = async (sessionId: string, checkpointId: string, maxBytes: number) => record(
    bridge,
    "checkpoint.diff",
    { session_id: sessionId, checkpoint_id: checkpointId, max_bytes: maxBytes },
    { checkpoint_id: checkpointId, diff: "x".repeat(10_000), truncated: false }
  );
  const cards = new ActionResultStore(() => undefined);
  const controller = controllerFor(bridge, { sessionId: "session-1" }, async () => undefined, undefined, cards);

  await controller.executeAction("session.search", "logging");
  await controller.executeAction("code.review.start", "range main HEAD");
  await controller.executeAction("code.review.result", "review-1");
  await controller.executeAction("checkpoint.diff", "checkpoint-1");

  assert.deepEqual(bridge.calls[1].params, {
    session_id: "session-1",
    workspace_trusted: true,
    scope: "range",
    base: "main",
    head: "HEAD"
  });
  const output = outputLines.join("\n");
  assert.match(output, /Structured code review started/);
  assert.match(output, /\[HIGH\] Unsafe logging/);
  assert.match(output, /src\/app\.ts:12/);
  assert.doesNotMatch(output, /sk-review-secret|abcdefghijklmnop/);
  const checkpointCard = cards.list().at(-1);
  assert.equal(checkpointCard?.actionId, "checkpoint.diff");
  assert.doesNotMatch(JSON.stringify(checkpointCard?.payload), /x{100}/);
  assert.match(JSON.stringify(checkpointCard?.payload), /available in Alysis Code Output/);
});

test("cold past-task search creates a live session and attaches only the selected typed context block", async () => {
  reset();
  const bridge = bridgeMock(["session.search"]);
  bridge.sessionSearch = async (sessionId: string, query: string, options: Record<string, unknown>) => record(
    bridge,
    "session.search",
    { session_id: sessionId, query, ...options },
    {
      results: [{
        session_id: "past-session",
        event_type: "assistant_message",
        snippet: "fixed the reconnect race",
        context_block: {
          type: "past_session",
          content: "fixed the reconnect race",
          session_id: "past-session",
          turn_id: "result-1"
        }
      }],
      truncated: false,
      redacted: true
    }
  );
  let liveSessionCalls = 0;
  const attached: Record<string, unknown>[] = [];
  const controller = controllerFor(
    bridge,
    {},
    undefined,
    async () => {
      liveSessionCalls += 1;
      return "cold-session";
    },
    undefined,
    undefined,
    async (block) => {
      attached.push(block);
    }
  );

  const run = await controller.executeAction("session.search", "reconnect");

  assert.equal(liveSessionCalls, 1);
  assert.equal(attached.length, 1);
  assert.equal(attached[0].type, "past_session");
  assert.equal((run.result as any).attached_context, true);
  assert.equal("context_block" in (run.result as any).results[0], false);
  assert.equal((bridge.calls[0].params as any).session_id, "cold-session");
});

test("code review actions route start and manual result through the native Problems presenter", async () => {
  reset();
  const bridge = bridgeMock(["code.review.start", "code.review.result"]);
  const presenterCalls: Array<{ kind: string; value: unknown }> = [];
  const presenter = {
    run: async (params: unknown, workspaceRoot?: string) => {
      presenterCalls.push({ kind: "run", value: { params, workspaceRoot } });
      return {
        session_id: "session-1",
        job_id: "review-native",
        status: "completed",
        scope: "working_tree",
        complete: true,
        findings: []
      };
    },
    present: async (result: unknown, expectedSessionId: string, workspaceRoot?: string) => {
      presenterCalls.push({ kind: "present", value: { result, expectedSessionId, workspaceRoot } });
      return result;
    }
  };
  const controller = controllerFor(
    bridge,
    { sessionId: "session-1" },
    async () => undefined,
    undefined,
    undefined,
    presenter
  );

  const started = await controller.executeAction("code.review.start", "working_tree");
  await controller.executeAction("code.review.result", "review-manual");

  assert.equal(presenterCalls[0]?.kind, "run");
  assert.deepEqual(presenterCalls[0]?.value, {
    params: {
      session_id: "session-1",
      workspace_trusted: true,
      scope: "working_tree"
    },
    workspaceRoot: "/workspace/project"
  });
  assert.equal(presenterCalls[1]?.kind, "present");
  assert.equal((presenterCalls[1]?.value as any).expectedSessionId, "session-1");
  assert.equal((presenterCalls[1]?.value as any).workspaceRoot, "/workspace/project");
  assert.equal(bridge.calls.filter((call: { method: string }) => call.method === "code.review.start").length, 0);
  assert.equal(bridge.calls.filter((call: { method: string }) => call.method === "code.review.result").length, 1);
  assert.equal((started.result as any).complete, true);
  assert.match(outputLines.join("\n"), /Status: completed/);
});

test("registered Review Git Changes reaches the existing action and presenter exactly once", async () => {
  reset();
  const bridge = bridgeMock(["code.review.start", "code.review.result"]);
  let presented = 0;
  const controller = controllerFor(bridge, { sessionId: "session-1" }, async () => undefined,
    undefined, undefined, {
      run: async (params) => { presented += 1; assert.equal((params as any).scope, "working_tree"); return completedReview(); },
      present: async (result) => result
    });
  const actionIds: string[] = [];
  const execute = controller.executeAction.bind(controller);
  controller.executeAction = async (id, ...args) => { actionIds.push(id); return execute(id, ...args); };
  registerReviewCommands({ subscriptions: [] } as any, controller);
  await registeredCommands.get("alysis.reviewGitChanges")!();
  assert.deepEqual(actionIds, ["code.review.start"]);
  assert.equal(presented, 1);
  assert.deepEqual(nativeCommands, []);
  assert.deepEqual(bridge.calls, [], "the presenter owns the bridge review request");
  assert.match(infos[0], /completed with 0 findings/);
});

for (const [label, inputs, expected] of [
  ["Working tree", [], { scope: "working_tree" }],
  ["Branch", ["main"], { scope: "branch", base: "main", head: "HEAD" }],
  ["Commit", ["HEAD~1"], { scope: "commit", revision: "HEAD~1" }],
  ["Revision range", ["main", "topic"], { scope: "range", base: "main", head: "topic" }]
] as const) {
  test(`review scope picker preserves ${label} arguments`, async () => {
    reset();
    quickPickLabel = label;
    inputValues = [...inputs];
    const bridge = bridgeMock(["code.review.start", "code.review.result"]);
    await controllerFor(bridge, { sessionId: "session-1" }, async () => undefined).executeAction("code.review.start");
    assert.deepEqual(quickPickItems.map((item) => item.label), ["Working tree", "Branch", "Commit", "Revision range"]);
    assert.deepEqual(bridge.calls[0].params, { session_id: "session-1", workspace_trusted: true, ...expected });
  });
}

for (const args of ["staged", "pr 123", "working_tree HEAD", "commit -bad", "commit a b", "branch main HEAD extra", "range a b extra", `commit ${"a".repeat(201)}`]) {
  test(`review rejects invalid scope or revision: ${args.slice(0, 40)}`, async () => {
    reset();
    const bridge = bridgeMock(["code.review.start", "code.review.result"]);
    await assert.rejects(controllerFor(bridge, { sessionId: "session-1" }, async () => undefined)
      .executeAction("code.review.start", args), /scope|revision/i);
    assert.deepEqual(bridge.calls, []);
  });
}

for (const phase of ["scope", "branch", "commit", "range-base", "range-head", "confirmation"]) {
  test(`cancelling review ${phase} neither starts review nor opens SCM`, async () => {
    reset();
    if (phase === "scope") quickPickHandler = async () => undefined;
    if (phase === "branch") quickPickLabel = "Branch";
    if (phase === "commit") quickPickLabel = "Commit";
    if (phase.startsWith("range")) quickPickLabel = "Revision range";
    if (phase === "range-head") inputValues = ["main"];
    if (phase === "confirmation") warningHandler = () => undefined;
    const bridge = bridgeMock(["code.review.start", "code.review.result"]);
    const cards = new ActionResultStore(() => undefined);
    const controller = controllerFor(bridge, { sessionId: "session-1" }, async () => undefined, undefined, cards);
    const result = await controller.executeAction("code.review.start");
    assert.equal((result.result as any).cancelled, true);
    assert.deepEqual(bridge.calls, []);
    assert.deepEqual(cards.list(), []);
    assert.deepEqual(nativeCommands, []);
  });
}

for (const prerequisite of ["session", "trust", "workspace", "capability", "result-capability", "credentials"]) {
  test(`review reports missing ${prerequisite} without starting a job or creating a session`, async () => {
    reset();
    let initialized = 0;
    let created = 0;
    const methods = prerequisite === "capability" ? [] : prerequisite === "result-capability" ? ["code.review.start"] : ["code.review.start", "code.review.result"];
    const bridge = bridgeMock(methods);
    const context = { sessionId: prerequisite === "session" ? undefined : "session-1", workspaceRoot: prerequisite === "workspace" ? undefined : "/workspace/project" };
    workspaceTrusted = prerequisite !== "trust";
    const controller = controllerFor(bridge, context,
      prerequisite === "credentials" ? undefined : async () => { initialized += 1; },
      async () => { created += 1; return "new-session"; });
    registerReviewCommands({ subscriptions: [] } as any, controller);
    await registeredCommands.get("alysis.reviewGitChanges")!();
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], prerequisite === "session" || prerequisite === "workspace" ? /New Session.*Task History/ : prerequisite.includes("capability") ? /Update.*CLI.*reconnect/ : prerequisite === "trust" ? /Trust this workspace/ : /credential-capable/);
    assert.equal(created, 0);
    if (["session", "trust", "workspace"].includes(prerequisite)) assert.equal(initialized, 0);
    assert.deepEqual(bridge.calls, []);
    assert.deepEqual(nativeCommands, []);
  });
}

for (const phase of ["credentials", "scope", "revision", "confirmation"]) {
  for (const change of ["session", "workspace", "removed-root", "trust"]) {
    test(`review aborts when ${change} changes during ${phase}`, async () => {
      reset();
      const context = { sessionId: "session-1", workspaceRoot: "/workspace/project" };
      const mutate = () => {
        if (change === "session") context.sessionId = "session-2";
        if (change === "workspace") context.workspaceRoot = "/workspace/other";
        if (change === "removed-root") workspaceFolders = [];
        if (change === "trust") workspaceTrusted = false;
      };
      quickPickHandler = async (items) => { if (phase === "scope") mutate(); return items[phase === "revision" ? 2 : 0]; };
      inputHandler = async () => { mutate(); return "HEAD"; };
      warningHandler = () => { if (phase === "confirmation") mutate(); return "Continue"; };
      const bridge = bridgeMock(["code.review.start", "code.review.result"]);
      const cards = new ActionResultStore(() => undefined);
      const controller = controllerFor(bridge, context, async () => { if (phase === "credentials") mutate(); }, undefined, cards);
      await assert.rejects(controller.executeAction("code.review.start"), /changed|Trust this workspace/);
      assert.deepEqual(bridge.calls, []);
      assert.deepEqual(cards.list(), []);
    });
  }
}

test("review workspace ownership uses native path equality, including Windows casing", async () => {
  reset();
  const bridge = bridgeMock(["code.review.start", "code.review.result"]);
  const workspaceRoot = process.platform === "win32" ? "/WORKSPACE/project" : "/workspace/project/.";
  await controllerFor(bridge, { sessionId: "session-1", workspaceRoot }, async () => undefined)
    .executeAction("code.review.start", "working_tree");
  assert.equal(bridge.calls.length, 1);
});

test("review keeps the session root when focus changes and preserves dirty-buffer guards", async () => {
  reset();
  workspaceFolders!.push({ uri: { fsPath: "/workspace/other", scheme: "file", authority: "" } });
  const bridge = bridgeMock(["code.review.start", "code.review.result"]);
  let reviewedRoot: string | undefined;
  const controller = controllerFor(bridge, { sessionId: "session-1" }, async () => undefined, undefined, undefined, {
    run: async (_params: unknown, root?: string) => { reviewedRoot = root; return completedReview(); },
    present: async (result) => result
  });
  warningHandler = () => { activeEditorUri = { fsPath: "/workspace/other/file.ts", scheme: "file", authority: "" }; return "Continue"; };
  textDocuments = [dirtyDocument("/workspace/other/file.ts")];
  await controller.executeAction("code.review.start", "working_tree");
  assert.equal(reviewedRoot, "/workspace/project");
  reviewedRoot = undefined;
  warningHandler = () => { textDocuments = [dirtyDocument("/workspace/project/file.ts")]; return "Continue"; };
  await assert.rejects(controller.executeAction("code.review.start", "working_tree"), /unsaved/i);
  assert.equal(reviewedRoot, undefined);
  assert.equal(textDocuments[0].isDirty, true);
});

for (const outcome of ["success", "cancellation", "failure"]) {
  test(`shared review guard spans prompts through completion and releases after ${outcome}`, async () => {
    reset();
    let releaseScope!: (value: any) => void;
    let scopeOpened!: () => void;
    const opened = new Promise<void>((resolve) => { scopeOpened = resolve; });
    quickPickHandler = async () => { scopeOpened(); return new Promise((resolve) => { releaseScope = resolve; }); };
    const bridge = bridgeMock(["code.review.start", "code.review.result"]);
    const controller = controllerFor(bridge, { sessionId: "session-1" }, async () => undefined);
    let invocations = 0;
    let finish!: () => void;
    let started!: () => void;
    const running = new Promise<void>((resolve) => { started = resolve; });
    bridge.codeReviewStart = async () => {
      invocations += 1;
      started();
      await new Promise<void>((resolve) => { finish = resolve; });
      if (outcome === "failure") throw new Error("provider failed Bearer abcdefgh1234567890");
      return completedReview();
    };
    const cockpitReview = registerReviewCommands({ subscriptions: [] } as any, controller);
    const first = registeredCommands.get("alysis.reviewGitChanges")!();
    await opened;
    await cockpitReview();
    releaseScope(outcome === "cancellation" ? undefined : { value: "working_tree" });
    if (outcome !== "cancellation") {
      await running;
      await cockpitReview();
      await registeredCommands.get("alysis.reviewGitChanges")!();
      assert.equal(invocations, 1);
      finish();
    }
    await first;
    assert.doesNotMatch(warnings.join("\n"), /abcdefgh1234567890/);
    if (outcome === "failure") assert.equal(warnings.filter((message) => /provider failed/.test(message)).length, 1);
    quickPickHandler = undefined;
    bridge.codeReviewStart = async () => { invocations += 1; return completedReview(); };
    await cockpitReview();
    assert.equal(invocations, outcome === "cancellation" ? 1 : 2);
    assert.deepEqual(nativeCommands, []);
  });
}

for (const status of ["completed", "cancelled", "failed"]) {
  test(`review ${status} retains truthful notices and action-result lifecycle`, async () => {
    reset();
    const cards = new ActionResultStore(() => undefined);
    const bridge = bridgeMock(["code.review.start", "code.review.result"]);
    const controller = controllerFor(bridge, { sessionId: "session-1" }, async () => undefined, undefined, cards, {
      run: async () => {
        if (status === "failed") throw new Error("provider failed Bearer abcdefgh1234567890");
        return { ...completedReview(), status, complete: status === "completed", cancelled: status === "cancelled" };
      }, present: async (result) => result
    });
    if (status === "failed") {
      await assert.rejects(controller.executeAction("code.review.start", "working_tree"), /provider failed/);
      assert.equal(cards.list()[0].status, "error");
      assert.deepEqual(infos, []);
    } else {
      const result = await controller.executeAction("code.review.start", "working_tree");
      assert.match(result.notice, status === "completed" ? /completed with 0 findings/ : /cancelled/);
      assert.equal(infos[0], result.notice);
      assert.equal(cards.list()[0].status, "ok");
    }
    assert.doesNotMatch(JSON.stringify(cards.list()), /abcdefgh1234567890/);
    if (status !== "completed") assert.doesNotMatch(infos.join("\n") + JSON.stringify(cards.list()), /0 findings|no issues|completed with/);
  });
}

test("Open Source Control only invokes native SCM even without trust or a session", async () => {
  reset();
  workspaceTrusted = false;
  workspaceFolders = undefined;
  let backendCalls = 0;
  registerReviewCommands({ subscriptions: [] } as any, {
    executeAction: async () => { backendCalls += 1; throw new Error("must not initialize backend"); }
  });
  await registeredCommands.get("alysis.openSourceControl")!();
  assert.deepEqual(nativeCommands, ["workbench.view.scm"]);
  assert.equal(backendCalls, 0);
  assert.deepEqual(warnings, []);
});

for (const result of [
  { status: "completed", complete: true },
  { status: "failed", findings: [] },
  { status: "unavailable", findings: [] },
  { status: "cancelled", findings: [] }
]) {
  test(`incomplete review results (${result.status}) never become zero findings`, async () => {
    reset();
    const bridge = bridgeMock(["code.review.result"]);
    bridge.codeReviewResult = async () => result;
    const cards = new ActionResultStore(() => undefined);
    const outcome = await controllerFor(bridge, { sessionId: "session-1" }, undefined, undefined, cards)
      .executeAction("code.review.result", "review-1");
    assert.match(outcome.notice, /unavailable|cancelled/);
    assert.match(outputLines.join("\n"), /Findings are unavailable/);
    assert.doesNotMatch(JSON.stringify(cards.list()) + infos.join("\n"), /0 finding|no issues/);
    assert.equal((cards.list()[0].payload as any).published_to_problems, false);
  });
}

test("completed reviews report findings and disclose bounded results", async () => {
  reset();
  const bridge = bridgeMock(["code.review.start", "code.review.result"]);
  const controller = controllerFor(bridge, { sessionId: "session-1" }, async () => undefined, undefined, undefined, {
    run: async () => ({ ...completedReview(), findings: [{ title: "Unsafe logging", path: "src/app.ts", line_start: 12 }], summary: { truncated: true } }),
    present: async (result) => result
  });
  await controller.executeAction("code.review.start", "working_tree");
  assert.match(infos[0], /completed with 1 finding available.*Results were limited/);
  assert.match(outputLines.join("\n"), /src\/app.ts:12/);
});

function completedReview(): any {
  return { session_id: "session-1", job_id: "review-1", status: "completed", scope: "working_tree", complete: true, findings: [] };
}

test("backend action controller rejects workspace-bound actions without a workspace folder", async () => {
  reset();
  workspaceFolders = undefined;
  inputValues = ["demo.ext"];
  const bridge = bridgeMock(["ext.install"]);
  const controller = controllerFor(bridge);

  await assert.rejects(
    controller.executeAction("ext.install"),
    /Open a workspace folder before using Install Extension/
  );
  assert.deepEqual(bridge.calls, []);
});

test("backend action controller hides workspace-bound grouped actions without a workspace folder", async () => {
  reset();
  workspaceFolders = undefined;
  const bridge = bridgeMock(["ext.list"]);
  const controller = controllerFor(bridge);

  const result = await controller.runGroup("extensions");

  assert.equal(result, undefined);
  assert.deepEqual(bridge.calls, []);
  assert.match(warnings[0], /not available with the connected Alysis Code CLI/);
});

test("backend action controller trust-gates every workspace-mutating action route", async () => {
  reset();
  workspaceTrusted = false;
  const methods = Array.from(new Set(BACKEND_ACTIONS.flatMap((action) => action.requiredMethods)));
  const bridge = bridgeMock(methods);
  const controller = controllerFor(bridge, {
    sessionId: "session-1"
  });

  for (const action of BACKEND_ACTIONS.filter((item) => item.workspaceTrustRequired)) {
    await assert.rejects(
      controller.executeAction(action.id, defaultArgsForAction(action.id)),
      /requires Workspace Trust/,
      action.id
    );
  }
  assert.deepEqual(bridge.calls, []);
});

test("backend action controller params satisfy the IDE method contract for every action", async () => {
  reset();
  const contract = loadProtocolContract();
  const methods = Array.from(new Set(BACKEND_ACTIONS.flatMap((action) => action.requiredMethods)));
  const bridge = contractBridgeMock(methods);
  const controller = controllerFor(
    bridge,
    { sessionId: "session-1" },
    async () => undefined
  );
  const tested = new Set<string>();

  for (const action of BACKEND_ACTIONS) {
    inputValues = contractInputsForAction(action.id);
    await controller.executeAction(action.id, contractArgsForAction(action.id));
    const call = bridge.calls.at(-1);
    assert.ok(call, action.id);
    assert.equal(call.method, action.requiredMethods[0], action.id);
    validateProtocolParams(call.method, call.params, contract[call.method], action.id);
    tested.add(action.id);
  }

  assert.deepEqual(tested, new Set(BACKEND_ACTIONS.map((action) => action.id)));
});

test("backend action controller treats cancelled parameter prompts as cancelled actions", async () => {
  reset();
  const bridge = bridgeMock(["profile.show"]);
  const controller = controllerFor(bridge);

  const result = await controller.executeAction("profile.show");

  assert.equal(result.notice, "Show Profile cancelled.");
  assert.deepEqual(bridge.calls, []);
});

test("backend action controller collects required profile add base URL", async () => {
  reset();
  inputValues = ["demo", "https://api.example.test/v1", "gpt-5"];
  const bridge = bridgeMock(["profile.add"]);
  const controller = controllerFor(bridge);

  await controller.executeAction("profile.add");

  assert.deepEqual(bridge.calls[0], {
    method: "profile.add",
    params: {
      name: "demo",
      base_url: "https://api.example.test/v1",
      default_model: "gpt-5",
      workspace_trusted: true
    }
  });
});

test("backend action controller routes profile rename with management protocol params", async () => {
  reset();
  inputValues = ["renamed"];
  const bridge = bridgeMock(["profile.rename"]);
  const controller = controllerFor(bridge);

  await controller.executeAction("profile.rename", "demo");

  assert.deepEqual(bridge.calls[0], {
    method: "profile.rename",
    params: {
      old: "demo",
      new: "renamed",
      workspace_trusted: true
    }
  });
});

test("backend action controller routes skill validate by raw name, prompted name, or all", async () => {
  reset();
  const bridge = bridgeMock(["skill.validate"]);
  const controller = controllerFor(bridge);

  await controller.executeAction("skill.validate", "python");
  inputValues = ["typescript"];
  await controller.executeAction("skill.validate");
  inputValues = [""];
  await controller.executeAction("skill.validate");

  assert.deepEqual(bridge.calls, [
    {
      method: "skill.validate",
      params: { workspace: "/workspace/project", path: "/workspace/project", name: "python" }
    },
    {
      method: "skill.validate",
      params: { workspace: "/workspace/project", path: "/workspace/project", name: "typescript" }
    },
    {
      method: "skill.validate",
      params: { workspace: "/workspace/project", path: "/workspace/project", all: true }
    }
  ]);
});

test("backend action controller routes guarded config set with schema discovery", async () => {
  reset();
  quickPickLabel = "default_model";
  inputValues = ["gpt-5"];
  const bridge = bridgeMock(["config.set", "config.schema"]);
  const controller = controllerFor(bridge);

  await controller.executeAction("config.set");

  assert.deepEqual(bridge.calls, [
    { method: "config.schema" },
    {
      method: "config.set",
      params: {
        key: "default_model",
        value: "gpt-5",
        workspace_trusted: true
      }
    }
  ]);
  assert.match(warnings[0], /Set Config Value.*can make changes/);
});

test("backend action controller routes guarded config set from raw key and value", async () => {
  reset();
  const bridge = bridgeMock(["config.set", "config.schema"]);
  const controller = controllerFor(bridge);

  await controller.executeSlashAction("config.set", "default_model gpt-5");

  assert.deepEqual(bridge.calls, [
    {
      method: "config.set",
      params: {
        key: "default_model",
        value: "gpt-5",
        workspace_trusted: true
      }
    }
  ]);
});

test("FE-18: a completed slash action returns an empty notice (the result card is the feedback)", async () => {
  reset();
  const bridge = bridgeMock(["profile.list"]);
  const controller = controllerFor(bridge);

  const notice = await controller.executeSlashAction("profile.list");

  // No redundant "X completed." notice — the FE-11 running -> ok result card is the single feedback,
  // and full detail still goes to the Output channel.
  assert.equal(notice, "");
  assert.equal(bridge.calls.length, 1);
  assert.equal(bridge.calls[0].method, "profile.list");
});

test("FE-18: a cancelled slash action keeps its notice (no result card was shown)", async () => {
  reset();
  const bridge = bridgeMock(["profile.show"]);
  const controller = controllerFor(bridge);

  // profile.show needs a name; with no input the prompt is cancelled -> the notice is the only feedback.
  const notice = await controller.executeSlashAction("profile.show");

  assert.equal(notice, "Show Profile cancelled.");
  assert.deepEqual(bridge.calls, []);
});

test("backend action controller rejects secret-looking config set keys and values", async () => {
  reset();
  const bridge = bridgeMock(["config.set", "config.schema"]);
  const controller = controllerFor(bridge);

  await assert.rejects(
    controller.executeAction("config.set", "api_key=sk-test-secret-value-123456"),
    /Secret-bearing config keys/
  );
  await assert.rejects(
    controller.executeAction("config.set", "base_url=https://user:pass@example.test/v1"),
    /Secret-looking config values/
  );
  assert.deepEqual(bridge.calls, []);
});

test("backend action controller input prompts never request inline secrets", () => {
  const packageRoot = resolve(__dirname, "../..");
  const source = readFileSync(resolve(packageRoot, "src/backend/BackendActionController.ts"), "utf8");
  const configureProviderSource = readFileSync(resolve(packageRoot, "src/commands/configureProvider.ts"), "utf8");
  const promptTitles = [
    ...Array.from(source.matchAll(/inputOrArg\([^,\n]+,\s*"([^"]+)"/g), (match) => match[1]),
    ...Array.from(source.matchAll(/optionalInput\("([^"]+)"/g), (match) => match[1])
  ];

  assert.ok(promptTitles.length > 0);
  for (const title of promptTitles) {
    assert.doesNotMatch(title, /api[_ -]?key|apikey|bearer|password|secret|token|authorization/i, title);
  }
  assert.doesNotMatch(source, /showInputBox\([^)]*password/i);
  assert.match(configureProviderSource, /AlysisSecretStore/);
  assert.match(configureProviderSource, /setApiKey/);
});

test("backend action controller marks remote skill installs with explicit remote intent", async () => {
  reset();
  inputValues = ["https://example.test/skills.git"];
  const bridge = bridgeMock(["skill.install"]);
  const controller = controllerFor(bridge);

  await controller.executeAction("skill.install");

  assert.deepEqual(bridge.calls[0], {
    method: "skill.install",
    params: {
      workspace: "/workspace/project",
      path: "/workspace/project",
      source: "https://example.test/skills.git",
      workspace_trusted: true,
      project: true,
      allow_remote: true,
      yes: true
    }
  });
});

test("backend action controller does not send yes-only extension install trust bypass", async () => {
  reset();
  const bridge = bridgeMock(["ext.install"]);
  const controller = controllerFor(bridge);

  await controller.executeAction("ext.install", "publisher.plugin");

  assert.deepEqual(bridge.calls[0], {
    method: "ext.install",
    params: {
      workspace: "/workspace/project",
      path: "/workspace/project",
      source: "publisher.plugin",
      workspace_trusted: true,
      project: true
    }
  });
});

test("backend action controller never guesses a multi-root scope and follows the active editor", async () => {
  reset();
  workspaceFolders = [
    { uri: { fsPath: "/workspace/primary" } },
    { uri: { fsPath: "/workspace/secondary" } }
  ];
  const bridge = bridgeMock(["ext.install"]);
  const controller = controllerFor(bridge);

  await assert.rejects(
    controller.executeAction("ext.install", "publisher.plugin"),
    (error: any) => error.code === "workspace_required"
  );
  activeEditorUri = { fsPath: "/workspace/secondary/src/main.ts" };
  await controller.executeAction("ext.install", "publisher.plugin");

  assert.deepEqual(bridge.calls[0], {
    method: "ext.install",
    params: {
      workspace: "/workspace/secondary",
      path: "/workspace/secondary",
      source: "publisher.plugin",
      workspace_trusted: true,
      project: true
    }
  });
});

test("backend action controller routes live session mode changes", async () => {
  reset();
  const bridge = bridgeMock(["session.setMode"]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await controller.executeSlashAction("session.setMode", "readonly");

  assert.deepEqual(bridge.calls[0], {
    method: "session.setMode",
    params: { session_id: "session-1", mode: "readonly" }
  });
});

test("backend action controller trust-gates non-readonly live session mode changes", async () => {
  reset();
  workspaceTrusted = false;
  const bridge = bridgeMock(["session.setMode"]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await controller.executeAction("session.setMode", "readonly");
  await assert.rejects(
    controller.executeAction("session.setMode", "review"),
    /Workspace Trust is required/
  );
  inputValues = ["auto"];
  await assert.rejects(controller.executeAction("session.setMode"), /Workspace Trust is required/);

  assert.deepEqual(bridge.calls, [
    {
      method: "session.setMode",
      params: { session_id: "session-1", mode: "readonly" }
    }
  ]);
});

test("backend action controller routes normal live-session status and settings", async () => {
  reset();
  const bridge = bridgeMock([
    "session.status",
    "session.history",
    "session.context",
    "session.setModel",
    "session.setStream",
    "session.setActiveWorkdir"
  ]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await controller.executeSlashAction("session.status");
  await controller.executeSlashAction("session.history", "pytest");
  await controller.executeSlashAction("session.context");
  await controller.executeSlashAction("session.setModel", "gpt-5");
  await controller.executeSlashAction("session.setStream", "off");
  await controller.executeSlashAction("session.setActiveWorkdir", "src");

  assert.deepEqual(bridge.calls, [
    { method: "session.status", params: { session_id: "session-1" } },
    { method: "session.history", params: { session_id: "session-1", pattern: "pytest" } },
    { method: "session.context", params: { session_id: "session-1" } },
    { method: "session.setModel", params: { session_id: "session-1", model: "gpt-5" } },
    { method: "session.setStream", params: { session_id: "session-1", stream: false } },
    { method: "session.setActiveWorkdir", params: { session_id: "session-1", path: "src" } }
  ]);
});

test("backend action controller routes session image basket actions", async () => {
  reset();
  const bridge = bridgeMock(["session.images.list", "session.images.add", "session.images.clear"]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await controller.executeSlashAction("session.images.add", "screenshots/fail.png");
  await controller.executeSlashAction("session.images.add", "screenshots/paste.png");
  await controller.executeSlashAction("session.images.list");
  await controller.executeSlashAction("session.images.clear");
  openDialogValues = [[{ fsPath: "/workspace/project/screenshots/picked.png" }]];
  await controller.executeSlashAction("session.images.add");

  assert.deepEqual(bridge.calls, [
    {
      method: "session.images.add",
      params: { session_id: "session-1", images: ["screenshots/fail.png"] }
    },
    {
      method: "session.images.add",
      params: { session_id: "session-1", images: ["screenshots/paste.png"] }
    },
    { method: "session.images.list", params: "session-1" },
    { method: "session.images.clear", params: "session-1" },
    {
      method: "session.images.add",
      params: { session_id: "session-1", images: ["/workspace/project/screenshots/picked.png"] }
    }
  ]);
});

test("backend action controller sends image paths only, never image binary payloads", async () => {
  reset();
  const bridge = bridgeMock(["session.images.add"]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await controller.executeSlashAction("session.images.add", "screenshots/failure.png");

  assert.deepEqual(bridge.calls, [
    {
      method: "session.images.add",
      params: { session_id: "session-1", images: ["screenshots/failure.png"] }
    }
  ]);
  assert.equal(JSON.stringify(bridge.calls).includes("data:"), false);
  assert.equal(JSON.stringify(bridge.calls).includes("base64"), false);
  assert.equal(JSON.stringify(bridge.calls).includes("bytes"), false);
});

test("backend action controller routes model info and safe subagent slash actions", async () => {
  reset();
  const bridge = bridgeMock([
    "session.modelInfo",
    "session.subagents.status",
    "session.subagents.setEnabled"
  ]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await controller.executeSlashAction("session.modelInfo", "gpt-5");
  await controller.executeSlashAction("session.subagents.status", "status");
  await controller.executeSlashAction("session.subagents.status", "on");
  await controller.executeSlashAction("session.subagents.setEnabled", "off");

  assert.deepEqual(bridge.calls, [
    {
      method: "session.modelInfo",
      params: { session_id: "session-1", model: "gpt-5" }
    },
    { method: "session.subagents.status", params: { session_id: "session-1" } },
    {
      method: "session.subagents.setEnabled",
      params: { session_id: "session-1", enabled: true, workspace_trusted: true }
    },
    {
      method: "session.subagents.setEnabled",
      params: { session_id: "session-1", enabled: false, workspace_trusted: true }
    }
  ]);
});

test("backend action controller blocks unsupported or untrusted subagent toggles", async () => {
  reset();
  const unsupported = bridgeMock(["session.subagents.status"]);
  const unsupportedController = controllerFor(unsupported, { sessionId: "session-1" });

  await assert.rejects(
    unsupportedController.executeSlashAction("session.subagents.status", "on"),
    /does not advertise session\.subagents\.setEnabled/
  );
  assert.deepEqual(unsupported.calls, []);

  reset();
  workspaceTrusted = false;
  const untrusted = bridgeMock(["session.subagents.status", "session.subagents.setEnabled"]);
  const untrustedController = controllerFor(untrusted, { sessionId: "session-1" });

  await untrustedController.executeSlashAction("session.subagents.status", "status");
  await assert.rejects(
    untrustedController.executeSlashAction("session.subagents.status", "off"),
    /Workspace Trust/
  );
  assert.deepEqual(untrusted.calls, [
    { method: "session.subagents.status", params: { session_id: "session-1" } }
  ]);
});

test("backend action controller routes trace slash actions with full confirmation", async () => {
  reset();
  const bridge = bridgeMock([
    "session.trace.status",
    "session.trace.setLevel",
    "session.trace.listEvents",
    "session.trace.clear"
  ]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await controller.executeSlashAction("session.trace.status", "");
  await controller.executeSlashAction("session.trace.status", "events");
  await controller.executeSlashAction("session.trace.status", "compact");
  await controller.executeSlashAction("session.trace.status", "full");
  await controller.executeSlashAction("session.trace.status", "clear");

  assert.deepEqual(bridge.calls, [
    { method: "session.trace.status", params: "session-1" },
    { method: "session.trace.listEvents", params: "session-1" },
    { method: "session.trace.setLevel", params: { session_id: "session-1", level: "compact", confirm: false, yes: false } },
    { method: "session.trace.setLevel", params: { session_id: "session-1", level: "full", confirm: true, yes: false } },
    { method: "session.trace.clear", params: "session-1" }
  ]);
  assert.equal(warnings.some((message) => message.includes("Full trace")), true);
});

test("backend action controller rejects invalid trace commands before bridge calls", async () => {
  reset();
  const bridge = bridgeMock(["session.trace.status", "session.trace.setLevel"]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await assert.rejects(
    controller.executeSlashAction("session.trace.status", "verbose"),
    /Usage: \/trace/
  );
  assert.deepEqual(bridge.calls, []);
});

test("backend action controller routes terminal list show and kill without shell execution", async () => {
  reset();
  const bridge = bridgeMock([
    "session.terminals.list",
    "session.terminals.show",
    "session.terminals.kill",
    "session.terminals.clear"
  ]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await controller.executeSlashAction("session.terminals.list", "");
  await controller.executeSlashAction("session.terminals.list", "list");
  await controller.executeSlashAction("session.terminals.list", "show proc-1");
  await controller.executeSlashAction("session.terminals.list", "kill proc-1");
  await controller.executeSlashAction("session.terminals.list", "clear proc-1");

  assert.deepEqual(bridge.calls, [
    { method: "session.terminals.list", params: "session-1" },
    { method: "session.terminals.list", params: "session-1" },
    { method: "session.terminals.show", params: { session_id: "session-1", process_id: "proc-1" } },
    {
      method: "session.terminals.kill",
      params: {
        session_id: "session-1",
        process_id: "proc-1",
        workspace_trusted: true,
        confirm: true,
        yes: false
      }
    },
    {
      method: "session.terminals.clear",
      params: {
        session_id: "session-1",
        process_id: "proc-1",
        workspace_trusted: true,
        confirm: true,
        yes: false
      }
    }
  ]);
  assert.equal(warnings.some((message) => message.includes("Kill Terminal")), true);
  assert.equal(warnings.some((message) => message.includes("Clear Terminal Output")), true);
});

test("backend action controller terminal mutations require Workspace Trust", async () => {
  reset();
  workspaceTrusted = false;
  const bridge = bridgeMock(["session.terminals.list", "session.terminals.kill"]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await controller.executeSlashAction("session.terminals.list", "show proc-1");
  await assert.rejects(
    controller.executeSlashAction("session.terminals.list", "kill proc-1"),
    /Workspace Trust/
  );
  assert.deepEqual(bridge.calls, [
    { method: "session.terminals.show", params: { session_id: "session-1", process_id: "proc-1" } }
  ]);
});

test("backend action controller rejects unsupported terminal shell commands", async () => {
  reset();
  const bridge = bridgeMock(["session.terminals.list"]);
  const controller = controllerFor(bridge, { sessionId: "session-1" });

  await assert.rejects(
    controller.executeSlashAction("session.terminals.list", "start npm test"),
    /arbitrary shell execution/
  );
  assert.deepEqual(bridge.calls, []);
});

test("backend action controller routes selected session read actions with selected ids", async () => {
  reset();
  const bridge = bridgeMock(["session.show", "session.score", "session.usage"]);
  const controller = controllerFor(bridge, {});

  await controller.executeAction("session.show", "retained-1");
  await controller.executeAction("session.score", "retained-1");
  await controller.executeAction("session.usage", "retained-1");

  assert.deepEqual(bridge.calls, [
    { method: "session.show", params: { session_id: "retained-1" } },
    { method: "session.score", params: { session_id: "retained-1" } },
    { method: "session.usage", params: { session_id: "retained-1" } }
  ]);
});

test("backend action controller scores latest retained session when no selected id is provided", async () => {
  reset();
  const bridge = bridgeMock(["session.score"]);
  const controller = controllerFor(bridge, {});

  await controller.executeAction("session.score");

  assert.deepEqual(bridge.calls[0], {
    method: "session.score",
    params: { latest: 1 }
  });
});

test("backend action controller resumes selected retained session into existing active session", async () => {
  reset();
  const bridge = bridgeMock(["session.resume"]);
  const controller = controllerFor(bridge, { sessionId: "live-1" });

  await controller.executeAction("session.resume", "retained-1");

  assert.deepEqual(bridge.calls[0], {
    method: "session.resume",
    params: { session_id: "live-1", target_session_id: "retained-1" }
  });
});

test("backend action controller creates a live session before retained session resume when needed", async () => {
  reset();
  const bridge = bridgeMock(["session.resume"]);
  const context: { sessionId?: string } = {};
  let liveSessionStarts = 0;
  const controller = controllerFor(
    bridge,
    context,
    undefined,
    async () => {
      liveSessionStarts += 1;
      context.sessionId = "live-created";
      return "live-created";
    }
  );

  await controller.executeAction("session.resume", "retained-1");

  assert.equal(liveSessionStarts, 1);
  assert.deepEqual(bridge.calls[0], {
    method: "session.resume",
    params: { session_id: "live-created", target_session_id: "retained-1" }
  });
});

test("backend action controller reports a clear resume error when no live-session starter is wired", async () => {
  reset();
  const bridge = bridgeMock(["session.resume"]);
  const controller = controllerFor(bridge, {});

  await assert.rejects(
    controller.executeAction("session.resume", "retained-1"),
    /Open Alysis Code or start a session first/
  );
  assert.deepEqual(bridge.calls, []);
});

test("backend action controller routes health diagnostics and cached update checks", async () => {
  reset();
  const bridge = bridgeMock([
    "doctor.summary",
    "doctor.providers",
    "doctor.bundle",
    "sandbox.doctor",
    "update.check"
  ]);
  const controller = controllerFor(bridge);

  await controller.executeAction("doctor.summary");
  await controller.executeAction("doctor.providers");
  await controller.executeAction("doctor.bundle");
  await controller.executeAction("sandbox.doctor");
  await controller.executeAction("update.check");

  assert.deepEqual(bridge.calls, [
    { method: "doctor.summary" },
    { method: "doctor.providers" },
    { method: "doctor.bundle" },
    {
      method: "sandbox.doctor",
      params: { smoke: true, include_server: false }
    },
    {
      method: "update.check",
      params: { cached: true }
    }
  ]);
});

test("backend action controller release dogfood redacts health diagnostics output", async () => {
  reset();
  const bridge = bridgeMock(["doctor.bundle"]);
  bridge.doctorBundle = async () =>
    record(bridge, "doctor.bundle", undefined, {
      bundle: {
        status: "ok",
        stderr: "Authorization: Bearer abcdefghijklmnop",
        api_key: "sk-test-secret-value-123456"
      }
    });
  const controller = controllerFor(bridge);

  await controller.runGroup("healthDiagnostics");

  const output = outputLines.join("\n");
  assert.match(output, /Doctor Bundle/);
  assert.match(output, /<redacted>/);
  assert.doesNotMatch(output, /abcdefghijklmnop|sk-test-secret-value/);
});

test("backend action controller routes selected convention details with focus_path", async () => {
  reset();
  const bridge = bridgeMock(["conventions.render"]);
  const controller = controllerFor(bridge);

  await controller.executeAction("conventions.render", "docs/conventions.md");

  assert.deepEqual(bridge.calls[0], {
    method: "conventions.render",
    params: {
      workspace: "/workspace/project",
      path: "/workspace/project",
      focus_path: "docs/conventions.md"
    }
  });
});

test("backend action controller performs online update checks only after explicit selection", async () => {
  reset();
  quickPickLabel = "Check online now";
  const bridge = bridgeMock(["update.check"]);
  const controller = controllerFor(bridge);

  await controller.executeAction("update.check");

  assert.deepEqual(bridge.calls[0], {
    method: "update.check",
    params: { cached: false, allow_network: true }
  });
});

test("backend action controller trust-gates sandbox setup and pull", async () => {
  reset();
  workspaceTrusted = false;
  const bridge = bridgeMock(["sandbox.setup", "sandbox.pull"]);
  const controller = controllerFor(bridge);

  await assert.rejects(controller.executeAction("sandbox.setup"), /requires Workspace Trust/);
  await assert.rejects(controller.executeAction("sandbox.pull"), /requires Workspace Trust/);
  assert.deepEqual(bridge.calls, []);

  reset();
  const trustedBridge = bridgeMock(["sandbox.setup", "sandbox.pull"]);
  const trustedController = controllerFor(trustedBridge);
  await trustedController.executeAction("sandbox.setup");
  await trustedController.executeAction("sandbox.pull", "img-a,img-b");

  assert.deepEqual(trustedBridge.calls, [
    {
      method: "sandbox.setup",
      params: { workspace_trusted: true, pull: false }
    },
    {
      method: "sandbox.pull",
      params: { workspace_trusted: true, images: ["img-a", "img-b"], include_server: false }
    }
  ]);
  assert.equal(warnings.filter((message) => /can make changes/.test(message)).length, 2);
});

test("backend action controller redacts secret-looking output", async () => {
  reset();
  const bridge = bridgeMock(["config.get"]);
  bridge.configGet = async () => ({
    config: {
      message: "Authorization: Bearer abcdefghijklmnop",
      token: "sk-test-secret-value-123456",
      password: "bare-value-without-a-provider-prefix",
      nested: { client_secret: "another-bare-credential" }
    }
  });
  const controller = controllerFor(bridge);

  await controller.executeAction("config.get");

  const output = outputLines.join("\n");
  assert.doesNotMatch(output, /abcdefghijklmnop/);
  assert.doesNotMatch(output, /sk-test-secret-value/);
  assert.doesNotMatch(output, /bare-value-without-a-provider-prefix/);
  assert.doesNotMatch(output, /another-bare-credential/);
  assert.match(output, /<redacted>/);
});

test("grouped command registration routes through action controller", async () => {
  reset();
  const bridge = bridgeMock(["profile.list"]);
  const controller = controllerFor(bridge);
  controller.register({ subscriptions: [] } as any);
  quickPickLabel = "List Profiles";

  await registeredCommands.get("alysis.manageProfiles")?.();

  assert.deepEqual(bridge.calls.map((call: { method: string }) => call.method), ["profile.list"]);
});

test("backend action controller source does not scrape terminal output", () => {
  const packageRoot = resolve(__dirname, "../..");
  const source = readFileSync(resolve(packageRoot, "src/backend/BackendActionController.ts"), "utf8");
  assert.doesNotMatch(source, /child_process|createTerminal|sendText|stdout\.on|stderr\.on/);
});

function reset(): void {
  registeredCommands.clear();
  nativeCommands.length = 0;
  quickPickHandler = undefined;
  inputHandler = undefined;
  infos.length = 0;
  warnings.length = 0;
  outputLines.length = 0;
  quickPickItems = [];
  quickPickLabel = undefined;
  inputValues = [];
  openDialogValues = [];
  openedExternalUris = [];
  workspaceTrusted = true;
  workspaceFolders = [{ uri: { fsPath: "/workspace/project", scheme: "file", authority: "" } }];
  textDocuments = [];
  activeEditorUri = undefined;
  remoteName = undefined;
  warningHandler = undefined;
}

function dirtyDocument(fsPath: string): any {
  return {
    isDirty: true,
    uri: { fsPath, scheme: "file", authority: "", toString: () => `file://${fsPath}` }
  };
}

function controllerFor(
  bridge: ReturnType<typeof bridgeMock>,
  context: { sessionId?: string; workspaceRoot?: string } = { sessionId: "session-1" },
  ensureCredentialBridge?: () => Promise<void>,
  ensureLiveSession?: () => Promise<string>,
  actionResults?: ActionResultStore,
  codeReviewPresenter?: {
    run(params: unknown): Promise<any>;
    present(result: unknown, expectedSessionId: string): Promise<any>;
  },
  attachContextBlock?: (block: Record<string, unknown>) => Promise<void> | void
): InstanceType<typeof BackendActionController> {
  return new BackendActionController({
    bridge: bridge as any,
    getConfig: () => ({
      cliPath: "alysis",
      defaultMode: "review",
      defaultModel: "",
      baseUrl: "",
      provider: "",
      transport: "stdio",
      sandboxProfile: "default",
      showStatusBar: true,
      autoStartBridge: false,
      security: { cliPath: { executionAllowed: true, apiKeyForwardingAllowed: false } }
    } as any),
    output: {
      appendLine: (line: string) => outputLines.push(line),
      show: () => undefined
    } as any,
    isWorkspaceTrusted: () => workspaceTrusted,
    activeContext: () => ({ workspaceRoot: "/workspace/project", ...context }),
    ensureLiveSession,
    ensureCredentialBridge,
    actionResults,
    codeReviewPresenter: codeReviewPresenter as any,
    attachContextBlock,
    remoteName: () => remoteName
  });
}

function bridgeMock(methods: string[], featureValues: Record<string, unknown> = {}): any {
  const supported = new Set(methods);
  const bridge = {
    calls: [] as Array<{ method: string; params?: unknown }>,
    ensureStarted: async () => undefined,
    supportsMethod: (method: string) => supported.has(method),
    supportsFeature: (path: readonly string[]) => featureValues[path.join(".")] === true,
    featureValue: (path: readonly string[]) => featureValues[path.join(".")],
    configGet: async () => record(bridge, "config.get", undefined, { config: { ok: true } }),
    configSchema: async () => record(bridge, "config.schema", undefined, { schema: { properties: { default_model: {}, api_key: {} } } }),
    configSet: async (params: unknown) => record(bridge, "config.set", params, { changed: true }),
    sessionStatus: async (sessionId: string) =>
      record(bridge, "session.status", { session_id: sessionId }, { session_id: sessionId, active_workdir: "/workspace/project" }),
    sessionUsage: async (sessionId: string) => record(bridge, "session.usage", { session_id: sessionId }, { call_count: 0 }),
    sessionHistory: async (sessionId: string, pattern: string) =>
      record(bridge, "session.history", { session_id: sessionId, pattern }, { session_id: sessionId, matches: [] }),
    sessionContext: async (sessionId: string) =>
      record(bridge, "session.context", { session_id: sessionId }, { session_id: sessionId, source: "unavailable" }),
    sessionModelInfo: async (sessionId: string, options?: { model?: string }) =>
      record(
        bridge,
        "session.modelInfo",
        { session_id: sessionId, model: options?.model },
        { session_id: sessionId, model: options?.model ?? "test-model", secret_values_included: false }
      ),
    sessionSubagentsStatus: async (sessionId: string) =>
      record(bridge, "session.subagents.status", { session_id: sessionId }, { session_id: sessionId, enabled: false }),
    sessionSubagentsSetEnabled: async (sessionId: string, enabled: boolean, workspaceTrusted: boolean) =>
      record(
        bridge,
        "session.subagents.setEnabled",
        { session_id: sessionId, enabled, workspace_trusted: workspaceTrusted },
        { session_id: sessionId, enabled, changed: true }
      ),
    sessionTraceStatus: async (sessionId: string) =>
      record(bridge, "session.trace.status", sessionId, { session_id: sessionId, level: "compact" }),
    sessionTraceSetLevel: async (sessionId: string, level: string, options?: { confirm?: boolean; yes?: boolean }) =>
      record(
        bridge,
        "session.trace.setLevel",
        { session_id: sessionId, level, confirm: options?.confirm === true, yes: options?.yes === true },
        { session_id: sessionId, level, changed: true }
      ),
    sessionTraceListEvents: async (sessionId: string) =>
      record(bridge, "session.trace.listEvents", sessionId, { session_id: sessionId, events: [] }),
    sessionTraceReadArtifact: async (sessionId: string, artifactId: string) =>
      record(bridge, "session.trace.readArtifact", { session_id: sessionId, artifact_id: artifactId }, { session_id: sessionId, content: "" }),
    sessionTraceClear: async (sessionId: string) =>
      record(bridge, "session.trace.clear", sessionId, { session_id: sessionId, cleared: true }),
    sessionTerminalsList: async (sessionId: string) =>
      record(bridge, "session.terminals.list", sessionId, { session_id: sessionId, terminals: [] }),
    sessionTerminalsShow: async (sessionId: string, processId: string) =>
      record(bridge, "session.terminals.show", { session_id: sessionId, process_id: processId }, { session_id: sessionId, process_id: processId, lines: [] }),
    sessionTerminalsKill: async (sessionId: string, processId: string, workspaceTrusted: boolean, options?: { confirm?: boolean; yes?: boolean }) =>
      record(
        bridge,
        "session.terminals.kill",
        {
          session_id: sessionId,
          process_id: processId,
          workspace_trusted: workspaceTrusted,
          confirm: options?.confirm === true,
          yes: options?.yes === true
        },
        { session_id: sessionId, process_id: processId, killed: true }
      ),
    sessionTerminalsClear: async (sessionId: string, processId: string, workspaceTrusted: boolean, options?: { confirm?: boolean; yes?: boolean }) =>
      record(
        bridge,
        "session.terminals.clear",
        {
          session_id: sessionId,
          process_id: processId,
          workspace_trusted: workspaceTrusted,
          confirm: options?.confirm === true,
          yes: options?.yes === true
        },
        { session_id: sessionId, process_id: processId, cleared: true }
      ),
    sessionCompact: async (sessionId: string, focus?: string) =>
      record(bridge, "session.compact", focus ? { session_id: sessionId, focus } : { session_id: sessionId }, { changed: false }),
    sessionResume: async (sessionId: string, targetSessionId: string) =>
      record(bridge, "session.resume", { session_id: sessionId, target_session_id: targetSessionId }, { resumed: true }),
    sessionSetMode: async (sessionId: string, mode: string) =>
      record(bridge, "session.setMode", { session_id: sessionId, mode }, { session_id: sessionId, mode }),
    sessionSetModel: async (sessionId: string, model: string) =>
      record(bridge, "session.setModel", { session_id: sessionId, model }, { session_id: sessionId, model }),
    sessionSetStream: async (sessionId: string, stream: boolean) =>
      record(bridge, "session.setStream", { session_id: sessionId, stream }, { session_id: sessionId, stream }),
    sessionSetActiveWorkdir: async (sessionId: string, path: string) =>
      record(bridge, "session.setActiveWorkdir", { session_id: sessionId, path }, { session_id: sessionId, active_workdir: path }),
    sessionImagesList: async (sessionId: string) => record(bridge, "session.images.list", sessionId, { images: [] }),
    sessionImagesAdd: async (params: unknown) => record(bridge, "session.images.add", params, { images: [] }),
    sessionImagesClear: async (sessionId: string) => record(bridge, "session.images.clear", sessionId, { images: [] }),
    sessionClear: async (sessionId: string) => record(bridge, "session.clear", { session_id: sessionId }, { cleared: true }),
    sessionShow: async (params: unknown) => record(bridge, "session.show", params, { session_id: "retained-1" }),
    sessionScore: async (params: unknown) => record(bridge, "session.score", params, { scores: [] }),
    sessionSearch: async (sessionId: string, query: string, options?: Record<string, unknown>) =>
      record(
        bridge,
        "session.search",
        { session_id: sessionId, query, ...options },
        { session_id: sessionId, workspace_root: "/workspace/project", query, results: [], redacted: true }
      ),
    chatQueueList: async (sessionId: string, options?: Record<string, unknown>) =>
      record(bridge, "chat.queue.list", { session_id: sessionId, ...options }, { session_id: sessionId, items: [], next_sequence: 0 }),
    chatQueueGet: async (sessionId: string, promptId: string) =>
      record(bridge, "chat.queue.get", { session_id: sessionId, prompt_id: promptId }, { session_id: sessionId, prompt_id: promptId, state: "pending" }),
    chatQueueDelete: async (sessionId: string, promptId: string) =>
      record(bridge, "chat.queue.delete", { session_id: sessionId, prompt_id: promptId }, { session_id: sessionId, prompt_id: promptId, state: "cancelled" }),
    checkpointList: async (sessionId: string, limit?: number) =>
      record(bridge, "checkpoint.list", { session_id: sessionId, limit }, []),
    checkpointDiff: async (sessionId: string, checkpointId: string, maxBytes?: number) =>
      record(bridge, "checkpoint.diff", { session_id: sessionId, checkpoint_id: checkpointId, max_bytes: maxBytes }, { checkpoint_id: checkpointId, diff: "" }),
    checkpointRevert: async (sessionId: string, checkpointId: string) =>
      record(
        bridge,
        "checkpoint.revert",
        { session_id: sessionId, checkpoint_id: checkpointId, workspace_trusted: true, confirm: true },
        { session_id: sessionId, checkpoint_id: checkpointId, changes: [] }
      ),
    checkpointRedo: async (sessionId: string) =>
      record(
        bridge,
        "checkpoint.redo",
        { session_id: sessionId, workspace_trusted: true, confirm: true },
        { session_id: sessionId, checkpoint_id: "redo-1", changes: [] }
      ),
    checkpointBranch: async (sessionId: string, checkpointId: string, name: string) =>
      record(bridge, "checkpoint.branch", { session_id: sessionId, checkpoint_id: checkpointId, name }, { checkpoint_id: checkpointId, ref: `refs/heads/${name}` }),
    codeReviewStart: async (params: unknown) =>
      record(bridge, "code.review.start", params, { session_id: "session-1", job_id: "review-1", status: "started", scope: "working_tree" }),
    codeReviewResult: async (jobId: string) =>
      record(bridge, "code.review.result", { job_id: jobId }, { session_id: "session-1", job_id: jobId, status: "completed", findings: [] }),
    permissionRulesList: async () => record(bridge, "permission.rules.list", {}, []),
    permissionRuleGrant: async (params: Record<string, unknown>) =>
      record(bridge, "permission.rules.grant", { ...params, confirm: true }, { id: "rule-1", ...params }),
    permissionRuleRevoke: async (ruleId: string) =>
      record(bridge, "permission.rules.revoke", { rule_id: ruleId, confirm: true }, true),
    permissionEvaluate: async (params: unknown) =>
      record(bridge, "permission.evaluate", params, { decision: "ask", reason: "default_policy", specificity: 0 }),
    permissionSessionList: async (sessionId: string) =>
      record(bridge, "permission.session.list", { session_id: sessionId }, []),
    permissionSessionRevoke: async (sessionId: string, grantId: string) =>
      record(bridge, "permission.session.revoke", { session_id: sessionId, grant_id: grantId }, true),
    profileList: async () => record(bridge, "profile.list", undefined, { profiles: [] }),
    profileShow: async (params: unknown) => record(bridge, "profile.show", params, { profile: {} }),
    profileAdd: async (params: unknown) => record(bridge, "profile.add", params, { changed: true }),
    profileUse: async (params: unknown) => record(bridge, "profile.use", params, { changed: true }),
    profileRename: async (params: unknown) => record(bridge, "profile.rename", params, { changed: true }),
    skillValidate: async (params: unknown) => record(bridge, "skill.validate", params, { valid: true }),
    skillInstall: async (params: unknown) => record(bridge, "skill.install", params, { changed: true }),
    extInstall: async (params: unknown) => record(bridge, "ext.install", params, { changed: true }),
    doctorSummary: async () => record(bridge, "doctor.summary", undefined, { ok: true }),
    doctorProviders: async () => record(bridge, "doctor.providers", undefined, { providers: [] }),
    doctorBundle: async () => record(bridge, "doctor.bundle", undefined, { bundle: {} }),
    sandboxDoctor: async (params: unknown) => record(bridge, "sandbox.doctor", params, { ok: true }),
    sandboxSetup: async (params: unknown) => record(bridge, "sandbox.setup", params, { changed: true }),
    sandboxPull: async (params: unknown) => record(bridge, "sandbox.pull", params, { changed: true }),
    updateCheck: async (params: unknown) => record(bridge, "update.check", params, { cached: true }),
    conventionsRender: async (params: unknown) => record(bridge, "conventions.render", params, { rendered: "" })
  };
  return bridge;
}

function record<T>(bridge: { calls: Array<{ method: string; params?: unknown }> }, method: string, params: unknown, result: T): T {
  bridge.calls.push(params === undefined ? { method } : { method, params });
  return result;
}

function defaultArgsForAction(actionId: string): string {
  if (actionId === "checkpoint.branch") {
    return "checkpoint-1 feature/checkpoint-1";
  }
  if (actionId === "permission.rules.grant") {
    return "ask tool shell_run";
  }
  if (actionId === "profile.rename") {
    return "old";
  }
  return "demo";
}

interface ProtocolContractEntry {
  required_params?: string[];
  optional_params?: string[];
  requires_one_of?: string[];
  exactly_one_of?: string[];
  forbidden_params?: string[];
  secret_forbidden_fields?: string[];
  mutates?: boolean;
  workspace_trust_required?: boolean;
  workspace_required?: boolean;
}

interface ProtocolContractFile {
  method_defaults: ProtocolContractEntry;
  method_groups?: Array<ProtocolContractEntry & { methods: string[] }>;
  methods?: Record<string, ProtocolContractEntry>;
}

function loadProtocolContract(): Record<string, ProtocolContractEntry> {
  const repoRoot = resolve(__dirname, "../../../..");
  const payload = JSON.parse(
    readFileSync(resolve(repoRoot, "docs/generated/ide_protocol_methods.json"), "utf8")
  ) as ProtocolContractFile;
  const contract: Record<string, ProtocolContractEntry> = {};
  for (const group of payload.method_groups ?? []) {
    const { methods, ...groupEntry } = group;
    for (const method of methods) {
      contract[method] = { ...payload.method_defaults, ...groupEntry };
    }
  }
  for (const [method, entry] of Object.entries(payload.methods ?? {})) {
    contract[method] = { ...payload.method_defaults, ...(contract[method] ?? {}), ...entry };
  }
  return contract;
}

function validateProtocolParams(
  method: string,
  rawParams: unknown,
  contract: ProtocolContractEntry | undefined,
  actionId: string
): void {
  assert.ok(contract, `${actionId} calls ${method}, which is missing from the protocol fixture`);
  const params = isPlainRecord(rawParams) ? rawParams : {};
  for (const field of contract.required_params ?? []) {
    assert.notEqual(params[field], undefined, `${actionId} missing required ${method}.${field}`);
  }
  const requiresOneOf = contract.requires_one_of ?? [];
  if (requiresOneOf.length > 0) {
    assert.equal(
      requiresOneOf.some((field) => params[field] !== undefined && params[field] !== ""),
      true,
      `${actionId} must include one of ${requiresOneOf.join(", ")}`
    );
  }
  const exactlyOneOf = contract.exactly_one_of ?? [];
  if (exactlyOneOf.length > 0) {
    assert.equal(
      exactlyOneOf.filter((field) => params[field] !== undefined && params[field] !== false).length,
      1,
      `${actionId} must include exactly one of ${exactlyOneOf.join(", ")}`
    );
  }
  const forbidden = [...(contract.secret_forbidden_fields ?? []), ...(contract.forbidden_params ?? [])];
  for (const field of forbidden) {
    assert.equal(containsFieldRecursive(params, field), false, `${actionId} must not send ${field}`);
  }
  const allowed = new Set([
    ...(contract.required_params ?? []),
    ...(contract.optional_params ?? []),
    ...(contract.requires_one_of ?? []),
    ...(contract.exactly_one_of ?? [])
  ]);
  for (const key of Object.keys(params)) {
    assert.equal(allowed.has(key), true, `${actionId} sent unexpected ${method}.${key}`);
  }
  if (contract.workspace_trust_required) {
    assert.equal(params.workspace_trusted, true, `${actionId} must send workspace_trusted true`);
  }
}

function containsFieldRecursive(value: unknown, field: string): boolean {
  if (Array.isArray(value)) {
    return value.some((item) => containsFieldRecursive(item, field));
  }
  if (!isPlainRecord(value)) {
    return false;
  }
  return Object.entries(value).some(
    ([key, child]) => key === field || containsFieldRecursive(child, field)
  );
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function contractBridgeMock(methods: string[]): any {
  const supported = new Set(methods);
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  const target = {
    calls,
    ensureStarted: async () => undefined,
    supportsMethod: (method: string) => supported.has(method),
    supportsFeature: () => false,
    featureValue: () => undefined
  };
  return new Proxy(target, {
    get(obj, prop: string | symbol) {
      if (typeof prop !== "string") {
        return Reflect.get(obj, prop);
      }
      if (prop in obj) {
        return Reflect.get(obj, prop);
      }
      const method = BRIDGE_METHOD_TO_PROTOCOL[prop];
      assert.ok(method, `No bridge-to-protocol mapping for ${prop}`);
      return async (...args: unknown[]) => {
        const params = normalizeBridgeParams(prop, args);
        calls.push({ method, params });
        return defaultBridgeResult(method);
      };
    }
  });
}

const BRIDGE_METHOD_TO_PROTOCOL: Record<string, string> = {
  sessionStatus: "session.status",
  sessionUsage: "session.usage",
  sessionModelInfo: "session.modelInfo",
  sessionSubagentsStatus: "session.subagents.status",
  sessionSubagentsSetEnabled: "session.subagents.setEnabled",
  sessionTraceStatus: "session.trace.status",
  sessionTraceSetLevel: "session.trace.setLevel",
  sessionTraceListEvents: "session.trace.listEvents",
  sessionTraceReadArtifact: "session.trace.readArtifact",
  sessionTraceClear: "session.trace.clear",
  sessionTerminalsList: "session.terminals.list",
  sessionTerminalsShow: "session.terminals.show",
  sessionTerminalsKill: "session.terminals.kill",
  sessionTerminalsClear: "session.terminals.clear",
  sessionHistory: "session.history",
  sessionContext: "session.context",
  sessionCompact: "session.compact",
  sessionResume: "session.resume",
  sessionImagesList: "session.images.list",
  sessionImagesAdd: "session.images.add",
  sessionImagesClear: "session.images.clear",
  sessionSetMode: "session.setMode",
  sessionSetModel: "session.setModel",
  sessionSetStream: "session.setStream",
  sessionSetActiveWorkdir: "session.setActiveWorkdir",
  sessionClear: "session.clear",
  sessionShow: "session.show",
  sessionScore: "session.score",
  sessionSearch: "session.search",
  chatQueueList: "chat.queue.list",
  chatQueueGet: "chat.queue.get",
  chatQueueDelete: "chat.queue.delete",
  checkpointList: "checkpoint.list",
  checkpointDiff: "checkpoint.diff",
  checkpointRevert: "checkpoint.revert",
  checkpointRedo: "checkpoint.redo",
  checkpointBranch: "checkpoint.branch",
  codeReviewStart: "code.review.start",
  codeReviewResult: "code.review.result",
  permissionRulesList: "permission.rules.list",
  permissionRuleGrant: "permission.rules.grant",
  permissionRuleRevoke: "permission.rules.revoke",
  permissionEvaluate: "permission.evaluate",
  permissionSessionList: "permission.session.list",
  permissionSessionRevoke: "permission.session.revoke",
  reportCreate: "report.create",
  configGet: "config.get",
  configSet: "config.set",
  configSchema: "config.schema",
  configValidate: "config.validate",
  profileList: "profile.list",
  profileShow: "profile.show",
  profileAdd: "profile.add",
  profileRemove: "profile.remove",
  profileUse: "profile.use",
  profileRename: "profile.rename",
  profilePresets: "profile.presets",
  profilePreset: "profile.preset",
  profileConvert: "profile.convert",
  toolsCatalog: "tools.catalog",
  toolList: "tool.list",
  toolInfo: "tool.info",
  toolTrust: "tool.trust",
  toolUntrust: "tool.untrust",
  skillList: "skill.list",
  skillInfo: "skill.info",
  skillInit: "skill.init",
  skillValidate: "skill.validate",
  skillInstall: "skill.install",
  skillEnable: "skill.enable",
  skillDisable: "skill.disable",
  skillRemove: "skill.remove",
  doctorSummary: "doctor.summary",
  doctorProviders: "doctor.providers",
  doctorBundle: "doctor.bundle",
  sandboxDoctor: "sandbox.doctor",
  sandboxSetup: "sandbox.setup",
  sandboxPull: "sandbox.pull",
  updateCheck: "update.check",
  mcpStatus: "mcp.status",
  mcpPromptsList: "mcp.prompts.list",
  mcpPromptsGet: "mcp.prompts.get",
  mcpAuthStatus: "mcp.auth.status",
  mcpAuthLoginStart: "mcp.auth.login.start",
  mcpAuthLogout: "mcp.auth.logout",
  hooksList: "hooks.list",
  hooksDoctor: "hooks.doctor",
  hooksEffective: "hooks.effective",
  hooksTest: "hooks.test",
  hooksTrace: "hooks.trace",
  hooksTrust: "hooks.trust",
  hooksUntrust: "hooks.untrust",
  hooksInit: "hooks.init",
  hooksEnable: "hooks.enable",
  hooksDisable: "hooks.disable",
  conventionsList: "conventions.list",
  conventionsRender: "conventions.render",
  extSearch: "ext.search",
  extList: "ext.list",
  extInfo: "ext.info",
  extInstall: "ext.install",
  extUninstall: "ext.uninstall",
  extEnable: "ext.enable",
  extDisable: "ext.disable"
};

function normalizeBridgeParams(prop: string, args: unknown[]): Record<string, unknown> {
  switch (prop) {
    case "sessionStatus":
    case "sessionUsage":
    case "sessionSubagentsStatus":
    case "sessionTraceStatus":
    case "sessionTraceListEvents":
    case "sessionTraceClear":
    case "sessionTerminalsList":
    case "sessionContext":
    case "sessionImagesList":
    case "sessionImagesClear":
    case "sessionClear":
      return { session_id: args[0] };
    case "sessionSubagentsSetEnabled":
      return { session_id: args[0], enabled: args[1], workspace_trusted: args[2] };
    case "sessionTraceSetLevel":
      return { session_id: args[0], level: args[1], ...(isPlainRecord(args[2]) ? args[2] : {}) };
    case "sessionTraceReadArtifact":
      return { session_id: args[0], artifact_id: args[1], ...(isPlainRecord(args[2]) ? args[2] : {}) };
    case "sessionTerminalsShow":
      return { session_id: args[0], process_id: args[1], ...(isPlainRecord(args[2]) ? args[2] : {}) };
    case "sessionTerminalsKill":
    case "sessionTerminalsClear":
      return { session_id: args[0], process_id: args[1], workspace_trusted: args[2], ...(isPlainRecord(args[3]) ? args[3] : {}) };
    case "sessionModelInfo":
      return { session_id: args[0], ...(isPlainRecord(args[1]) ? args[1] : {}) };
    case "sessionHistory":
      return { session_id: args[0], pattern: args[1] };
    case "sessionSearch":
      return { session_id: args[0], query: args[1], ...(isPlainRecord(args[2]) ? args[2] : {}) };
    case "chatQueueList":
      return { session_id: args[0], ...(isPlainRecord(args[1]) ? args[1] : {}) };
    case "chatQueueGet":
    case "chatQueueDelete":
      return { session_id: args[0], prompt_id: args[1] };
    case "checkpointList":
      return { session_id: args[0], limit: args[1] };
    case "checkpointDiff":
      return { session_id: args[0], checkpoint_id: args[1], max_bytes: args[2] };
    case "checkpointRevert":
      return { session_id: args[0], checkpoint_id: args[1], workspace_trusted: true, confirm: true };
    case "checkpointRedo":
      return { session_id: args[0], workspace_trusted: true, confirm: true };
    case "checkpointBranch":
      return { session_id: args[0], checkpoint_id: args[1], name: args[2] };
    case "codeReviewResult":
      return { job_id: args[0] };
    case "permissionRulesList":
      return {};
    case "permissionRuleGrant":
      return { ...(isPlainRecord(args[0]) ? args[0] : {}), confirm: true };
    case "permissionRuleRevoke":
      return { rule_id: args[0], confirm: true };
    case "permissionSessionList":
      return { session_id: args[0] };
    case "permissionSessionRevoke":
      return { session_id: args[0], grant_id: args[1] };
    case "sessionCompact":
      return args[1] ? { session_id: args[0], focus: args[1] } : { session_id: args[0] };
    case "sessionResume":
      return { session_id: args[0], target_session_id: args[1] };
    case "sessionSetMode":
      return { session_id: args[0], mode: args[1] };
    case "sessionSetModel":
      return { session_id: args[0], model: args[1] };
    case "sessionSetStream":
      return { session_id: args[0], stream: args[1] };
    case "sessionSetActiveWorkdir":
      return { session_id: args[0], path: args[1] };
    default:
      if (isPlainRecord(args[0])) {
        return args[0];
      }
      return {};
  }
}

function defaultBridgeResult(method: string): Record<string, unknown> {
  if (method === "config.schema") {
    return { schema: { properties: { default_model: {}, api_key: {} } } };
  }
  return { ok: true, method };
}

function contractArgsForAction(actionId: string): string {
  switch (actionId) {
    case "session.search":
      return "purple widget";
    case "chat.queue.get":
    case "chat.queue.delete":
      return "prompt-1";
    case "checkpoint.diff":
    case "checkpoint.revert":
      return "checkpoint-1";
    case "checkpoint.branch":
      return "checkpoint-1 feature/checkpoint-1";
    case "code.review.start":
      return "working_tree";
    case "code.review.result":
      return "review-job-1";
    case "permission.rules.grant":
      return "ask tool shell_run";
    case "permission.rules.revoke":
      return "rule-1";
    case "permission.evaluate":
      return "shell_run";
    case "permission.session.revoke":
      return "grant-1";
    case "session.history":
      return "pytest";
    case "session.resume":
      return "retained-session";
    case "session.images.add":
      return "screenshots/fail.png";
    case "session.setMode":
      return "readonly";
    case "session.setModel":
      return "gpt-5";
    case "session.setStream":
      return "on";
    case "session.setActiveWorkdir":
      return "src";
    case "session.subagents.setEnabled":
      return "on";
    case "session.trace.setLevel":
      return "compact";
    case "session.trace.readArtifact":
      return "session:trace.txt";
    case "session.terminals.show":
    case "session.terminals.kill":
    case "session.terminals.clear":
      return "proc-1";
    case "session.show":
      return "session-1";
    case "report.create":
      return "diagnostic feedback";
    case "config.set":
      return "default_model=gpt-5";
    case "profile.show":
    case "profile.use":
    case "profile.remove":
      return "demo";
    case "profile.rename":
      return "old-profile";
    case "profile.preset":
      return "ollama";
    case "profile.convert":
      return "demo";
    case "tool.info":
    case "tool.trust":
    case "tool.untrust":
      return "demo-tool";
    case "skill.info":
    case "skill.validate":
    case "skill.init":
    case "skill.enable":
    case "skill.disable":
    case "skill.remove":
      return "python";
    case "skill.install":
      return "skills/python";
    case "mcp.prompts.get":
    case "mcp.auth.login.start":
    case "mcp.auth.logout":
      return "server-1";
    case "hooks.effective":
    case "hooks.test":
      return "PreToolUse";
    case "hooks.enable":
    case "hooks.disable":
      return "hook-1";
    case "ext.search":
      return "python";
    case "ext.info":
    case "ext.install":
    case "ext.uninstall":
    case "ext.enable":
    case "ext.disable":
      return "publisher.plugin";
    case "sandbox.pull":
      return "img-a,img-b";
    default:
      return "";
  }
}

function contractInputsForAction(actionId: string): string[] {
  switch (actionId) {
    case "profile.add":
      return ["demo", "https://api.example.test/v1", "gpt-5"];
    case "profile.rename":
      return ["new-profile"];
    case "profile.preset":
      return [""];
    case "profile.convert":
      return ["openai"];
    case "mcp.prompts.get":
      return ["prompt"];
    default:
      return [];
  }
}
