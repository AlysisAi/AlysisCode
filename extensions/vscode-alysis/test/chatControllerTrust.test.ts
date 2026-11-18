import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

import type { ChatSessionPersistence, ChatSessionReference } from "../src/chat/ChatController";
import { CockpitRuntimeState } from "../src/chat/CockpitRuntimeState";

let workspaceTrusted = false;
let inputBoxValue: string | undefined;
let activeTextEditor: any;
let openDialogValue: any[] | undefined;
let warningResponse: string | undefined;
let clipboardText = "";
let textDocuments: any[] = [];
const warnings: string[] = [];
const infos: string[] = [];
const webviewMessages: unknown[] = [];
let receiveWebviewMessage: ((message: unknown) => void) | undefined;

const disposable = { dispose: () => undefined };
const vscodeStub = {
  workspace: {
    get isTrusted() {
      return workspaceTrusted;
    },
    workspaceFolders: [{ uri: { scheme: "file", authority: "", fsPath: "/workspace/project" } }],
    getWorkspaceFolder: (uri: { scheme?: string; authority?: string; fsPath?: string }) =>
      vscodeStub.workspace.workspaceFolders.find((folder) =>
        folder.uri.scheme === uri.scheme
        && folder.uri.authority === (uri.authority ?? "")
        && typeof uri.fsPath === "string"
        && (uri.fsPath === folder.uri.fsPath || uri.fsPath.startsWith(`${folder.uri.fsPath}/`))
      ),
    get textDocuments() {
      return textDocuments;
    },
    asRelativePath: (uri: { fsPath?: string }) =>
      (uri.fsPath ?? "")
        .replace(/^[/\\]workspace[/\\]project[/\\]/, "")
        .replace(/\\/g, "/")
  },
  window: {
    get activeTextEditor() {
      return activeTextEditor;
    },
    showWarningMessage: async (message: string) => {
      warnings.push(message);
      return warningResponse;
    },
    showInformationMessage: async (message: string) => {
      infos.push(message);
      return undefined;
    },
    showOpenDialog: async () => openDialogValue,
    showErrorMessage: async () => undefined,
    showInputBox: async () => inputBoxValue,
    createWebviewPanel: () => ({
      webview: {
        html: "",
        cspSource: "vscode-resource:",
        asWebviewUri: (uri: unknown) => uri,
        onDidReceiveMessage: (callback: (message: unknown) => void) => {
          receiveWebviewMessage = callback;
          return disposable;
        },
        postMessage: async (message: unknown) => {
          webviewMessages.push(message);
          return true;
        }
      },
      onDidDispose: () => disposable,
      reveal: () => undefined,
      dispose: () => undefined
    })
  },
  commands: {
    executed: [] as string[],
    executeCommand: async (command: string) => {
      vscodeStub.commands.executed.push(command);
    }
  },
  env: {
    clipboard: {
      readText: async () => clipboardText
    }
  },
  ViewColumn: { Beside: 2 },
  Uri: {
    joinPath: (...parts: unknown[]) => ({ parts })
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

test.afterEach(() => {
  textDocuments = [];
  activeTextEditor = undefined;
  vscodeStub.workspace.workspaceFolders = [
    { uri: { scheme: "file", authority: "", fsPath: "/workspace/project" } }
  ];
});

test("mutating chat fails closed on an unsaved workspace file before transcript or backend side effects", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  textDocuments = [dirtyDocument("file", "/workspace/project/src/dirty.ts")];
  const bridge = bridgeMock();
  let startCalls = 0;
  bridge.startRun = async (params: Record<string, unknown>) => {
    startCalls += 1;
    return { session_id: String(params.session_id), job_id: "run-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const submitted = await controller.submitPrimaryMessage("change this file", "review");

  assert.equal(submitted, false);
  assert.equal(startCalls, 0);
  assert.equal(controller.testState().sessionId, null);
  assert.deepEqual(controller.testState().items, []);
  assert.equal(warnings.some((message) => /Save or revert 1 unsaved workspace file/.test(message)), true);
});

test("an unanswered warning toast cannot wedge the composer behind 'already starting a task'", async () => {
  // VS Code resolves showWarningMessage only when the toast is dismissed. One that slides into the
  // notification bell stays pending indefinitely, so awaiting it inside the guarded start held
  // runStartPromise forever and every later Send was refused. The user saw exactly that: a stack of
  // "already starting a task" notices and a composer that did nothing.
  workspaceTrusted = true;
  warnings.length = 0;
  textDocuments = [dirtyDocument("file", "/workspace/project/src/dirty.ts")];
  const originalShowWarning = vscodeStub.window.showWarningMessage;
  vscodeStub.window.showWarningMessage = ((message: string) => {
    warnings.push(message);
    return new Promise<undefined>(() => undefined); // never dismissed
  }) as typeof vscodeStub.window.showWarningMessage;
  try {
    const bridge = bridgeMock();
    let startCalls = 0;
    bridge.startRun = async (params: Record<string, unknown>) => {
      startCalls += 1;
      return { session_id: String(params.session_id), job_id: "run-job", status: "started" };
    };
    const controller = controllerForBridge(bridge);

    const blocked = await Promise.race([
      controller.submitPrimaryMessage("change this file", "review"),
      new Promise<"wedged">((resolve) => setTimeout(() => resolve("wedged"), 500))
    ]);
    assert.equal(blocked, false, "the refused start settles even though its warning toast never does");
    assert.equal(warnings.filter((message) => /unsaved workspace file/.test(message)).length, 1);

    // Save the file and try again: the guard must be free, not stuck on the first attempt.
    textDocuments = [];
    const second = await Promise.race([
      controller.submitPrimaryMessage("change this file", "review"),
      new Promise<"wedged">((resolve) => setTimeout(() => resolve("wedged"), 500))
    ]);
    assert.equal(second, true);
    assert.equal(startCalls, 1);
    assert.equal(
      controller.testState().items.some((item) => /already starting a task/.test(String(item.text))),
      false
    );
  } finally {
    vscodeStub.window.showWarningMessage = originalShowWarning;
    textDocuments = [];
  }
});

test("read-only chat remains available with an unsaved workspace file", async () => {
  workspaceTrusted = true;
  textDocuments = [dirtyDocument("file", "/workspace/project/src/dirty.ts")];
  const bridge = bridgeMock();
  let startCalls = 0;
  bridge.startRun = async (params: Record<string, unknown>) => {
    startCalls += 1;
    return { session_id: String(params.session_id), job_id: "run-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const submitted = await controller.submitPrimaryMessage("explain this file", "readonly");

  assert.equal(submitted, true);
  assert.equal(startCalls, 1);
  assert.equal(controller.testState().items.filter((item) => item.kind === "user").length, 1);
});

test("mutating chat rechecks dirty files after async preparation without leaving an unsent transcript row", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  textDocuments = [];
  const bridge = bridgeMock();
  let startCalls = 0;
  bridge.startRun = async (params: Record<string, unknown>) => {
    startCalls += 1;
    return { session_id: String(params.session_id), job_id: "run-job", status: "started" };
  };
  const controller = controllerForBridge(bridge, undefined, undefined, {
    collect: async () => {
      textDocuments = [dirtyDocument("file", "/workspace/project/src/became-dirty.ts")];
      return { blocks: [], skipped: [], totalBytes: 0, truncated: false };
    }
  });

  const submitted = await controller.submitPrimaryMessage("change after preparation", "review");

  assert.equal(submitted, false);
  assert.equal(startCalls, 0);
  assert.deepEqual(controller.testState().items, []);
  assert.equal(warnings.some((message) => /Save or revert 1 unsaved workspace file/.test(message)), true);
});

test("Add Selection keeps source out of the webview and enables bounded language-service context", async () => {
  workspaceTrusted = true;
  webviewMessages.length = 0;
  const bridge = bridgeMock();
  let request: Record<string, unknown> | undefined;
  const controller = controllerForBridge(bridge, undefined, undefined, {
    collect: async (value?: Record<string, unknown>) => {
      request = value;
      return {
        blocks: [{ type: "selection", uri: "file:///workspace/project/src/example.ts", content: "const answer = 42;" }],
        skipped: [],
        totalBytes: 100,
        truncated: false
      };
    }
  });
  activeTextEditor = {
    selection: {
      isEmpty: false,
      start: { line: 3, character: 0 },
      end: { line: 4, character: 8 }
    },
    document: {
      uri: { fsPath: "/workspace/project/src/example.ts" },
      languageId: "typescript",
      getText: () => "const answer = 42;"
    }
  };

  const prefills: string[] = [];
  controller.setPrimaryChatView({
    reveal: async () => undefined,
    prefill: async (text: string) => {
      prefills.push(text);
    }
  });

  await controller.addSelectionToTask();
  await Promise.resolve();

  assert.deepEqual(prefills, ["Help me with the attached editor context."], "composer prefill posted");
  assert.doesNotMatch(JSON.stringify(prefills), /example\.ts|const answer/);
  assert.equal(request?.includeReferences, true);
  assert.equal(request?.includeDefinitions, true);
  assert.equal(request?.includeHover, true);
  assert.equal((bridge as any).sendChatCalls?.length ?? 0, 0, "selection is not sent automatically");
  activeTextEditor = undefined;
});

test("host-owned selection attachment is delivered to run.start and consumed after acceptance", async () => {
  workspaceTrusted = true;
  webviewMessages.length = 0;
  const bridge = bridgeMock();
  const contexts: unknown[] = [];
  bridge.startRun = async (params: Record<string, unknown>) => {
    contexts.push(params.context_blocks);
    return { session_id: String(params.session_id), job_id: "run-job", status: "started" };
  };
  const controller = controllerForBridge(bridge, undefined, undefined, {
    collect: async () => ({
      blocks: [{ type: "selection", uri: "file:///workspace/project/src/dirty.ts", content: "const unsaved = 'rocket';" }],
      skipped: [],
      totalBytes: 120,
      truncated: false
    })
  });
  activeTextEditor = {
    selection: {
      isEmpty: false,
      start: { line: 0, character: 0 },
      end: { line: 0, character: 24 }
    },
    document: {
      uri: { fsPath: "/workspace/project/src/dirty.ts" },
      languageId: "typescript",
      isDirty: true,
      getText: () => "const unsaved = '🚀';"
    }
  };

  await controller.addSelectionToTask();
  assert.equal(await controller.submitPrimaryMessage("inspect it", "readonly"), true);
  assert.match(JSON.stringify(contexts[0]), /const unsaved = 'rocket'/);
  assert.equal((controller as any).pendingContextBlocks.length, 0);
  activeTextEditor = undefined;
});

test("pending host-owned context survives a rejected send and is consumed by the successful retry", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let attempts = 0;
  const contexts: unknown[] = [];
  bridge.startRun = async (params: Record<string, unknown>) => {
    attempts += 1;
    contexts.push(params.context_blocks);
    if (attempts === 1) {
      throw new Error("temporary provider failure");
    }
    return { session_id: String(params.session_id), job_id: "run-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);
  await controller.attachContextBlock({
    type: "past_session",
    content: "bounded prior result",
    session_id: "past-session",
    turn_id: "result-1"
  });

  assert.equal(await controller.submitPrimaryMessage("retry this", "readonly"), false);
  assert.equal((controller as any).pendingContextBlocks.length, 1);
  assert.equal(await controller.submitPrimaryMessage("retry this", "readonly"), true);
  assert.match(JSON.stringify(contexts[1]), /bounded prior result/);
  assert.equal((controller as any).pendingContextBlocks.length, 0);
});

test("file and terminal attachments stay host-owned and are delivered exactly once", async () => {
  workspaceTrusted = true;
  webviewMessages.length = 0;
  openDialogValue = [{ scheme: "file", fsPath: "/workspace/project/src/chosen.ts", toString: () => "file:///workspace/project/src/chosen.ts" }];
  warningResponse = "Read Clipboard";
  clipboardText = "terminal-private-sentinel";
  const bridge = bridgeMock();
  const contexts: unknown[] = [];
  let sendNumber = 0;
  bridge.sendChat = async (sessionId: string, _message: string, options?: { context_blocks?: unknown[] }) => {
    contexts.push(options?.context_blocks ?? []);
    sendNumber += 1;
    return { session_id: sessionId, job_id: `chat-job-${sendNumber}`, status: "completed" };
  };
  const explicitRoots: Array<string | undefined> = [];
  const controller = controllerForBridge(bridge, undefined, undefined, {
    collect: async () => ({ blocks: [], skipped: [], totalBytes: 0, truncated: false }),
    collectFiles: async (_uris, root) => {
      explicitRoots.push(root);
      return {
        blocks: [{ type: "file", uri: "file:///workspace/project/src/chosen.ts", content: "file-private-sentinel" }],
        skipped: [],
        totalBytes: 100,
        truncated: false
      };
    },
    collectTerminalExcerpt: async (_selection, root) => {
      explicitRoots.push(root);
      return {
        blocks: [{ type: "terminal", content: "terminal-private-sentinel" }],
        skipped: [],
        totalBytes: 100,
        truncated: false
      };
    }
  });

  try {
    await controller.addFilesToTask();
    await controller.addTerminalExcerptToTask();
    receiveWebviewMessage?.({ type: "webview.ready" });
    await Promise.resolve();

    assert.doesNotMatch(JSON.stringify(webviewMessages), /file-private-sentinel|terminal-private-sentinel/);
    assert.deepEqual(explicitRoots, ["/workspace/project", "/workspace/project"]);
    assert.equal(await controller.ensureLiveSession(), "chat-session");
    assert.equal(await controller.submitPrimaryMessage("inspect attachments", "readonly"), true);
    assert.match(JSON.stringify(contexts[0]), /file-private-sentinel/);
    assert.match(JSON.stringify(contexts[0]), /terminal-private-sentinel/);

    assert.equal(await controller.submitPrimaryMessage("second turn", "readonly"), true);
    assert.doesNotMatch(JSON.stringify(contexts[1]), /file-private-sentinel|terminal-private-sentinel/);
    assert.equal((controller as any).pendingContextBlocks.length, 0);
  } finally {
    openDialogValue = undefined;
    warningResponse = undefined;
    clipboardText = "";
  }
});

test("ChatController blocks untrusted non-readonly submit before bridge start or SecretStorage", async () => {
  workspaceTrusted = false;
  let ensureStartedCalls = 0;
  let secretReads = 0;
  const errors: string[] = [];
  const controller = new ChatController(
    {
      on: () => undefined,
      ensureStarted: async () => {
        ensureStartedCalls += 1;
      },
      createSession: async () => {
        throw new Error("createSession must not be called");
      },
      sendChat: async () => {
        throw new Error("sendChat must not be called");
      },
      cancelSession: async () => ({}),
      respondApproval: async () => ({ allow: false, allow_for_session: false, allow_for_session_warning: null }),
      jobStatus: async () => ({ job_id: "job-1", session_id: "session-1", status: "completed" }),
      sessionList: async () => ({ sessions: [] }),
      getEvents: async () => ({ events: [], truncated: false }),
      artifactList: async () => ({ artifacts: [], truncated: false }),
      artifactRead: async () => ({}),
      shutdown: async () => undefined
    },
    () => ({
      cliPath: "alysis",
      defaultMode: "review",
      defaultModel: "",
      baseUrl: "",
      provider: "",
      transport: "stdio",
      sandboxProfile: "default",
      showStatusBar: true,
      autoStartBridge: false,
      security: {
        isWorkspaceTrusted: false,
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
    }),
    {
      getApiKey: async () => {
        secretReads += 1;
        return "secret";
      }
    } as any,
    {
      setError: (message: string) => errors.push(message),
      setActiveRun: () => undefined,
      setBridgeOk: () => undefined,
      setIdle: () => undefined
    } as any,
    { setSessions: () => undefined, setActiveSession: () => undefined, rememberSession: () => undefined } as any,
    { appendLine: () => undefined, show: () => undefined } as any
  );

  await (controller as any).sendUserMessage("write something");

  assert.equal(ensureStartedCalls, 0);
  assert.equal(secretReads, 0);
  assert.match(errors.join("\n"), /Workspace Trust.*readonly/);
});

test("ChatController routes unknown owned events to Output without creating conversation cards", () => {
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  const outputLines: string[] = [];
  (controller as any).output.appendLine = (message: string) => outputLines.push(message);
  (controller as any).sessionId = "chat-session";
  (controller as any).ownedSessionIds.add("chat-session");

  bridge.emitEvent({
    protocol_version: "1",
    session_id: "chat-session",
    run_id: null,
    job_id: null,
    sequence: 1,
    timestamp: "2026-07-30T00:00:00.000Z",
    type: "internal.runtime_debug",
    payload: { raw_method: "rs.read", patch: "private patch body" }
  });

  assert.deepEqual(controller.conversationState().items, []);
  assert.deepEqual(outputLines, ["Ignored unsupported Alysis Code event type: internal.runtime_debug"]);
  assert.equal(outputLines.join("\n").includes("rs.read"), false);
  assert.equal(outputLines.join("\n").includes("private patch body"), false);
});

test("ChatController enables semantic activity from the connected bridge event capability", async () => {
  const bridge = bridgeMock();
  bridge.supportedEvents.add("activity_update");
  const controller = controllerForBridge(bridge);
  (controller as any).sessionId = "chat-session";
  (controller as any).ownedSessionIds.add("chat-session");
  await (controller as any).ensureCredentialBridge((controller as any).getConfig());

  bridge.emitEvent({
    protocol_version: "1",
    session_id: "chat-session",
    run_id: null,
    job_id: null,
    sequence: 1,
    timestamp: "2026-07-30T00:00:00.000Z",
    type: "tool_call_started",
    payload: { call_id: "call-1", name: "rs.read", arguments_preview: "raw arguments" }
  });
  // The legacy start opens a row immediately (a tool the backend never describes semantically must
  // still be visible); the semantic activity that follows adopts that row rather than adding one.
  assert.equal(controller.conversationState().items.length, 1);

  bridge.emitEvent({
    protocol_version: "1",
    session_id: "chat-session",
    run_id: null,
    job_id: null,
    sequence: 2,
    timestamp: "2026-07-30T00:00:01.000Z",
    type: "activity_update",
    payload: {
      activity_id: "call-1",
      kind: "read",
      display_title: "Read source file",
      status: "running",
      target: "src/index.ts"
    }
  });

  const items = controller.conversationState().items;
  assert.equal(items.length, 1);
  assert.equal(items[0].title, "Read source file");
  assert.equal(JSON.stringify(items).includes("rs.read"), false);
  assert.equal(JSON.stringify(items).includes("raw arguments"), false);
});

test("primary sidebar keeps chat approvals pending when the Details panel is closed", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  controller.setPrimaryChatView({
    reveal: async () => undefined,
    prefill: async () => undefined
  });
  (controller as any).sessionId = "chat-session";
  (controller as any).activeJobId = "chat-job";
  (controller as any).ownedSessionIds.add("chat-session");
  (controller as any).ownedJobIds.add("chat-job");

  bridge.emitEvent({
    protocol_version: "1",
    session_id: "chat-session",
    run_id: null,
    job_id: "chat-job",
    sequence: 3,
    timestamp: "2026-07-26T00:00:00.000Z",
    type: "prompt_for_input",
    payload: {
      kind: "approval",
      approval_kind: "shell_run",
      approval_id: "sidebar-approval",
      prompt_id: "sidebar-approval",
      reason: "Run the verification suite",
      preview: "npm test"
    }
  });
  await delay(20);

  assert.equal(bridge.respondApprovalCalls.length, 0, "the visible primary sidebar owns the approval");
  assert.equal(
    controller.conversationState().items.some((item) => item.kind === "approval" && item.status === "pending"),
    true
  );
});

test("primary sidebar sends an approval decision only once on a double click", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  controller.setPrimaryChatView({ reveal: async () => undefined, prefill: async () => undefined });
  (controller as any).sessionId = "chat-session";
  (controller as any).ownedSessionIds.add("chat-session");
  let releaseResponse: (() => void) | undefined;
  const responseGate = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  bridge.respondApproval = async (params: any) => {
    bridge.respondApprovalCalls.push(params);
    await responseGate;
    return { allow: true, allow_for_session: false, allow_for_session_warning: null };
  };
  bridge.emitEvent({
    protocol_version: "1",
    session_id: "chat-session",
    run_id: null,
    job_id: null,
    sequence: 1,
    timestamp: "2026-07-26T00:00:00.000Z",
    type: "prompt_for_input",
    payload: {
      kind: "approval",
      approval_kind: "shell_run",
      approval_id: "approval-once",
      prompt_id: "approval-once",
      reason: "Run tests",
      preview: "npm test"
    }
  });

  const first = controller.respondToPrimaryApproval("approval-once", "allow_once");
  const duplicate = controller.respondToPrimaryApproval("approval-once", "allow_once");
  await delay(0);

  assert.equal(bridge.respondApprovalCalls.length, 1);
  releaseResponse?.();
  await Promise.all([first, duplicate]);
  assert.equal(controller.conversationState().approvals.length, 0);
});

test("chat approval allow stays pending and never reaches the backend when the workspace becomes dirty", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  textDocuments = [dirtyDocument("file", "/workspace/project/src/became-dirty.ts")];
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  controller.setPrimaryChatView({ reveal: async () => undefined, prefill: async () => undefined });
  (controller as any).sessionId = "chat-session";
  (controller as any).ownedSessionIds.add("chat-session");
  bridge.emitEvent({
    protocol_version: "1",
    session_id: "chat-session",
    run_id: null,
    job_id: "chat-job",
    sequence: 1,
    timestamp: "2026-07-30T00:00:00.000Z",
    type: "prompt_for_input",
    payload: {
      kind: "approval",
      approval_kind: "fs_write",
      approval_id: "dirty-approval",
      prompt_id: "dirty-approval",
      reason: "Write the selected change",
      preview: "src/became-dirty.ts"
    }
  });

  await controller.respondToPrimaryApproval("dirty-approval", "allow_once");

  assert.deepEqual(bridge.respondApprovalCalls, []);
  assert.equal(controller.conversationState().approvals.some((approval) => approval.approvalId === "dirty-approval"), true);
  assert.equal(warnings.some((message) => /Save or revert 1 unsaved workspace file/.test(message)), true);
});

test("ChatController routes slash commands before chat.send", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let sendChatCalls = 0;
  bridge.sendChat = async () => {
    sendChatCalls += 1;
    return { session_id: "chat-session", job_id: "chat-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);
  const routed: string[] = [];
  controller.setSlashCommandRouter({
    execute: async (input: string) => {
      routed.push(input);
      return { handled: true, command: "/doctor", notice: "Ran setup checks from /doctor." };
    }
  } as any);

  await (controller as any).sendUserMessage("/doctor");

  assert.deepEqual(routed, ["/doctor"]);
  assert.equal(sendChatCalls, 0);
  assert.equal(controller.testState().items.some((item) => item.text.includes("Ran setup checks")), true);
});

test("ChatController.ensureLiveSession creates a session without sending chat", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let createSessionCalls = 0;
  let sendChatCalls = 0;
  bridge.createSession = async () => {
    createSessionCalls += 1;
    return { session_id: "created-live", workspace_root: "/workspace/project", mode: "readonly" };
  };
  bridge.sendChat = async () => {
    sendChatCalls += 1;
    return { session_id: "created-live", job_id: "chat-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const sessionId = await controller.ensureLiveSession();

  assert.equal(sessionId, "created-live");
  assert.equal(createSessionCalls, 1);
  assert.equal(sendChatCalls, 0);
});

test("ChatController serializes concurrent live-session creation", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let createSessionCalls = 0;
  let releaseCreate: (() => void) | undefined;
  const createGate = new Promise<void>((resolve) => {
    releaseCreate = resolve;
  });
  bridge.createSession = async () => {
    createSessionCalls += 1;
    await createGate;
    return { session_id: "single-live", workspace_root: "/workspace/project", mode: "readonly" };
  };
  const controller = controllerForBridge(bridge);

  const first = controller.ensureLiveSession();
  const second = controller.ensureLiveSession();
  for (let attempt = 0; attempt < 20 && createSessionCalls === 0; attempt += 1) {
    await delay(0);
  }
  assert.equal(createSessionCalls, 1);

  releaseCreate?.();
  assert.deepEqual(await Promise.all([first, second]), ["single-live", "single-live"]);
  assert.equal(createSessionCalls, 1);
});

test("ChatController ignores and closes a session.create response superseded by New Session", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let createSessionCalls = 0;
  let releaseCreate: (() => void) | undefined;
  const createGate = new Promise<void>((resolve) => {
    releaseCreate = resolve;
  });
  bridge.createSession = async () => {
    createSessionCalls += 1;
    await createGate;
    return { session_id: "late-session", workspace_root: "/workspace/project", mode: "readonly" };
  };
  let sendChatCalls = 0;
  bridge.sendChat = async () => {
    sendChatCalls += 1;
    return { session_id: "late-session", job_id: "late-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const pending = (controller as any).sendUserMessage("slow session", false, false) as Promise<boolean>;
  for (let attempt = 0; attempt < 20 && createSessionCalls === 0; attempt += 1) {
    await delay(0);
  }
  assert.equal(createSessionCalls, 1);

  await controller.newSession();
  releaseCreate?.();

  assert.equal(await pending, false);
  assert.equal(controller.testState().sessionId, null);
  assert.equal(sendChatCalls, 0);
  assert.deepEqual(bridge.cancelSessionCalls, ["late-session"]);
  assert.deepEqual(bridge.cancelSessionOptions, [
    { reason: "session_start_superseded", closeWhenIdle: true }
  ]);
});

test("ChatController ignores and closes a run.start response superseded by New Session", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let startRunCalls = 0;
  let releaseRun: (() => void) | undefined;
  const runGate = new Promise<void>((resolve) => {
    releaseRun = resolve;
  });
  bridge.startRun = async (params: Record<string, unknown>) => {
    startRunCalls += 1;
    await runGate;
    return { session_id: String(params.session_id), job_id: "late-run-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const pending = controller.submitPrimaryMessage("slow run", "review");
  for (let attempt = 0; attempt < 20 && startRunCalls === 0; attempt += 1) {
    await delay(0);
  }
  assert.equal(startRunCalls, 1);

  await controller.newSession();
  releaseRun?.();

  assert.equal(await pending, false);
  assert.equal(controller.testState().sessionId, null);
  assert.deepEqual(bridge.cancelSessionCalls, ["chat-session"]);
});

test("ChatController starts the newer message after a superseded session.create settles", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let createSessionCalls = 0;
  let releaseFirstCreate: (() => void) | undefined;
  const firstCreateGate = new Promise<void>((resolve) => {
    releaseFirstCreate = resolve;
  });
  bridge.createSession = async () => {
    createSessionCalls += 1;
    if (createSessionCalls === 1) {
      await firstCreateGate;
      return { session_id: "superseded-session", workspace_root: "/workspace/project", mode: "readonly" };
    }
    return { session_id: "current-session", workspace_root: "/workspace/project", mode: "readonly" };
  };
  const sent: Array<{ sessionId: string; message: string }> = [];
  bridge.sendChat = async (sessionId: string, message: string) => {
    sent.push({ sessionId, message });
    return { session_id: sessionId, job_id: "current-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const first = (controller as any).sendUserMessage("old message", false, false) as Promise<boolean>;
  for (let attempt = 0; attempt < 20 && createSessionCalls === 0; attempt += 1) {
    await delay(0);
  }
  await controller.newSession();
  const second = (controller as any).sendUserMessage("new message", false, false) as Promise<boolean>;
  releaseFirstCreate?.();

  assert.equal(await first, false);
  assert.equal(await second, true);
  assert.equal(controller.testState().sessionId, "current-session");
  assert.equal(createSessionCalls, 2);
  assert.deepEqual(bridge.cancelSessionCalls, ["superseded-session"]);
  assert.deepEqual(sent, [{ sessionId: "current-session", message: "new message" }]);
});

test("ChatController continues the submitted message across a credential profile upgrade", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  (bridge as any).ensureStartedForProfile = async () => {
    bridge.emitReset("bridge_profile_upgrade");
    return { restarted: true, profile: {} };
  };
  const sent: Array<{ sessionId: string; message: string }> = [];
  bridge.sendChat = async (sessionId: string, message: string) => {
    sent.push({ sessionId, message });
    return { session_id: sessionId, job_id: "upgraded-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const submitted = await (controller as any).sendUserMessage("hello after diagnostics", false, false);

  assert.equal(submitted, true);
  assert.deepEqual(sent, [{ sessionId: "chat-session", message: "hello after diagnostics" }]);
  assert.equal(controller.testState().activeJobId, "upgraded-job");
});

test("ChatController verifies diagnostics for a completed chat job", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.sendChat = async () => ({
    session_id: "chat-session",
    job_id: "verified-job",
    status: "completed"
  });
  let captures = 0;
  let verifications = 0;
  const diagnosticsVerification = {
    captureBaseline: () => {
      captures += 1;
      return { schemaVersion: 1 } as any;
    },
    verifyAfterChanges: async () => {
      verifications += 1;
      return {
        status: "failed",
        blocksCompletion: true,
        reason: "one new error",
        introducedCounts: { error: 1, warning: 0, information: 0, hint: 0, total: 1 }
      } as any;
    }
  };
  const controller = controllerForBridge(bridge, undefined, undefined, undefined, diagnosticsVerification);

  assert.equal(await (controller as any).sendUserMessage("introduce a diagnostic", false, false), true);
  assert.equal(captures, 1);
  assert.equal(verifications, 1);
  const state = controller.conversationState();
  assert.equal(state.jobStatus, "needs_attention");
  assert.equal(state.items.some((item) =>
    item.kind === "error"
    && item.status === "needs_attention"
    && item.metadata?.backend_status === "completed"
    && item.metadata?.verification_status === "failed"
  ), true);
});

test("ChatController binds diagnostic verification to the active session workspace root", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let capturedScope: { workspaceRoot: string } | undefined;
  const diagnosticsVerification = {
    captureBaseline: (_required?: readonly string[], scope?: { workspaceRoot: string }) => {
      capturedScope = scope;
      return { schemaVersion: 1 } as any;
    },
    verifyAfterChanges: async () => ({ status: "passed", blocksCompletion: false }) as any
  };
  const controller = controllerForBridge(bridge, undefined, undefined, undefined, diagnosticsVerification);

  assert.equal(await controller.submitPrimaryMessage("explain diagnostics", "readonly"), true);
  assert.deepEqual(capturedScope, { workspaceRoot: "/workspace/project" });
});

test("a bridge exit and a bridge restart are both recorded on the Cockpit timeline", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const runtime = new CockpitRuntimeState();
  controllerForBridge(bridge, runtime);

  bridge.emitExit(7);
  bridge.emitReset("credentials_changed");

  const events = runtime.snapshot().events;
  assert.deepEqual(
    events.map((event) => [event.source, event.title, event.severity]),
    [
      ["bridge", "Bridge restarting", "warning"],
      ["bridge", "Bridge exited", "error"]
    ]
  );
  assert.equal(events[1].message.includes("exited with code 7"), true);
  assert.equal(events[1].exitCode, 7);
});

test("ChatController restores retained context after an unexpected bridge exit", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.supportedMethods.add("session.resume");
  let createSessionCalls = 0;
  bridge.createSession = async () => {
    createSessionCalls += 1;
    return {
      session_id: createSessionCalls === 1 ? "before-crash" : "after-crash",
      workspace_root: "/workspace/project",
      mode: "readonly"
    };
  };
  const sent: Array<{ sessionId: string; message: string }> = [];
  bridge.sendChat = async (sessionId: string, message: string) => {
    sent.push({ sessionId, message });
    return { session_id: sessionId, job_id: "recovered-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  assert.equal(await controller.ensureLiveSession(), "before-crash");
  bridge.emitExit(17);
  assert.equal(controller.testState().sessionId, null);

  assert.equal(await controller.submitPrimaryMessage("continue after reconnect", "readonly"), true);
  assert.deepEqual(bridge.sessionResumeCalls, [
    { sessionId: "after-crash", targetSessionId: "before-crash" }
  ]);
  assert.deepEqual(sent, [
    { sessionId: "after-crash", message: "continue after reconnect" }
  ]);
  assert.equal(
    controller.testState().items.some((item) => item.text.includes("restored your previous conversation")),
    true
  );
});

test("ChatController restores and refreshes a workspace-persisted session reference", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.supportedMethods.add("session.resume");
  (bridge as any).getEvents = async (sessionId: string) => ({
    events: [
      {
        protocol_version: "1.0",
        session_id: sessionId,
        sequence: 1,
        timestamp: "2026-07-30T10:00:00Z",
        type: "message_delta",
        payload: { text: "Earlier retained answer" }
      },
      {
        protocol_version: "1.0",
        session_id: sessionId,
        sequence: 2,
        timestamp: "2026-07-30T10:00:01Z",
        type: "message_end",
        payload: {}
      }
    ],
    truncated: false
  });
  const updates: Array<ChatSessionReference | undefined> = [];
  const controller = controllerForBridge(bridge, undefined, {
    get: () => "retained-before-reload",
    update: async (reference) => {
      updates.push(reference);
    }
  });

  assert.equal(await controller.submitPrimaryMessage("continue after reload", "readonly"), true);
  await controller.shutdown({ shutdownBridge: false });

  assert.deepEqual(bridge.sessionResumeCalls, [
    { sessionId: "chat-session", targetSessionId: "retained-before-reload" }
  ]);
  assert.deepEqual(updates, [{
    sessionId: "chat-session",
    workspace: { root: "/workspace/project", scheme: "file", authority: "" },
    mode: "readonly"
  }]);
  const messages = controller.conversationState().items.filter((item) => item.kind === "assistant" || item.kind === "user");
  assert.deepEqual(messages.map((item) => item.text), ["Earlier retained answer", "continue after reload"]);
});

test("event replay orders concurrent live events losslessly and deduplicates overlapping sequences", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  assert.equal(await controller.ensureLiveSession(), "chat-session");

  let releaseReplay: (() => void) | undefined;
  const replayGate = new Promise<void>((resolve) => {
    releaseReplay = resolve;
  });
  (bridge as any).getEvents = async () => {
    await replayGate;
    return {
      events: [
        protocolEvent(1, "message_delta", { text: "Earlier " }),
        protocolEvent(2, "message_end", { text: "Earlier answer" }),
        protocolEvent(3, "message_delta", { text: "Later " })
      ],
      truncated: false
    };
  };

  const replaying = (controller as any).replayEvents() as Promise<void>;
  bridge.emitEvent(protocolEvent(3, "message_delta", { text: "Later " }));
  bridge.emitEvent(protocolEvent(4, "message_end", { text: "Later answer" }));
  releaseReplay?.();
  await replaying;

  const assistantMessages = controller.conversationState().items
    .filter((item) => item.kind === "assistant")
    .map((item) => item.text);
  assert.deepEqual(assistantMessages, ["Earlier answer", "Later answer"]);
  assert.equal(controller.conversationState().lastSequence, 4);
});

test("ChatController discards a chat.send result superseded by New Session", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let sendCalls = 0;
  let releaseSend: (() => void) | undefined;
  const sendGate = new Promise<void>((resolve) => {
    releaseSend = resolve;
  });
  bridge.sendChat = async (sessionId: string) => {
    sendCalls += 1;
    await sendGate;
    return { session_id: sessionId, job_id: "late-chat-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);
  assert.equal(await controller.ensureLiveSession(), "chat-session");

  const pending = controller.submitPrimaryMessage("slow follow-up", "readonly");
  for (let attempt = 0; attempt < 20 && sendCalls === 0; attempt += 1) {
    await delay(0);
  }
  await controller.newSession();
  assert.deepEqual(bridge.cancelSessionOptions, [
    { reason: "new_session_requested", closeWhenIdle: true }
  ]);
  releaseSend?.();

  assert.equal(await pending, false);
  assert.equal(controller.testState().sessionId, null);
  assert.equal(controller.testState().activeJobId, null);
});

test("New Session keeps prior-session cleanup failure visible", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  const bridge = bridgeMock();
  bridge.cancelSession = async () => {
    throw new Error("cleanup failed");
  };
  const controller = controllerForBridge(bridge);
  assert.equal(await controller.ensureLiveSession(), "chat-session");

  await controller.newSession();

  assert.equal(controller.testState().sessionId, null);
  assert.equal(
    warnings.some((message) => message.includes("could not confirm cleanup of the previous session")),
    true
  );
});

test("chat forwards the sandbox preference and displays the runtime's effective policy", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let observed: Record<string, unknown> | undefined;
  const create = bridge.createSession;
  bridge.createSession = async (params: Record<string, unknown>) => {
    observed = params;
    return { ...await create(params), sandbox_mode: "strict" };
  };
  const controller = controllerForBridge(bridge);
  const original = (controller as any).getConfig;
  (controller as any).getConfig = () => ({ ...original(), sandboxProfile: "off" });
  await controller.ensureLiveSession();
  assert.equal(observed?.sandbox_profile, "off");
  assert.equal(controller.primaryCockpitState().cockpit.status.sandbox, "strict");
});

test("ChatController Run Task uses run.start instead of chat.send", async () => {
  workspaceTrusted = true;
  inputBoxValue = "inspect this workspace";
  try {
    const bridge = bridgeMock();
    let runStartParams: Record<string, unknown> | undefined;
    let createSessionParams: Record<string, unknown> | undefined;
    let sendChatCalls = 0;
    const originalCreateSession = bridge.createSession;
    bridge.createSession = async (params: Record<string, unknown>) => {
      createSessionParams = params;
      return originalCreateSession(params);
    };
    bridge.startRun = async (params: Record<string, unknown>) => {
      runStartParams = params;
      return { session_id: String(params.session_id), job_id: "run-job", status: "started" };
    };
    bridge.sendChat = async () => {
      sendChatCalls += 1;
      return { session_id: "chat-session", job_id: "chat-job", status: "started" };
    };
    const controller = controllerForBridge(bridge);

    await controller.runTask();

    // Session options go to session.create; run.start on that session carries turn params only.
    // The bridge rejects workspace/permissions/model/base_url on an existing session
    // (`unsupported_turn_option`), which the integration mock now mirrors.
    assert.deepEqual(createSessionParams, { workspace: "/workspace/project", mode: "readonly", sandbox_profile: "default" });
    assert.equal(typeof runStartParams?.idempotency_key, "string");
    assert.deepEqual(runStartParams?.context_blocks, []);
    assert.deepEqual({ ...runStartParams, idempotency_key: "<id>" }, {
      instruction: "inspect this workspace",
      idempotency_key: "<id>",
      context_blocks: [],
      session_id: "chat-session"
    });
    assert.equal(sendChatCalls, 0);
    assert.equal(controller.testState().sessionId, "chat-session");
    assert.equal(controller.testState().activeJobId, "run-job");
  } finally {
    inputBoxValue = undefined;
  }
});

test("ChatController Start Here task uses the mode chosen for that task", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let runStartParams: Record<string, unknown> | undefined;
  bridge.startRun = async (params: Record<string, unknown>) => {
    runStartParams = params;
    return { session_id: String(params.session_id), job_id: "start-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const started = await controller.runTaskWithInstruction("  update the welcome screen  ", "review");

  assert.equal(started, true);
  assert.equal(typeof runStartParams?.idempotency_key, "string");
  assert.deepEqual(runStartParams?.context_blocks, []);
  assert.deepEqual({ ...runStartParams, idempotency_key: "<id>" }, {
    instruction: "update the welcome screen",
    idempotency_key: "<id>",
    context_blocks: [],
    session_id: "chat-session"
  });
  assert.equal(controller.testState().sessionId, "chat-session");
  assert.equal(controller.testState().cockpit.status.mode, "review");
});

test("correlated Start Here retries reuse one backend idempotency key and reject request-id payload conflicts", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const starts: Record<string, unknown>[] = [];
  bridge.startRun = async (params: Record<string, unknown>) => {
    starts.push(params);
    return { session_id: String(params.session_id), job_id: "correlated-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);
  const requestId = "request-recovered-after-webview-reload";

  assert.equal(await controller.submitPrimaryMessage("keep this task", "review", requestId), true);
  assert.equal(
    await controller.submitPrimaryMessage("keep this task", "review", requestId),
    true,
    "a retry after a dropped host acknowledgement reports the accepted result"
  );
  assert.equal(
    await controller.submitPrimaryMessage("different payload", "review", requestId),
    false,
    "the same request id cannot be rebound to a different payload"
  );
  assert.equal(starts.length, 1);
  assert.equal(starts[0]?.idempotency_key, requestId);
  assert.equal(controller.primaryCockpitState().lastAcceptedTaskRequestId, requestId);
  assert.deepEqual(
    controller.conversationState().items.filter((item) => item.kind === "user").map((item) => item.text),
    ["keep this task"]
  );
  await controller.shutdown({ shutdownBridge: false });
});

test("correlated follow-up retries dedupe chat.send with the same backend idempotency key", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const sends: Array<{ message: string; options: Record<string, unknown> | undefined }> = [];
  bridge.sendChat = async (_sessionId: string, message: string, options?: Record<string, unknown>) => {
    sends.push({ message, options });
    return { session_id: "chat-session", job_id: "follow-up-job", status: "queued" };
  };
  const controller = controllerForBridge(bridge);
  assert.equal(await controller.ensureLiveSession(), "chat-session");
  const requestId = "follow-up-recovered-after-reload";

  assert.equal(await controller.submitPrimaryMessage("check the tests", "readonly", requestId), true);
  assert.equal(await controller.submitPrimaryMessage("check the tests", "readonly", requestId), true);
  assert.equal(sends.length, 1);
  assert.equal(sends[0]?.options?.idempotency_key, requestId);
  assert.deepEqual(
    controller.conversationState().items.filter((item) => item.kind === "user").map((item) => item.text),
    ["check the tests"]
  );
  await controller.shutdown({ shutdownBridge: false });
});

test("ChatController sends collected typed IDE context with the prompt", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let runStartParams: Record<string, unknown> | undefined;
  bridge.startRun = async (params: Record<string, unknown>) => {
    runStartParams = params;
    return { session_id: String(params.session_id), job_id: "context-job", status: "started" };
  };
  const block = {
    type: "selection",
    path: "src/app.ts",
    content: "const answer = 42;"
  };
  const controller = controllerForBridge(bridge, undefined, undefined, {
    collect: async () => ({
      blocks: [block],
      skipped: [],
      totalBytes: 64,
      truncated: false
    })
  });

  assert.equal(await controller.runTaskWithInstruction("explain this", "readonly"), true);
  assert.deepEqual(runStartParams?.context_blocks, [block]);
});

test("ChatController binds attached diagnostic identities locally and never sends them over JSONL", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const fingerprint = "a".repeat(64);
  let runStartParams: Record<string, unknown> | undefined;
  let requiredFingerprints: readonly string[] | undefined;
  bridge.startRun = async (params: Record<string, unknown>) => {
    runStartParams = params;
    return { session_id: String(params.session_id), job_id: "diagnostic-job", status: "started" };
  };
  const controller = controllerForBridge(
    bridge,
    undefined,
    undefined,
    {
      collect: async () => ({
        blocks: [{
          type: "diagnostics",
          items: [{ uri: "file:///workspace/project/src/app.ts", message: "Fix me", verification_id: fingerprint }]
        }],
        skipped: [],
        totalBytes: 128,
        truncated: false
      })
    },
    {
      captureBaseline: (required?: readonly string[]) => {
        requiredFingerprints = required;
        return { schemaVersion: 1 } as any;
      },
      verifyAfterChanges: async () => ({}) as any
    }
  );

  assert.equal(await controller.runTaskWithInstruction("fix the attached problem", "readonly"), true);
  assert.deepEqual(requiredFingerprints, [fingerprint]);
  assert.equal(JSON.stringify(runStartParams?.context_blocks).includes("verification_id"), false);
  assert.equal(JSON.stringify(runStartParams?.context_blocks).includes(fingerprint), false);
});

test("rapid first-message submits start only one run and add one user item", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let startCalls = 0;
  let releaseStart: (() => void) | undefined;
  const startGate = new Promise<void>((resolve) => {
    releaseStart = resolve;
  });
  bridge.startRun = async (params: Record<string, unknown>) => {
    startCalls += 1;
    await startGate;
    return { session_id: String(params.session_id), job_id: "only-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const first = controller.submitPrimaryMessage("first task", "review");
  for (let attempt = 0; attempt < 20 && startCalls === 0; attempt += 1) {
    await delay(0);
  }
  const second = await controller.submitPrimaryMessage("duplicate task", "review");

  assert.equal(second, false);
  assert.equal(startCalls, 1);
  assert.equal(controller.conversationState().items.filter((item) => item.kind === "user").length, 1);
  assert.equal(controller.conversationState().items.some((item) => item.text.includes("already starting")), true);
  releaseStart?.();
  assert.equal(await first, true);
  await controller.shutdown({ shutdownBridge: false });
});

test("New Session allows a newer run while a stale run.start response is pending", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let startCalls = 0;
  let createCalls = 0;
  bridge.createSession = async (params: Record<string, unknown>) => {
    createCalls += 1;
    return {
      session_id: createCalls === 1 ? "stale-session" : "current-session",
      workspace_root: String(params.workspace),
      mode: String(params.mode) as "readonly" | "review" | "auto"
    };
  };
  let releaseFirst: (() => void) | undefined;
  const firstGate = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  bridge.startRun = async (params: Record<string, unknown>) => {
    startCalls += 1;
    if (startCalls === 1) {
      await firstGate;
      return { session_id: String(params.session_id), job_id: "stale-job", status: "started" };
    }
    return { session_id: String(params.session_id), job_id: "current-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const stale = controller.submitPrimaryMessage("stale task", "review");
  for (let attempt = 0; attempt < 20 && startCalls === 0; attempt += 1) {
    await delay(0);
  }
  await controller.newSession();
  assert.equal(await controller.submitPrimaryMessage("current task", "review"), true);
  releaseFirst?.();

  assert.equal(await stale, false);
  assert.equal(controller.testState().sessionId, "current-session");
  assert.deepEqual(bridge.cancelSessionCalls, ["stale-session"]);
  assert.deepEqual(
    controller.conversationState().items.filter((item) => item.kind === "user").map((item) => item.text),
    ["current task"]
  );
  await controller.shutdown({ shutdownBridge: false });
});

test("an active job accepts a durable queued follow-up", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let sendCalls = 0;
  bridge.sendChat = async (sessionId: string) => {
    sendCalls += 1;
    return { session_id: sessionId, job_id: "queued-job", status: "queued" };
  };
  const controller = controllerForBridge(bridge);

  assert.equal(await controller.submitPrimaryMessage("first task", "review"), true);
  assert.equal(await controller.submitPrimaryMessage("queued follow-up", "review"), true);

  assert.equal(sendCalls, 1);
  assert.deepEqual(
    controller.conversationState().items.filter((item) => item.kind === "user").map((item) => item.text),
    ["first task", "queued follow-up"]
  );
  assert.equal(controller.conversationState().items.some((item) => item.text.includes("Follow-up queued")), true);
  await controller.shutdown({ shutdownBridge: false });
});

test("primary sidebar keeps follow-up messages in one session without opening Details", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const sent: string[] = [];
  bridge.startRun = async (params: Record<string, unknown>) => ({
    session_id: String(params.session_id),
    job_id: "sidebar-job-1",
    status: "started"
  });
  (bridge as any).sendChat = async (sessionId: string, message: string) => {
    sent.push(message);
    return { session_id: sessionId, job_id: "sidebar-job-2", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  assert.equal(await controller.submitPrimaryMessage("first task", "review"), true);
  // Simulate the poller observing the first job's terminal state before the follow-up.
  (controller as any).activeJobId = undefined;
  assert.equal(await controller.submitPrimaryMessage("one follow-up", "review"), true);

  assert.deepEqual(sent, ["one follow-up"]);
  assert.equal(controller.conversationState().items.filter((item) => item.kind === "user").length, 2);
});

test("rapid follow-up submits start only one chat job and add one user item", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  assert.equal(await controller.ensureLiveSession(), "chat-session");

  let sendCalls = 0;
  let releaseSend: (() => void) | undefined;
  const sendGate = new Promise<void>((resolve) => {
    releaseSend = resolve;
  });
  bridge.sendChat = async (sessionId: string) => {
    sendCalls += 1;
    await sendGate;
    return { session_id: sessionId, job_id: "only-follow-up-job", status: "started" };
  };

  const first = controller.submitPrimaryMessage("first follow-up", "readonly");
  for (let attempt = 0; attempt < 20 && sendCalls === 0; attempt += 1) {
    await delay(0);
  }
  const second = await controller.submitPrimaryMessage("duplicate follow-up", "readonly");

  assert.equal(second, false);
  assert.equal(sendCalls, 1);
  assert.deepEqual(
    controller.conversationState().items.filter((item) => item.kind === "user").map((item) => item.text),
    ["first follow-up"]
  );
  assert.equal(controller.conversationState().items.some((item) => item.text.includes("already sending")), true);
  releaseSend?.();
  assert.equal(await first, true);
  await controller.shutdown({ shutdownBridge: false });
});

test("a rejected follow-up releases the send guard for a retry", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  assert.equal(await controller.ensureLiveSession(), "chat-session");

  let sendCalls = 0;
  bridge.sendChat = async (sessionId: string) => {
    sendCalls += 1;
    if (sendCalls === 1) {
      throw new Error("temporary send failure");
    }
    return { session_id: sessionId, job_id: "retry-job", status: "started" };
  };

  assert.equal(await controller.submitPrimaryMessage("failed follow-up", "readonly"), false);
  assert.equal(await controller.submitPrimaryMessage("retry follow-up", "readonly"), true);
  assert.equal(sendCalls, 2);
  assert.equal(controller.testState().activeJobId, "retry-job");
  await controller.shutdown({ shutdownBridge: false });
});

test("New Session gives a newer follow-up its own send generation", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let createCalls = 0;
  bridge.createSession = async () => {
    createCalls += 1;
    return {
      session_id: `generation-session-${createCalls}`,
      workspace_root: "/workspace/project",
      mode: "readonly"
    };
  };
  const controller = controllerForBridge(bridge);
  assert.equal(await controller.ensureLiveSession(), "generation-session-1");

  let releaseStale: (() => void) | undefined;
  const staleGate = new Promise<void>((resolve) => {
    releaseStale = resolve;
  });
  let releaseCurrent: (() => void) | undefined;
  const currentGate = new Promise<void>((resolve) => {
    releaseCurrent = resolve;
  });
  const sent: string[] = [];
  bridge.sendChat = async (sessionId: string, message: string) => {
    sent.push(`${sessionId}:${message}`);
    if (sessionId === "generation-session-1") {
      await staleGate;
    } else {
      await currentGate;
    }
    return { session_id: sessionId, job_id: `${sessionId}-job`, status: "started" };
  };

  const stale = (controller as any).sendUserMessage("stale follow-up", false, false) as Promise<boolean>;
  for (let attempt = 0; attempt < 20 && sent.length === 0; attempt += 1) {
    await delay(0);
  }
  await controller.newSession();
  const current = (controller as any).sendUserMessage("current follow-up", false, false) as Promise<boolean>;
  for (let attempt = 0; attempt < 20 && sent.length < 2; attempt += 1) {
    await delay(0);
  }
  releaseStale?.();
  assert.equal(await stale, false);

  // The stale operation's finally ran while the current send was still pending. It must not clear
  // the current generation's guard and let a third message through.
  assert.equal(await (controller as any).sendUserMessage("third follow-up", false, false), false);
  assert.equal(sent.length, 2);
  releaseCurrent?.();
  assert.equal(await current, true);
  assert.deepEqual(sent, [
    "generation-session-1:stale follow-up",
    "generation-session-2:current follow-up"
  ]);
  assert.equal(controller.testState().sessionId, "generation-session-2");
  assert.equal(controller.testState().activeJobId, "generation-session-2-job");
  await controller.shutdown({ shutdownBridge: false });
});

test("primary sidebar routes a slash command on the first message without starting an AI run", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let startRunCalls = 0;
  bridge.startRun = async () => {
    startRunCalls += 1;
    return { session_id: "unexpected-session", job_id: "unexpected-job", status: "started" };
  };
  const routed: string[] = [];
  const controller = controllerForBridge(bridge);
  controller.setSlashCommandRouter({
    execute: async (input: string) => {
      routed.push(input);
      return { handled: true, command: input, notice: "Help opened.", severity: "info" };
    }
  } as any);

  assert.equal(await controller.submitPrimaryMessage("/help", "review"), true);
  assert.deepEqual(routed, ["/help"]);
  assert.equal(startRunCalls, 0);
});

