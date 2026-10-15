import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import Module from "node:module";
import { resolve } from "node:path";
import test from "node:test";

import type { AlysisConfig } from "../src/client/CliDiscovery";

const disposable = { dispose: () => undefined };

class EventEmitter<T = unknown> {
  private readonly listeners = new Set<(value: T) => void>();
  public event = (listener: (value: T) => void) => {
    this.listeners.add(listener);
    return { dispose: () => this.listeners.delete(listener) };
  };
  public fire(value?: T): void {
    for (const listener of [...this.listeners]) {
      listener(value as T);
    }
  }
  public dispose(): void {
    this.listeners.clear();
  }
}

class TreeItem {
  public description?: string | boolean;
  public tooltip?: unknown;
  public contextValue?: string;
  public iconPath?: unknown;
  public command?: unknown;
  public constructor(public label: string, public collapsibleState?: number) {}
}

class ThemeIcon {
  public constructor(public readonly id: string) {}
}

class ThemeColor {
  public constructor(public readonly id: string) {}
}

class Position {
  public constructor(public readonly line: number, public readonly character: number) {}
}

class Range {
  public constructor(public readonly start: unknown, public readonly end: unknown) {}
}

class Diagnostic {
  public source: string | undefined;
  public code: string | number | undefined;
  public constructor(
    public readonly range: unknown,
    public readonly message: string,
    public readonly severity?: number
  ) {}
}