test("ChatController Start Here task reports a blocked start without calling run.start", async () => {
  workspaceTrusted = false;
  const bridge = bridgeMock();
  let runStartCalls = 0;
  bridge.startRun = async () => {
    runStartCalls += 1;
    return { session_id: "blocked-session", job_id: "blocked-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  const started = await controller.runTaskWithInstruction("change a file", "review");

  assert.equal(started, false);
  assert.equal(runStartCalls, 0);
});

test("a run.start config error becomes a visible sidebar error and ends startup", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.startRun = async () => {
    throw new Error("Subscription is not configured (config_error).");
  };
  const controller = controllerForBridge(bridge);

  assert.equal(await controller.submitPrimaryMessage("explain this project", "readonly"), false);

  const state = controller.conversationState();
  assert.equal(state.jobId, null);
  assert.equal(state.items.some((item) => item.kind === "error" && item.text.includes("config_error")), true);
});

test("a session.create config error becomes a visible sidebar error after reload", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.createSession = async () => {
    throw new Error("Provider subscription is missing (config_error).");
  };
  const controller = controllerForBridge(bridge, undefined, {
    get: () => "retained-session",
    update: async () => undefined
  });

  assert.equal(await controller.submitPrimaryMessage("continue", "readonly"), false);

  const state = controller.conversationState();
  assert.equal(state.jobId, null);
  assert.equal(state.items.some((item) => item.kind === "error" && item.text.includes("config_error")), true);
});

test("slash command arguments are redacted from runtime and transcript state", async () => {
  workspaceTrusted = true;
  const secret = "abcdefghijklmnop1234";
  const bridge = bridgeMock();
  const runtime = new CockpitRuntimeState();
  const controller = controllerForBridge(bridge, runtime);
  controller.setSlashCommandRouter({
    parse: (input: string) => ({ id: "config", raw: input, command: "/config", args: input }),
    execute: async (input: string) => ({ handled: true, command: input, notice: `token=${secret}` })
  } as any);

  await (controller as any).sendUserMessage(`/config set token=${secret}`);

  const rendered = JSON.stringify({ state: controller.testState(), runtime: runtime.snapshot() });
  assert.equal(rendered.includes(secret), false);
  assert.equal(rendered.includes("<redacted>"), true);
});

test("a rejected cockpit action is caught and redacted before it reaches the transcript", async () => {
  workspaceTrusted = true;
  const secret = "abcdefghijklmnop1234";
  const controller = controllerForBridge(bridgeMock());
  controller.setCockpitActions({
    cancel: async () => { throw new Error(`Authorization: Bearer ${secret}`); }
  });
  await controller.openChat();

  await assert.rejects(() => controller.handlePrimaryCockpitAction({ type: "cancel" }));
  controller.refreshCockpit();
  await delay(10);

  const rendered = JSON.stringify(controller.conversationState());
  assert.equal(rendered.includes(secret), false);
});