const vscodeStub = {
  EventEmitter,
  TreeItem,
  ThemeIcon,
  ThemeColor,
  Position,
  Range,
  Diagnostic,
  DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  ViewColumn: { One: 1, Beside: -2 },
  QuickPickItemKind: { Separator: -1, Default: 0 },
  ExtensionMode: { Production: 1, Development: 2, Test: 3 },
  ProgressLocation: { Notification: 15 },
  StatusBarAlignment: { Left: 1, Right: 2 },
  ConfigurationTarget: { Global: 1 },
  MarkdownString: class {
    public isTrusted: unknown;
    public constructor(public readonly value: string) {}
  },
  Uri: {
    from: (parts: Record<string, unknown>) => ({ ...parts, toString: () => JSON.stringify(parts) }),
    joinPath: (base: { fsPath: string }, ...segments: string[]) => ({
      fsPath: [base.fsPath, ...segments].join("/")
    })
  },
  env: {},
  languages: { createDiagnosticCollection: () => ({ clear: () => undefined, set: () => undefined, dispose: () => undefined }) },
  workspace: {
    isTrusted: true,
    workspaceFolders: [],
    getConfiguration: () => ({ get: <T>(_section: string, fallback: T) => fallback }),
    registerTextDocumentContentProvider: () => disposable,
    onDidChangeConfiguration: () => disposable,
    onDidGrantWorkspaceTrust: () => disposable,
    onDidChangeWorkspaceFolders: () => disposable
  },
  window: {
    createOutputChannel: () => ({ appendLine: () => undefined, dispose: () => undefined }),
    createStatusBarItem: () => ({ show: () => undefined, hide: () => undefined, dispose: () => undefined }),
    createTreeView: () => ({ visible: false, onDidChangeVisibility: () => disposable, dispose: () => undefined }),
    registerTreeDataProvider: () => disposable,
    registerWebviewViewProvider: () => disposable,
    registerWebviewPanelSerializer: () => disposable,
    onDidChangeActiveTextEditor: () => disposable,
    withProgress: async (_options: unknown, task: () => Promise<unknown>) => task()
  },
  commands: {
    registerCommand: () => disposable,
    executeCommand: async () => undefined
  },
  chat: { createChatParticipant: () => ({ dispose: () => undefined }) }
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
const {
  DEACTIVATE_DEADLINE_MS,
  RUNTIME_VALIDATION_PENDING_MESSAGE,
  isRecoverableBridgeFailure,
  runtimeValidatingConfig,
  withDeadline
} = require("../src/extension") as typeof import("../src/extension");
moduleLoader._load = originalLoad;

test("a missing managed runtime stays recoverable while a real trust failure does not", () => {
  assert.equal(isRecoverableBridgeFailure("cli_untrusted", "managed"), false);
  assert.equal(isRecoverableBridgeFailure("cli_untrusted", "unavailable"), true);
  assert.equal(isRecoverableBridgeFailure("cli_runtime_unavailable", "managed"), true);
  assert.equal(isRecoverableBridgeFailure("cli_missing", "managed"), true);
  assert.equal(isRecoverableBridgeFailure("cli_error", undefined), true);
});

test("config stays fail-closed with a validating reason until the runtime pointer settles", () => {
  const blocked = {
    runtimeSelection: { origin: "unavailable", production: false, message: "No signed managed CLI." },
    security: {
      cliPath: { value: "alysis", resolvedExecutablePath: null, source: "default", trusted: false, executionAllowed: false, apiKeyForwardingAllowed: false, reason: "No signed managed CLI." }
    }
  } as unknown as AlysisConfig;

  const validating = runtimeValidatingConfig(blocked);
  assert.equal(validating.security?.cliPath.executionAllowed, false, "execution must stay blocked while validating");
  assert.equal(validating.security?.cliPath.reason, RUNTIME_VALIDATION_PENDING_MESSAGE);
  assert.equal(validating.runtimeSelection?.message, RUNTIME_VALIDATION_PENDING_MESSAGE);
  assert.equal(validating.runtimeSelection?.validating, true);

  const managed = {
    runtimeSelection: { origin: "managed", production: true, message: "Verified managed CLI." }
  } as unknown as AlysisConfig;
  assert.equal(runtimeValidatingConfig(managed), managed, "a resolved runtime is never relabelled");
});

test("deactivate teardown is bounded so a wedged CLI cannot hang Reload Window", async () => {
  assert.ok(DEACTIVATE_DEADLINE_MS > 0 && DEACTIVATE_DEADLINE_MS <= 5_000);

  // The deadline timer is unref'd so it can never hold the extension host open; keep the loop alive
  // for the duration of the assertion only.
  const keepAlive = setTimeout(() => undefined, 5_000);
  const started = Date.now();
  await withDeadline(new Promise<void>(() => undefined), 40);
  clearTimeout(keepAlive);
  assert.ok(Date.now() - started < 2_000, "the deadline resolves without waiting for the wedged work");

  await withDeadline(Promise.reject(new Error("shutdown failed")), 1_000);
  await withDeadline(Promise.resolve(), 1_000);
});

test("activation keeps managed-runtime validation and preview cleanup off the critical path", () => {
  const source = readFileSync(resolve(__dirname, "../../src/extension.ts"), "utf8");
  // Nothing at the top level of activate() awaits the runtime pointer or the preview cleanup.
  assert.doesNotMatch(source, /^ {2}await managedRuntime\.refresh\(/m);
  assert.doesNotMatch(source, /await browserPreviewStore\.clear\(\)/);
  assert.doesNotMatch(source, /^ {4}await checkBridgeStatus\(\);$/m);
  assert.match(source, /await runtimeValidation;/);
  // A reconnect probe only re-validates when there is no usable runtime to probe with.
  assert.match(
    source,
    /if \(getConfig\(\)\.runtimeSelection\?\.origin === "unavailable"\) \{\s*\n\s*await managedRuntime\.refresh\(/
  );
  // Forge state must feed the cancel command's enablement key.
  assert.match(source, /forgeController\.onDidChangeActivity\(syncCommandContexts\)/);
  assert.match(source, /chatController\.hasActiveJob\(\) \|\| forgeController\.hasActiveJob\(\)/);
  // Only runtime-affecting settings may re-run validation.
  assert.match(source, /RUNTIME_AFFECTING_SETTINGS\.some\(\(setting\) => event\.affectsConfiguration\(setting\)\)/);
});