test("bridge and persistence errors never expose secrets to status, runtime, transcript, or output", async () => {
  workspaceTrusted = true;
  const bridgeSecret = "abcdefghijklmnop1234";
  const persistenceSecret = "sk-abcdefghijklmnop";
  const bridge = bridgeMock();
  bridge.sendChat = async () => {
    throw new Error(`Authorization: Bearer ${bridgeSecret}`);
  };
  const runtime = new CockpitRuntimeState();
  const controller = controllerForBridge(bridge, runtime, {
    get: () => undefined,
    update: () => { throw new Error(`api_key=${persistenceSecret}`); }
  });
  const statusErrors: string[] = [];
  const outputLines: string[] = [];
  (controller as any).statusBar.setError = (message: string) => statusErrors.push(message);
  (controller as any).output.appendLine = (message: string) => outputLines.push(message);

  await controller.testSubmit("hello");
  await controller.shutdown({ shutdownBridge: false });

  const rendered = JSON.stringify({
    state: controller.testState(),
    runtime: runtime.snapshot(),
    statusErrors,
    outputLines
  });
  assert.equal(rendered.includes(bridgeSecret), false);
  assert.equal(rendered.includes(persistenceSecret), false);
  assert.equal(rendered.includes("<redacted>"), true);
});

test("ChatController fails unknown slash commands closed when no router is attached", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let sendChatCalls = 0;
  bridge.sendChat = async () => {
    sendChatCalls += 1;
    return { session_id: "chat-session", job_id: "chat-job", status: "started" };
  };
  const controller = controllerForBridge(bridge);

  await (controller as any).sendUserMessage("/unknown");

  assert.equal(sendChatCalls, 0);
  assert.equal(controller.testState().items.some((item) => item.text.includes("Slash commands are unavailable")), true);
});

test("ChatController webview cancel routes through central cockpit cancel action", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  let centralCancelCalls = 0;
  controller.setCockpitActions({
    cancel: async () => {
      centralCancelCalls += 1;
    }
  });

  await controller.handlePrimaryCockpitAction({ type: "cancel" });

  assert.equal(centralCancelCalls, 1);
  assert.equal(bridge.cancelSessionCalls.length, 0);
});

test("ChatController does not request active cancellation without backend capability", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  infos.length = 0;
  const bridge = bridgeMock();
  bridge.supportsActiveJobCancellationFeature = false;
  const controller = controllerForBridge(bridge);

  await controller.testSubmit("hello");
  await controller.cancelCurrentRun();
  await controller.cancelCurrentRun();

  assert.equal(controller.testState().activeJobId, "chat-job");
  assert.deepEqual(bridge.cancelSessionCalls, []);
  assert.equal(
    controller.testState().items.some((item) => item.text.includes("does not advertise cooperative active-job cancellation")),
    true
  );
  assert.equal(
    warnings.some((message) => message.includes("does not advertise cooperative active-job cancellation")),
    true
  );
  assert.equal(
    warnings.some((message) => message.includes("already reported as unsupported or non-cancellable")),
    true
  );
  await controller.shutdown({ shutdownBridge: false });
});

test("ChatController suppresses repeated backend calls for non-cancellable jobs", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  infos.length = 0;
  const bridge = bridgeMock();
  bridge.cancelSessionResult = { status: "non_cancellable" };
  const controller = controllerForBridge(bridge);

  await controller.testSubmit("hello");
  await controller.cancelCurrentRun();
  await controller.cancelCurrentRun();

  assert.equal(controller.testState().activeJobId, "chat-job");
  assert.deepEqual(bridge.cancelSessionCalls, ["chat-session"]);
  assert.equal(
    controller.testState().items.some((item) => item.text.includes("cannot be cancelled by the backend")),
    true
  );
  assert.equal(
    warnings.some((message) => message.includes("already reported as unsupported or non-cancellable")),
    true
  );
  await controller.shutdown({ shutdownBridge: false });
});

test("ChatController routes Cockpit setup, output, and restart actions", async () => {
  workspaceTrusted = true;
  vscodeStub.commands.executed.length = 0;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  let outputShown = 0;
  (controller as any).output.show = () => {
    outputShown += 1;
  };

  await controller.handlePrimaryCockpitAction({ type: "command.openSetupGuide" });
  await controller.handlePrimaryCockpitAction({ type: "command.openOutput" });
  await controller.handlePrimaryCockpitAction({ type: "cockpit.retryStatus" });
  await controller.handlePrimaryCockpitAction({ type: "bridge.restart" });

  assert.deepEqual(vscodeStub.commands.executed, ["alysis.openSetupGuide", "alysis.showBridgeHealth"]);
  assert.equal(outputShown, 1);
  assert.equal(controller.testState().items.some((item) => item.text.includes("IDE bridge restarted")), true);
});

test("FE-10: cockpit restart re-reads the live cliPath and re-runs detection (no window reload)", async () => {
  workspaceTrusted = true;
  let cliPath = "alysis";
  const restartConfigs: string[] = [];
  const bridge = bridgeMock();
  // Capture the config the restart receives so we can prove it re-reads a freshly-set cliPath.
  (bridge as any).restartIfSafe = async (config: any) => {
    restartConfigs.push(config.cliPath);
    return true;
  };
  const controller = new ChatController(
    bridge as any,
    () => ({
      cliPath, // live getConfig — re-read on every call, so a newly-set path is visible immediately
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
        cliPath: { value: cliPath, source: "global", trusted: true, executionAllowed: true, apiKeyForwardingAllowed: true }
      }
    }),
    { getApiKey: async () => "secret" } as any,
    { setError: () => undefined, setActiveRun: () => undefined, setBridgeOk: () => undefined, setIdle: () => undefined, setApprovalNeeded: () => undefined } as any,
    { setSessions: () => undefined, setActiveSession: () => undefined, rememberSession: () => undefined } as any,
    { appendLine: () => undefined } as any,
    undefined
  );
  let reconnects = 0;
  controller.setCockpitActions({
    cancel: async () => undefined,
    reconnect: async () => {
      reconnects += 1;
    }
  });

  // The user fixes the path via Locate CLI, then hits Restart Bridge.
  cliPath = "/opt/alysis/bin/alysis";
  await controller.handlePrimaryCockpitAction({ type: "bridge.restart" });

  assert.deepEqual(restartConfigs, ["/opt/alysis/bin/alysis"], "restart spawns with the freshly-set cliPath");
  assert.equal(reconnects, 1, "restart re-runs health detection so stale recovery cards clear without a reload");
});

test("ChatController declines fullaccess mode.set as a reserved (not-yet-wired) mode", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);

  await controller.handlePrimaryCockpitAction({ type: "mode.set", mode: "fullaccess" });

  assert.equal(
    controller.testState().items.some((item) => item.text.includes("Fullaccess mode is advertised")),
    true
  );
});

test("ChatController routes readonly/review/auto mode.set through the slash router", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  const routed: string[] = [];
  controller.setSlashCommandRouter({
    execute: async (input: string) => {
      routed.push(input);
      return { handled: true, command: input, notice: "Alysis Code default mode set to auto. New sessions will use this mode.", severity: "info" };
    }
  } as any);

  await controller.handlePrimaryCockpitAction({ type: "mode.set", mode: "auto" });

  assert.deepEqual(routed, ["/permissions auto"]);
  // With no session open, the composer switch itself is the feedback: the start surface must not
  // gain a conversation whose only content is "default mode set to …".
  assert.equal(controller.testState().items.some((item) => item.text.includes("default mode set to auto")), false);
  assert.equal(controller.testState().items.length, 0);
});

test("ChatController still cards a mode.set warning when no session is open", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  controller.setSlashCommandRouter({
    execute: async (input: string) => ({
      handled: true,
      command: input,
      notice: "Workspace Trust is required before switching Alysis Code to auto mode.",
      severity: "warning"
    })
  } as any);

  await controller.handlePrimaryCockpitAction({ type: "mode.set", mode: "auto" });

  assert.equal(controller.testState().items.some((item) => item.text.includes("Workspace Trust is required")), true);
});

test("ChatController shutdown denies only chat-owned pending approvals", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  await controller.openChat();
  (controller as any).sessionId = "chat-session";
  (controller as any).activeJobId = "chat-job";
  (controller as any).ownedSessionIds.add("chat-session");
  (controller as any).ownedJobIds.add("chat-job");

  bridge.emitEvent({
    protocol_version: "1",
    session_id: "chat-session",
    run_id: null,
    job_id: "chat-job",
    sequence: 3,
    timestamp: "2026-05-26T00:00:00.000Z",
    type: "prompt_for_input",
    payload: {
      kind: "approval",
      approval_kind: "shell_run",
      approval_id: "chat-approval",
      prompt_id: "chat-approval",
      reason: "Chat approval",
      preview: "npm test"
    }
  });
  bridge.emitEvent({
    protocol_version: "1",
    session_id: "other-session",
    run_id: null,
    job_id: "other-job",
    sequence: 4,
    timestamp: "2026-05-26T00:00:00.000Z",
    type: "prompt_for_input",
    payload: {
      kind: "approval",
      approval_kind: "fs_write",
      approval_id: "other-approval",
      prompt_id: "other-approval",
      reason: "Approval owned by another session",
      preview: "write file"
    }
  });

  await controller.shutdown({ shutdownBridge: false });

  assert.deepEqual(bridge.respondApprovalCalls, [
    {
      session_id: "chat-session",
      approval_id: "chat-approval",
      allow: false,
      allow_for_session: false
    }
  ]);
});

test("ChatController shutdown is idempotent and detaches bridge listeners", async () => {
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  assert.equal(bridge.listenerCount("event"), 1);
  assert.equal(bridge.listenerCount("exit"), 1);

  const first = controller.shutdown();
  const second = controller.shutdown();

  assert.equal(first, second);
  await first;
  assert.equal(bridge.shutdownCalls.length, 1);
  assert.equal(bridge.listenerCount("event"), 0);
  assert.equal(bridge.listenerCount("stderr"), 0);
  assert.equal(bridge.listenerCount("error"), 0);
  assert.equal(bridge.listenerCount("exit"), 0);
  assert.equal(bridge.listenerCount("reset"), 0);
});

test("ChatController retries transient job.status failures and still observes terminal state", { timeout: 8_000 }, async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let statusCalls = 0;
  bridge.jobStatus = async () => {
    statusCalls += 1;
    if (statusCalls <= 2) {
      throw new Error("temporary bridge read failure");
    }
    return { job_id: "chat-job", session_id: "chat-session", status: "completed" };
  };
  const controller = controllerForBridge(bridge);
  (controller as any).sessionId = "chat-session";
  (controller as any).activeJobId = "chat-job";
  (controller as any).trackedJobIds.add("chat-job");
  (controller as any).ownedJobIds.add("chat-job");

  await (controller as any).pollJob("chat-job");

  assert.equal(statusCalls, 3);
  assert.equal((controller as any).activeJobId, undefined);
  assert.equal((controller as any).trackedJobIds.has("chat-job"), false);
  await controller.shutdown({ shutdownBridge: false });
});

test("ChatController releases a completed job before waiting for task history", { timeout: 5_000 }, async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.jobStatus = async () => ({ job_id: "chat-job", session_id: "chat-session", status: "completed" });
  const controller = controllerForBridge(bridge);
  (controller as any).sessionId = "chat-session";
  (controller as any).activeJobId = "chat-job";
  (controller as any).trackedJobIds.add("chat-job");
  (controller as any).ownedJobIds.add("chat-job");
  let releaseHistory!: () => void;
  const historyWait = new Promise<void>((resolve) => { releaseHistory = resolve; });
  let historyStarted!: () => void;
  const historyReached = new Promise<void>((resolve) => { historyStarted = resolve; });
  (controller as any).refreshSessionsView = async () => {
    historyStarted();
    await historyWait;
  };
  const polling = (controller as any).pollJob("chat-job");
  try {
    await historyReached;
    assert.equal((controller as any).activeJobId, undefined);
    assert.equal((controller as any).trackedJobIds.has("chat-job"), false);
    assert.equal(controller.primaryCockpitState().jobStatus, "completed");
  } finally {
    releaseHistory();
    await polling;
    await controller.shutdown({ shutdownBridge: false });
  }
});

test("opening chat after reload restores retained dialogue without submitting a new prompt", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.supportedMethods.add("session.resume");
  let submitted = false;
  bridge.sendChat = async () => { submitted = true; throw new Error("Must not send a prompt"); };
  (bridge as any).getEvents = async () => ({ events: [
    protocolEvent(1, "message_delta", { text: "Saved answer" }),
    protocolEvent(2, "message_end", { text: "Saved answer" })
  ], truncated: false });
  const controller = controllerForBridge(bridge, undefined, {
    get: () => "saved-task", update: () => undefined
  });
  await controller.openChat();
  assert.equal(submitted, false);
  assert.deepEqual(bridge.sessionResumeCalls, [{ sessionId: "chat-session", targetSessionId: "saved-task" }]);
  assert.ok(controller.conversationState().items.some((item) => item.text === "Saved answer"));
  await controller.openChat();
  assert.equal(bridge.sessionResumeCalls.length, 1);
});

test("opening retained chat in an untrusted workspace does not start a process", async () => {
  workspaceTrusted = false;
  const bridge = bridgeMock();
  bridge.supportedMethods.add("session.resume");
  bridge.createSession = async () => { throw new Error("Untrusted process start"); };
  const controller = controllerForBridge(bridge, undefined, {
    get: () => "saved-task", update: () => undefined
  });
  await controller.openChat();
  assert.deepEqual(bridge.sessionResumeCalls, []);
  assert.equal(controller.testState().sessionId, null);
});

test("explicit retained resume hydrates the visible conversation and serializes concurrent actions", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  await controller.ensureLiveSession();
  let release: (() => void) | undefined;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  (bridge as any).sessionResume = async (_live: string, _target: string, options: unknown) => {
    assert.deepEqual(options, { emitHistory: true });
    await waiting;
    return { session_id: "chat-session", resumed_session_id: "old-task", resumed: true };
  };
  (bridge as any).getEvents = async () => ({ events: [
    protocolEvent(1, "message_end", { text: "A retained answer", role: "assistant" })
  ], truncated: false });
  const restoring = controller.resumeRetainedContext("chat-session", "old-task");
  assert.equal(controller.workspaceTransitionBlocked(), true);
  assert.equal(await controller.submitPrimaryMessage("Must wait", "readonly"), false);
  release?.();
  await restoring;
  assert.equal(controller.workspaceTransitionBlocked(), false);
  assert.ok(controller.conversationState().items.some((item) => item.text === "A retained answer"));
  await assert.rejects(controller.resumeRetainedContext("superseded-session", "old-task"), /active conversation changed/);
});

test("explicit retained resume refuses to merge history while a task is running", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  await controller.submitPrimaryMessage("Active request", "readonly");
  await assert.rejects(controller.resumeRetainedContext("chat-session", "old-task"), /Finish or stop/);
  assert.deepEqual(bridge.sessionResumeCalls, []);
});

test("ChatController stops stale active state after the bounded job.status retry budget", { timeout: 10_000 }, async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let statusCalls = 0;
  bridge.jobStatus = async () => {
    statusCalls += 1;
    throw new Error("persistent bridge read failure");
  };
  const controller = controllerForBridge(bridge);
  (controller as any).sessionId = "chat-session";
  (controller as any).activeJobId = "chat-job";
  (controller as any).trackedJobIds.add("chat-job");
  (controller as any).ownedJobIds.add("chat-job");

  await (controller as any).pollJob("chat-job");

  assert.equal(statusCalls, 4);
  assert.equal((controller as any).activeJobId, undefined);
  assert.equal((controller as any).trackedJobIds.has("chat-job"), false);
  assert.equal(controller.primaryCockpitState().jobStatus, "status_unknown");
  await controller.shutdown({ shutdownBridge: false });
});

test("session.resume immediately re-adopts recovered active and queued chat work", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.supportedMethods.add("session.resume");
  bridge.supportedMethods.add("chat.queue.list");
  bridge.sessionResume = async (sessionId: string, targetSessionId: string) => {
    bridge.sessionResumeCalls.push({ sessionId, targetSessionId });
    return {
      session_id: sessionId,
      resumed_session_id: targetSessionId,
      resumed: true,
      queued_prompts_rebound: 1,
      active_prompts_observed: 1
    };
  };
  (bridge as any).chatQueueList = async (sessionId: string) => ({
    session_id: sessionId,
    next_sequence: 9,
    items: [
      {
        session_id: sessionId,
        prompt_id: "recovered-running",
        sequence: 7,
        state: "running" as const,
        created_at: "2026-07-30T10:00:00Z",
        updated_at: "2026-07-30T10:00:01Z",
        attempts: 1
      },
      {
        session_id: sessionId,
        prompt_id: "recovered-pending",
        sequence: 8,
        state: "pending" as const,
        created_at: "2026-07-30T10:00:02Z",
        updated_at: "2026-07-30T10:00:02Z",
        attempts: 0
      }
    ]
  });
  const controller = controllerForBridge(bridge, undefined, {
    get: () => ({
      sessionId: "retained-session",
      workspace: { root: "/workspace/project", scheme: "file", authority: "" }
    }),
    update: async () => undefined
  });

  assert.equal(await controller.ensureLiveSession(), "chat-session");
  assert.deepEqual(bridge.sessionResumeCalls, [
    { sessionId: "chat-session", targetSessionId: "retained-session" }
  ]);
  assert.equal(controller.testState().activeJobId, "recovered-running");
  assert.equal(controller.primaryCockpitState().jobStatus, "running");
  assert.equal((controller as any).trackedJobIds.has("recovered-running"), true);
  assert.equal((controller as any).trackedJobIds.has("recovered-pending"), true);
  assert.equal((controller as any).queuedJobIds.has("recovered-pending"), true);
  await controller.shutdown({ shutdownBridge: false });
});

test("a chat session remains pinned to its original remote workspace across focus changes", async () => {
  workspaceTrusted = true;
  const rootA = { uri: { scheme: "vscode-remote", authority: "ssh-remote+a", fsPath: "/work/a" } };
  const rootB = { uri: { scheme: "vscode-remote", authority: "ssh-remote+b", fsPath: "/work/b" } };
  vscodeStub.workspace.workspaceFolders = [rootA, rootB];
  activeTextEditor = { document: { uri: { ...rootA.uri, fsPath: "/work/a/src/a.ts" } } };
  const bridge = bridgeMock();
  let startWorkspace = "";
  const sends: Array<{ sessionId: string; message: string }> = [];
  // The workspace is pinned at session.create; run.start joins that session with turn params only.
  const originalCreateSession = bridge.createSession;
  bridge.createSession = async (params: Record<string, unknown>) => {
    startWorkspace = String(params.workspace);
    return originalCreateSession(params);
  };
  bridge.startRun = async (params: Record<string, unknown>) => {
    assert.equal("workspace" in params, false, "run.start on an existing session must not repeat the workspace");
    return { session_id: String(params.session_id), job_id: "pinned-active", status: "started" };
  };
  bridge.sendChat = async (sessionId: string, message: string) => {
    sends.push({ sessionId, message });
    return { session_id: sessionId, job_id: "pinned-queued", status: "queued" };
  };
  const contextRoots: Array<string | undefined> = [];
  const persisted: Array<ChatSessionReference | undefined> = [];
  const controller = controllerForBridge(
    bridge,
    undefined,
    { get: () => undefined, update: (reference) => { persisted.push(reference); } },
    {
      collect: async (request?: { workspaceRoot?: string }) => {
        contextRoots.push(request?.workspaceRoot);
        return { blocks: [], skipped: [], totalBytes: 0, truncated: false };
      }
    }
  );

  assert.equal(await controller.submitPrimaryMessage("start in A", "readonly"), true);
  activeTextEditor = { document: { uri: { ...rootB.uri, fsPath: "/work/b/src/b.ts" } } };
  assert.equal(await controller.submitPrimaryMessage("follow up from B", "readonly"), true);

  assert.equal(startWorkspace, "/work/a");
  assert.deepEqual(sends, [{ sessionId: "chat-session", message: "follow up from B" }]);
  assert.deepEqual(contextRoots, ["/work/a", "/work/a"]);
  assert.equal(controller.activeSessionWorkspaceRoot(), "/work/a");
  assert.deepEqual(persisted.at(-1), {
    sessionId: "chat-session",
    workspace: { root: "/work/a", scheme: "vscode-remote", authority: "ssh-remote+a" },
    mode: "readonly"
  });
  await controller.shutdown({ shutdownBridge: false });
});

test("a persisted multi-root session resumes under its original authority, not the focused folder", async () => {
  workspaceTrusted = true;
  const rootA = { uri: { scheme: "vscode-remote", authority: "ssh-remote+a", fsPath: "/work/a" } };
  const rootB = { uri: { scheme: "vscode-remote", authority: "ssh-remote+b", fsPath: "/work/b" } };
  vscodeStub.workspace.workspaceFolders = [rootA, rootB];
  activeTextEditor = { document: { uri: { ...rootB.uri, fsPath: "/work/b/src/b.ts" } } };
  const bridge = bridgeMock();
  bridge.supportedMethods.add("session.resume");
  let createWorkspace = "";
  (bridge as any).createSession = async (params: Record<string, unknown>) => {
    createWorkspace = String(params.workspace);
    return { session_id: "live-session", workspace_root: createWorkspace, mode: "readonly" };
  };
  const controller = controllerForBridge(bridge, undefined, {
    get: () => ({
      sessionId: "retained-a",
      workspace: { root: "/work/a", scheme: "vscode-remote", authority: "ssh-remote+a" }
    }),
    update: async () => undefined
  });

  assert.equal(await controller.ensureLiveSession(), "live-session");
  assert.equal(createWorkspace, "/work/a");
  assert.deepEqual(bridge.sessionResumeCalls, [{ sessionId: "live-session", targetSessionId: "retained-a" }]);
  await controller.shutdown({ shutdownBridge: false });
});

test("an older polling failure cannot replace a newer active job's global UI", { timeout: 10_000 }, async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.jobStatus = async () => {
    throw new Error("old queue lease disappeared");
  };
  const controller = controllerForBridge(bridge);
  (controller as any).sessionId = "chat-session";
  (controller as any).activeJobId = "new-active";
  (controller as any).trackedJobIds.add("old-queued");
  (controller as any).trackedJobIds.add("new-active");
  (controller as any).ownedJobIds.add("old-queued");
  (controller as any).transcript.setJob("new-active", "running");

  await (controller as any).pollJob("old-queued");

  assert.equal((controller as any).activeJobId, "new-active");
  assert.equal(controller.primaryCockpitState().jobStatus, "running");
  assert.equal((controller as any).trackedJobIds.has("new-active"), true);
  assert.equal((controller as any).trackedJobIds.has("old-queued"), false);
  await controller.shutdown({ shutdownBridge: false });
});

test("host-side provider readiness blocks Details sends before session or transcript side effects", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  const bridge = bridgeMock();
  let createCalls = 0;
  bridge.createSession = async () => {
    createCalls += 1;
    return { session_id: "should-not-start", workspace_root: "/workspace/project", mode: "readonly" };
  };
  const controller = controllerForBridge(bridge);
  controller.setProviderReadiness(() => ({
    status: "incomplete",
    reason: "Choose a subscription model and reasoning effort before sending."
  }));

  await controller.testSubmit("must not send");

  assert.equal(createCalls, 0);
  assert.deepEqual(controller.testState().items, []);
  assert.equal(warnings.some((message) => /reasoning effort/.test(message)), true);
  await controller.shutdown({ shutdownBridge: false });
});

test("worktree handoff blocks sends, preserves the source on failure, and closes the persisted destination", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.supportedMethods.add("session.fork");
  let created = 0;
  bridge.createSession = async (params) => ({ session_id: `session-${++created}`, workspace_root: String(params.workspace), mode: "readonly" as const });
  let fail = true;
  Object.assign(bridge, { sessionFork: async () => { if (fail) throw new Error("fork unavailable"); } });
  const controller = controllerForBridge(bridge);
  await controller.ensureLiveSession();
  vscodeStub.workspace.workspaceFolders.push({ uri: { scheme: "file", authority: "", fsPath: "/workspace/worktree" } });
  try {
    await assert.rejects(controller.withWorkspaceTransition(async () => {
      assert.equal(await controller.submitPrimaryMessage("must not send", "readonly"), false);
      await controller.prepareWorktreeHandoff("/workspace/worktree");
    }), /fork unavailable/);
    assert.equal(controller.activeSessionId(), "session-1");
    assert.equal(controller.activeSessionWorkspaceRoot(), "/workspace/project");
    assert.deepEqual(bridge.cancelSessionCalls, ["session-2"]);
    fail = false;
    await controller.withWorkspaceTransition(async () => {
      const reference = await controller.prepareWorktreeHandoff("/workspace/worktree");
      assert.equal(reference.sessionId, "session-3");
      assert.equal(reference.workspace.root, "/workspace/worktree");
    });
    assert.equal(controller.activeSessionId(), "session-1");
    assert.equal(controller.activeSessionWorkspaceRoot(), "/workspace/project");
    assert.ok(bridge.cancelSessionCalls.includes("session-3"));
    assert.ok(!bridge.cancelSessionCalls.includes("session-1"));
  } finally { await controller.shutdown({ shutdownBridge: false }); }
});

for (const savedMode of ["readonly", "review", "auto", undefined] as const) {
  test(`restored conversation retains ${savedMode ?? "legacy read-only"} permissions instead of the global default`, async () => {
    workspaceTrusted = true;
    const bridge = bridgeMock();
    bridge.supportedMethods.add("session.resume");
    const createdModes: unknown[] = [];
    bridge.createSession = async (params) => {
      createdModes.push(params.mode);
      return { session_id: "chat-session", workspace_root: "/workspace/project", mode: String(params.mode) };
    };
    const updates: Array<ChatSessionReference | undefined> = [];
    const controller = controllerForBridge(bridge, undefined, {
      get: () => ({ sessionId: "retained-mode", workspace: { root: "/workspace/project", scheme: "file", authority: "" }, mode: savedMode }),
      update: async (value) => { updates.push(value); }
    });
    const config = (controller as any).getConfig();
    (controller as any).getConfig = () => ({ ...config, defaultMode: savedMode === "auto" ? "review" : "auto" });
    try {
      assert.equal(controller.primaryCockpitState().mode, savedMode ?? "readonly");
      await controller.ensureLiveSession();
      assert.deepEqual(createdModes, [savedMode ?? "readonly"]);
      assert.equal(controller.conversationState().mode, savedMode ?? "readonly");
    } finally { await controller.shutdown({ shutdownBridge: false }); }
    assert.equal(updates.at(-1)?.mode, savedMode ?? "readonly");
  });
}

test("confirmed permission changes persist through bridge reconnect and worktree handoff", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.supportedMethods.add("session.resume");
  bridge.supportedMethods.add("session.fork");
  (bridge as any).sessionFork = async () => ({});
  const modes: unknown[] = [];
  bridge.createSession = async (params) => {
    modes.push(params.mode);
    return { session_id: "chat-session", workspace_root: String(params.workspace), mode: String(params.mode) };
  };
  const updates: Array<ChatSessionReference | undefined> = [];
  const controller = controllerForBridge(bridge, undefined, { get: () => undefined, update: async (value) => { updates.push(value); } });
  const config = (controller as any).getConfig();
  (controller as any).getConfig = () => ({ ...config, defaultMode: "auto" });
  try {
    await controller.ensureLiveSession();
    bridge.emitEvent(protocolEvent(1, "mode_changed", { mode: "readonly" }));
    bridge.emitExit(17);
    await controller.ensureLiveSession();
    assert.deepEqual(modes, ["auto", "readonly"]);
    await controller.withWorkspaceTransition(async () => {
      const reference = await controller.prepareWorktreeHandoff("/workspace/worktree");
      assert.equal(reference.mode, "readonly");
    });
    assert.deepEqual(modes, ["auto", "readonly", "readonly"]);
  } finally { await controller.shutdown({ shutdownBridge: false }); }
  assert.equal(updates.at(-1)?.mode, "readonly");
});

test("permission changes block sends and handoffs until the host confirms", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  await controller.ensureLiveSession();
  let finish!: () => void;
  const confirmation = new Promise<void>((resolve) => { finish = resolve; });
  const change = controller.changePermissions("review", async () => {
    await confirmation;
    bridge.emitEvent(protocolEvent(1, "status_update", { mode: "review" }));
    return "";
  });
  try {
    assert.equal(controller.primaryCockpitState().mode, "readonly");
    assert.equal(await controller.submitPrimaryMessage("do something", "review"), false);
    assert.equal(controller.workspaceTransitionBlocked(), true);
    await assert.rejects(controller.changePermissions("auto", async () => ""), /before changing permissions/);
    await assert.rejects(controller.withWorkspaceTransition(async () => undefined), /before changing worktrees/);
    finish();
    await change;
    assert.equal(controller.primaryCockpitState().mode, "review");
    assert.equal(controller.workspaceTransitionBlocked(), false);
  } finally { finish(); await change; await controller.shutdown({ shutdownBridge: false }); }
});

test("permission changes target a retained conversation and remain unchanged after rejection", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.supportedMethods.add("session.resume");
  const controller = controllerForBridge(bridge, undefined, {
    get: () => ({ sessionId: "retained-mode", mode: "readonly", workspace: { root: "/workspace/project", scheme: "file", authority: "" } }),
    update: async () => undefined
  });
  try {
    await assert.rejects(controller.changePermissions("auto", async () => {
      assert.equal(controller.activeSessionId(), "chat-session");
      assert.deepEqual(bridge.sessionResumeCalls, [{ sessionId: "chat-session", targetSessionId: "retained-mode" }]);
      throw new Error("backend rejected change");
    }), /backend rejected change/);
    assert.equal(controller.primaryCockpitState().mode, "readonly");
    assert.equal(controller.workspaceTransitionBlocked(), false);
    workspaceTrusted = false;
    await assert.rejects(controller.changePermissions("auto", async () => { throw new Error("must not run"); }), /Workspace Trust/);
  } finally { workspaceTrusted = true; await controller.shutdown({ shutdownBridge: false }); }
});

function protocolEvent(sequence: number, type: string, payload: Record<string, unknown>): any {
  return {
    protocol_version: "1.0",
    session_id: "chat-session",
    sequence,
    timestamp: `2026-07-30T10:00:${String(sequence).padStart(2, "0")}Z`,
    type,
    payload
  };
}

test("provider transition keeps an idle conversation and blocks sends during preparation", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerForBridge(bridge);
  const originalSession = await controller.ensureLiveSession();
  let applied: string | undefined;
  await controller.withProviderTransition(async () => {
    assert.equal(controller.workspaceTransitionBlocked(), true);
    assert.equal(await controller.submitPrimaryMessage("do not start during the switch", "readonly"), false);
  }, async sessionId => { applied = sessionId; });
  assert.equal(applied, originalSession);
  assert.equal(controller.workspaceTransitionBlocked(), false);
  controller.dispose();
});

test("provider transition recovers the conversation after a credential restart", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.supportedMethods.add("session.resume");
  const controller = controllerForBridge(bridge);
  const originalSession = await controller.ensureLiveSession();
  let applied: string | undefined;
  await controller.withProviderTransition(async () => {
    bridge.emitReset("credentials_changed");
    bridge.createSession = async () => ({ session_id: "recovered-session", workspace_root: "/workspace/project", mode: "readonly" });
  }, async sessionId => { applied = sessionId; });
  assert.equal(applied, "recovered-session");
  assert.deepEqual(bridge.sessionResumeCalls, [{ sessionId: "recovered-session", targetSessionId: originalSession }]);
  assert.equal(controller.workspaceTransitionBlocked(), false);
  controller.dispose();
});

test("provider transition without a conversation does not create one, and a failure releases its gate", async () => {
  workspaceTrusted = true;
  const controller = controllerForBridge(bridgeMock());
  await assert.rejects(controller.withProviderTransition(async () => undefined, async sessionId => {
    assert.equal(sessionId, undefined);
    throw new Error("provider rejected");
  }), /provider rejected/);
  assert.equal(controller.hasSession(), false);
  assert.equal(controller.workspaceTransitionBlocked(), false);
  controller.dispose();
});

test("provider transition refuses a running turn before credential or default changes", async () => {
  workspaceTrusted = true;
  const controller = controllerForBridge(bridgeMock());
  await controller.submitPrimaryMessage("working", "readonly");
  let prepared = false;
  await assert.rejects(controller.withProviderTransition(async () => { prepared = true; }, async () => undefined), /Finish or stop/);
  assert.equal(prepared, false);
  controller.dispose();
});

function bridgeMock() {
  const listeners = new Map<string | symbol, Array<(...args: any[]) => void>>();
  const bridge = {
    supportsActiveJobCancellationFeature: true,
    cancelSessionResult: { status: "closed" } as Record<string, unknown>,
    cancelSessionCalls: [] as string[],
    cancelSessionOptions: [] as Array<
      { reason?: string; closeWhenIdle?: boolean } | undefined
    >,
    respondApprovalCalls: [] as any[],
    sessionResumeCalls: [] as Array<{ sessionId: string; targetSessionId: string }>,
    shutdownCalls: [] as string[],
    supportedMethods: new Set(["session.cancel"]),
    supportedEvents: new Set<string>(),
    on: (event: string | symbol, listener: (...args: any[]) => void) => {
      const current = listeners.get(event) ?? [];
      current.push(listener);
      listeners.set(event, current);
    },
    off: (event: string | symbol, listener: (...args: any[]) => void) => {
      listeners.set(event, (listeners.get(event) ?? []).filter((candidate) => candidate !== listener));
    },
    listenerCount: (event: string | symbol) => (listeners.get(event) ?? []).length,
    emitEvent: (event: unknown) => {
      for (const listener of listeners.get("event") ?? []) {
        listener(event);
      }
    },
    emitExit: (code: number | null) => {
      for (const listener of listeners.get("exit") ?? []) {
        listener(code);
      }
    },
    emitReset: (reason: string) => {
      for (const listener of listeners.get("reset") ?? []) {
        listener(reason);
      }
    },
    supportsMethod: (method: string) => bridge.supportedMethods.has(method),
    supportsEvent: (eventType: string) => bridge.supportedEvents.has(eventType),
    supportsFeature: (path: readonly string[]) =>
      path.join(".") === "cancellation.active_jobs" && bridge.supportsActiveJobCancellationFeature,
    ensureStarted: async () => undefined,
    restartIfSafe: async () => true,
    createSession: async (_params: Record<string, unknown>) => ({
      session_id: "chat-session",
      workspace_root: "/workspace/project",
      mode: "readonly"
    }),
    sendChat: async (_sessionId: string, _message: string) => ({ session_id: "chat-session", job_id: "chat-job", status: "started" }),
    startRun: async (params: Record<string, unknown>) => ({
      session_id: typeof params.session_id === "string" ? params.session_id : "run-session",
      job_id: "run-job",
      status: "started"
    }),
    sessionResume: async (sessionId: string, targetSessionId: string) => {
      bridge.sessionResumeCalls.push({ sessionId, targetSessionId });
      return { session_id: sessionId, resumed_session_id: targetSessionId, resumed: true };
    },
    chatQueueList: async (sessionId: string) => ({ session_id: sessionId, items: [], next_sequence: 1 }),
    cancelSession: async (
      sessionId: string,
      options?: { reason?: string; closeWhenIdle?: boolean }
    ) => {
      bridge.cancelSessionCalls.push(sessionId);
      bridge.cancelSessionOptions.push(options);
      return bridge.cancelSessionResult;
    },
    respondApproval: async (params: any) => {
      bridge.respondApprovalCalls.push(params);
      return { allow: false, allow_for_session: false, allow_for_session_warning: null };
    },
    jobStatus: async () => ({ job_id: "chat-job", session_id: "chat-session", status: "completed" }),
    sessionList: async () => ({ sessions: [] }),
    getEvents: async () => ({ events: [], truncated: false }),
    artifactList: async () => ({ artifacts: [], truncated: false }),
    artifactRead: async () => ({}),
    shutdown: async (code = "bridge_stopped") => {
      bridge.shutdownCalls.push(code);
    }
  };
  return bridge;
}

function controllerForBridge(
  bridge: ReturnType<typeof bridgeMock>,
  runtime?: CockpitRuntimeState,
  sessionPersistence?: ChatSessionPersistence,
  contextCollector?: {
    collect(request?: any): Promise<any>;
    collectFiles?(uris: readonly any[], workspaceRoot?: string): Promise<any>;
    collectTerminalExcerpt?(selection: any, workspaceRoot?: string): Promise<any>;
  },
  diagnosticsVerification?: {
    captureBaseline(requiredFingerprints?: readonly string[], scope?: { workspaceRoot: string }): any;
    verifyAfterChanges(baseline: any): Promise<any>;
  }
): InstanceType<typeof ChatController> {
  const controller = new ChatController(
    bridge as any,
    () => ({
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
    }),
    { getApiKey: async () => "secret" } as any,
    {
      setError: () => undefined,
      setActiveRun: () => undefined,
      setBridgeOk: () => undefined,
      setIdle: () => undefined,
      setApprovalNeeded: () => undefined
    } as any,
    { setSessions: () => undefined, setActiveSession: () => undefined, rememberSession: () => undefined } as any,
    { appendLine: () => undefined } as any,
    runtime,
    sessionPersistence,
    contextCollector,
    diagnosticsVerification
  );
  if (runtime) {
    controller.setCockpitProvider(() => runtimeCockpit(runtime));
  }
  return controller;
}

function dirtyDocument(scheme: string, fsPath: string, authority = ""): any {
  return {
    isDirty: true,
    uri: { scheme, authority, fsPath, toString: () => `${scheme}://${authority}${fsPath}` }
  };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function runtimeCockpit(runtime: CockpitRuntimeState): any {
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
      runtime: runtime.snapshot()
    }
  };
}
