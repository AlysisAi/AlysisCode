#!/usr/bin/env node
/*
 * Headless extension host for QA.
 *
 * Activates the REAL compiled extension (out/src/extension.js) under a stubbed `vscode` module in
 * ExtensionMode.Development, resolves the sidebar webview with a fake webview, and drives it with
 * the same messages media/startView.js posts. The bridge, CLI, provider, and workspace are all real;
 * only the VS Code UI is simulated. Every published `state` message and every host-side prompt is
 * logged, and every access to a `vscode` API the stub does not implement is logged as UNSTUBBED so
 * a silent gap cannot masquerade as a passing run.
 *
 * Usage:
 *   node scripts/qa/headless-host.js --workspace <dir> --cli <alysis executable>
 *       [--task "<instruction>"] [--mode review|readonly|auto]
 *       [--approve allow_once|allow_for_session|deny] [--timeout-ms 600000] [--log <file>]
 *       [--storage <dir>]
 */
"use strict";

const Module = require("node:module");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// ----------------------------------------------------------------------------------------------
// Args
// ----------------------------------------------------------------------------------------------
const argv = process.argv.slice(2);
const opt = (name, fallback) => {
  const index = argv.indexOf(`--${name}`);
  if (index === -1) return fallback;
  const value = argv[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};
const extRoot = path.resolve(opt("ext", path.resolve(__dirname, "..", "..")));
const workspaceDir = path.resolve(String(opt("workspace", process.cwd())));
const cliPath = String(opt("cli", ""));
const task = opt("task", "");
const mode = String(opt("mode", "review"));
const approveDecision = String(opt("approve", "allow_once"));
const timeoutMs = Number(opt("timeout-ms", 600_000));
const logFile = opt("log", "");
const storageDir = String(opt("storage", path.join(os.tmpdir(), "alysis-headless-host")));
fs.mkdirSync(storageDir, { recursive: true });
// --env-file <json>: replace this process's environment with the given map (e.g. one captured from
// a live extension host), so the bridge is spawned exactly as that host would spawn it.
const envFile = opt("env-file", "");
if (envFile) {
  const captured = JSON.parse(fs.readFileSync(String(envFile), "utf8"));
  for (const key of Object.keys(process.env)) delete process.env[key];
  Object.assign(process.env, captured);
}

// ----------------------------------------------------------------------------------------------
// Logging
// ----------------------------------------------------------------------------------------------
const startedAt = Date.now();
const logStream = logFile ? fs.createWriteStream(String(logFile), { flags: "w" }) : null;
const findings = [];
function log(tag, ...parts) {
  const text = parts.map((part) => (typeof part === "string" ? part : safeJson(part))).join(" ");
  const line = `[+${String(Date.now() - startedAt).padStart(6)}ms] ${tag.padEnd(10)} ${text}`;
  process.stdout.write(line + "\n");
  if (logStream) logStream.write(line + "\n");
}
function finding(kind, detail) {
  findings.push({ kind, detail });
  log("FINDING", kind, detail);
}
function safeJson(value) {
  try {
    return JSON.stringify(value, (_key, item) => (typeof item === "bigint" ? String(item) : item));
  } catch {
    return String(value);
  }
}
function preview(value, max = 220) {
  const text = typeof value === "string" ? value : String(safeJson(value));
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

// ----------------------------------------------------------------------------------------------
// vscode stub
// ----------------------------------------------------------------------------------------------
const disposable = { dispose() {} };

class EventEmitter {
  constructor() {
    this.listeners = new Set();
    this.event = (listener) => {
      this.listeners.add(listener);
      return { dispose: () => this.listeners.delete(listener) };
    };
  }
  fire(value) {
    for (const listener of [...this.listeners]) {
      try {
        listener(value);
      } catch (error) {
        finding("listener_threw", `${error && error.stack ? error.stack : error}`);
      }
    }
  }
  dispose() {
    this.listeners.clear();
  }
}

class Disposable {
  constructor(fn) {
    this.fn = fn;
  }
  dispose() {
    if (this.fn) this.fn();
  }
  static from(...items) {
    return new Disposable(() => items.forEach((item) => item && item.dispose && item.dispose()));
  }
}

class Uri {
  constructor(scheme, authority, p, query, fragment) {
    this.scheme = scheme;
    this.authority = authority || "";
    this.path = p || "";
    this.query = query || "";
    this.fragment = fragment || "";
  }
  get fsPath() {
    if (this.scheme !== "file") return this.path;
    let p = this.path;
    if (/^\/[A-Za-z]:/.test(p)) p = p.slice(1);
    return process.platform === "win32" ? p.replace(/\//g, "\\") : p;
  }
  with(change) {
    return new Uri(
      change.scheme ?? this.scheme,
      change.authority ?? this.authority,
      change.path ?? this.path,
      change.query ?? this.query,
      change.fragment ?? this.fragment
    );
  }
  toString() {
    const p = this.path.split("/").map((seg) => encodeURIComponent(seg)).join("/");
    return `${this.scheme}://${this.authority}${p}${this.query ? `?${this.query}` : ""}${this.fragment ? `#${this.fragment}` : ""}`;
  }
  toJSON() {
    return { scheme: this.scheme, authority: this.authority, path: this.path, fsPath: this.fsPath };
  }
  static file(p) {
    let normalized = path.resolve(p).replace(/\\/g, "/");
    if (/^[A-Za-z]:/.test(normalized)) normalized = `/${normalized}`;
    return new Uri("file", "", normalized, "", "");
  }
  static parse(value) {
    const match = /^([a-zA-Z][a-zA-Z0-9+.-]*):(?:\/\/([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/.exec(String(value));
    if (!match) return Uri.file(String(value));
    return new Uri(match[1], match[2] || "", decodeURIComponent(match[3] || ""), match[4] || "", match[5] || "");
  }
  static from(parts) {
    return new Uri(parts.scheme || "file", parts.authority, parts.path, parts.query, parts.fragment);
  }
  static joinPath(base, ...segments) {
    if (base.scheme === "file") return Uri.file(path.join(base.fsPath, ...segments));
    return base.with({ path: [base.path.replace(/\/$/, ""), ...segments].join("/") });
  }
}

class Position {
  constructor(line, character) {
    this.line = line;
    this.character = character;
  }
}
class Range {
  constructor(a, b, c, d) {
    if (typeof a === "number") {
      this.start = new Position(a, b);
      this.end = new Position(c, d);
    } else {
      this.start = a;
      this.end = b;
    }
    this.isEmpty = this.start.line === this.end.line && this.start.character === this.end.character;
  }
}
class Selection extends Range {}
class Diagnostic {
  constructor(range, message, severity) {
    this.range = range;
    this.message = message;
    this.severity = severity;
  }
}
class TreeItem {
  constructor(label, collapsibleState) {
    this.label = label;
    this.collapsibleState = collapsibleState;
  }
}
class ThemeIcon {
  constructor(id, color) {
    this.id = id;
    this.color = color;
  }
}
class ThemeColor {
  constructor(id) {
    this.id = id;
  }
}
class MarkdownString {
  constructor(value) {
    this.value = value || "";
  }
  appendMarkdown(text) {
    this.value += text;
    return this;
  }
  appendText(text) {
    this.value += text;
    return this;
  }
}
class CancellationTokenSource {
  constructor() {
    this.emitter = new EventEmitter();
    this.token = { isCancellationRequested: false, onCancellationRequested: this.emitter.event };
  }
  cancel() {
    this.token.isCancellationRequested = true;
    this.emitter.fire();
  }
  dispose() {}
}
class RelativePattern {
  constructor(base, pattern) {
    this.base = base;
    this.pattern = pattern;
  }
}

// Records
const registeredCommands = new Map();
const executedCommands = [];
const notifications = [];
const quickPicks = [];
const inputBoxes = [];
const outputChannels = new Map();
let webviewViewProvider = null;
const contextKeys = new Map();

const memento = () => {
  const store = new Map();
  return {
    get: (key, fallback) => (store.has(key) ? store.get(key) : fallback),
    update: async (key, value) => {
      if (value === undefined) store.delete(key);
      else store.set(key, value);
    },
    keys: () => [...store.keys()],
    setKeysForSync() {}
  };
};

const settings = {
  alysis: {
    cliPath,
    defaultMode: mode,
    defaultModel: "",
    baseUrl: "",
    provider: "",
    transport: "stdio",
    sandboxProfile: "default",
    showStatusBar: true,
    autoStartBridge: false
  },
  python: { defaultInterpreterPath: "" }
};
function configurationFor(section) {
  const values = settings[section] || {};
  return {
    get: (key, fallback) => (Object.prototype.hasOwnProperty.call(values, key) ? values[key] : fallback),
    has: (key) => Object.prototype.hasOwnProperty.call(values, key),
    inspect: (key) => ({ key: `${section}.${key}`, defaultValue: undefined, globalValue: values[key] }),
    update: async (key, value) => {
      log("CONFIG", `update ${section}.${key} =`, preview(value));
      values[key] = value;
    }
  };
}

const workspaceFolder = { uri: Uri.file(workspaceDir), name: path.basename(workspaceDir), index: 0 };

function unstubbed(namespace) {
  return (target, prop) => {
    if (prop in target || typeof prop === "symbol" || prop === "then" || prop === "toJSON") return target[prop];
    finding("unstubbed_api", `vscode.${namespace}.${String(prop)}`);
    return (...args) => {
      log("STUB", `vscode.${namespace}.${String(prop)}(`, preview(args, 160), ") -> undefined");
      return undefined;
    };
  };
}

const outputChannelFactory = (name) => {
  const channel = {
    name,
    appendLine: (line) => log("OUTPUT", `[${name}]`, String(line)),
    append: (text) => log("OUTPUT", `[${name}]`, String(text)),
    replace() {},
    clear() {},
    show() {},
    hide() {},
    dispose() {}
  };
  outputChannels.set(name, channel);
  return channel;
};

const fakeDocumentFor = (uri, content) => ({
  uri,
  fileName: uri.fsPath,
  languageId: "plaintext",
  version: 1,
  isDirty: false,
  isUntitled: false,
  lineCount: content.split(/\r?\n/).length,
  getText: () => content,
  save: async () => true
});

const vscodeStub = {
  EventEmitter,
  Disposable,
  Uri,
  Position,
  Range,
  Selection,
  Diagnostic,
  TreeItem,
  ThemeIcon,
  ThemeColor,
  MarkdownString,
  CancellationTokenSource,
  RelativePattern,
  DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  ViewColumn: { Active: -1, Beside: -2, One: 1, Two: 2, Three: 3 },
  QuickPickItemKind: { Separator: -1, Default: 0 },
  ExtensionMode: { Production: 1, Development: 2, Test: 3 },
  ProgressLocation: { SourceControl: 1, Window: 10, Notification: 15 },
  StatusBarAlignment: { Left: 1, Right: 2 },
  ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
  UIKind: { Desktop: 1, Web: 2 },
  EndOfLine: { LF: 1, CRLF: 2 },
  FileType: { Unknown: 0, File: 1, Directory: 2, SymbolicLink: 64 },
  TaskScope: { Global: 1, Workspace: 2 },
  ShellExecution: class {
    constructor(commandLine, options) {
      this.commandLine = commandLine;
      this.options = options;
    }
  },
  Task: class {
    constructor(definition, scope, name, source, execution) {
      Object.assign(this, { definition, scope, name, source, execution });
    }
  },
  env: new Proxy(
    {
      appName: "Headless Host",
      appRoot: extRoot,
      language: "en",
      machineId: "headless-machine",
      sessionId: "headless-session",
      remoteName: undefined,
      uiKind: 1,
      shell: process.env.ComSpec || "/bin/sh",
      clipboard: {
        text: "",
        writeText: async (text) => {
          log("CLIPBOARD", preview(text));
          vscodeStub.env.clipboard.text = text;
        },
        readText: async () => vscodeStub.env.clipboard.text
      },
      openExternal: async (uri) => {
        log("EXTERNAL", String(uri));
        return true;
      },
      asExternalUri: async (uri) => uri
    },
    { get: unstubbed("env") }
  ),
  languages: new Proxy(
    {
      createDiagnosticCollection: () => ({ clear() {}, set() {}, delete() {}, dispose() {}, get: () => [] }),
      getDiagnostics: () => [],
      onDidChangeDiagnostics: () => disposable
    },
    { get: unstubbed("languages") }
  ),
  tasks: new Proxy(
    {
      taskExecutions: [],
      fetchTasks: async () => [],
      executeTask: async (task) => {
        log("TASKS", "executeTask", preview(task && task.name));
        return { task, terminate() {} };
      },
      onDidStartTask: () => disposable,
      onDidEndTask: () => disposable,
      onDidStartTaskProcess: () => disposable,
      onDidEndTaskProcess: () => disposable
    },
    { get: unstubbed("tasks") }
  ),
  debug: new Proxy(
    {
      activeDebugSession: undefined,
      breakpoints: [],
      startDebugging: async () => false,
      stopDebugging: async () => undefined,
      onDidStartDebugSession: () => disposable,
      onDidTerminateDebugSession: () => disposable,
      onDidChangeActiveDebugSession: () => disposable,
      onDidReceiveDebugSessionCustomEvent: () => disposable
    },
    { get: unstubbed("debug") }
  ),
  extensions: new Proxy({ all: [], getExtension: () => undefined, onDidChange: () => disposable }, { get: unstubbed("extensions") }),
  chat: new Proxy({ createChatParticipant: () => ({ dispose() {} }) }, { get: unstubbed("chat") }),
  workspace: new Proxy(
    {
      isTrusted: true,
      workspaceFolders: [workspaceFolder],
      name: workspaceFolder.name,
      textDocuments: [],
      getConfiguration: (section) => configurationFor(section),
      getWorkspaceFolder: () => workspaceFolder,
      asRelativePath: (target) => path.relative(workspaceDir, typeof target === "string" ? target : target.fsPath),
      registerTextDocumentContentProvider: () => disposable,
      onDidChangeConfiguration: () => disposable,
      onDidGrantWorkspaceTrust: () => disposable,
      onDidChangeWorkspaceFolders: () => disposable,
      onDidChangeTextDocument: () => disposable,
      onDidOpenTextDocument: () => disposable,
      onDidCloseTextDocument: () => disposable,
      onDidSaveTextDocument: () => disposable,
      createFileSystemWatcher: () => ({ onDidChange: () => disposable, onDidCreate: () => disposable, onDidDelete: () => disposable, dispose() {} }),
      findFiles: async () => [],
      openTextDocument: async (target) => {
        const uri = typeof target === "string" ? Uri.file(target) : target && target.fsPath ? target : Uri.file(String(target && target.content ? "untitled.txt" : target));
        let content = "";
        try {
          if (uri.scheme === "file") content = fs.readFileSync(uri.fsPath, "utf8");
        } catch {
          content = "";
        }
        log("DOCUMENT", "open", uri.toString());
        return fakeDocumentFor(uri, content);
      },
      applyEdit: async () => true,
      fs: {
        readFile: async (uri) => new Uint8Array(fs.readFileSync(uri.fsPath)),
        writeFile: async (uri, data) => fs.writeFileSync(uri.fsPath, Buffer.from(data)),
        stat: async (uri) => {
          const stat = fs.statSync(uri.fsPath);
          return { type: stat.isDirectory() ? 2 : 1, ctime: stat.ctimeMs, mtime: stat.mtimeMs, size: stat.size };
        },
        readDirectory: async (uri) => fs.readdirSync(uri.fsPath, { withFileTypes: true }).map((entry) => [entry.name, entry.isDirectory() ? 2 : 1]),
        createDirectory: async (uri) => fs.mkdirSync(uri.fsPath, { recursive: true }),
        delete: async (uri) => fs.rmSync(uri.fsPath, { recursive: true, force: true })
      }
    },
    { get: unstubbed("workspace") }
  ),
  window: new Proxy(
    {
      activeTextEditor: undefined,
      visibleTextEditors: [],
      terminals: [],
      activeTerminal: undefined,
      createOutputChannel: outputChannelFactory,
      createStatusBarItem: () => ({ text: "", tooltip: "", command: undefined, show() {}, hide() {}, dispose() {} }),
      createTreeView: () => ({ visible: false, onDidChangeVisibility: () => disposable, onDidChangeSelection: () => disposable, reveal: async () => undefined, dispose() {} }),
      registerTreeDataProvider: () => disposable,
      registerWebviewViewProvider: (viewType, provider) => {
        log("HOST", `registerWebviewViewProvider ${viewType}`);
        if (viewType === "alysis.start") webviewViewProvider = provider;
        return disposable;
      },
      registerWebviewPanelSerializer: () => disposable,
      createWebviewPanel: (viewType, title) => {
        log("HOST", `createWebviewPanel ${viewType} "${title}"`);
        const onDidDispose = new EventEmitter();
        const onDidReceive = new EventEmitter();
        return {
          viewType,
          title,
          visible: true,
          active: true,
          webview: { html: "", options: {}, cspSource: "headless", asWebviewUri: (uri) => uri, onDidReceiveMessage: onDidReceive.event, postMessage: async () => true },
          onDidDispose: onDidDispose.event,
          onDidChangeViewState: () => disposable,
          reveal() {},
          dispose: () => onDidDispose.fire()
        };
      },
      onDidChangeActiveTextEditor: () => disposable,
      onDidChangeTextEditorSelection: () => disposable,
      onDidChangeVisibleTextEditors: () => disposable,
      onDidChangeActiveTerminal: () => disposable,
      onDidOpenTerminal: () => disposable,
      onDidCloseTerminal: () => disposable,
      withProgress: async (_options, work) => work({ report() {} }, new CancellationTokenSource().token),
      setStatusBarMessage: () => disposable,
      showTextDocument: async (doc) => {
        log("EDITOR", "showTextDocument", preview(doc && doc.uri ? doc.uri.toString() : doc));
        return { document: doc, selection: undefined, edit: async () => true };
      },
      showOpenDialog: async () => undefined,
      showSaveDialog: async () => undefined,
      createTerminal: (options) => {
        log("TERMINAL", "create", preview(options));
        return { name: "headless", sendText() {}, show() {}, dispose() {}, processId: Promise.resolve(0) };
      },
      showInformationMessage: async (message, ...items) => {
        notifications.push({ level: "info", message, items });
        log("NOTIFY", "info:", preview(message), items.length ? `choices=${preview(items, 120)}` : "");
        return undefined;
      },
      showWarningMessage: async (message, ...items) => {
        notifications.push({ level: "warning", message, items });
        log("NOTIFY", "warning:", preview(message), items.length ? `choices=${preview(items, 120)}` : "");
        // A modal asking to confirm execution: pick the affirmative choice so the flow continues.
        const choices = items.filter((item) => typeof item === "string" || (item && item.title));
        const labels = choices.map((item) => (typeof item === "string" ? item : item.title));
        const affirmative = labels.find((label) => /^(run|execute|continue|proceed|yes|allow|start|apply|confirm|ok)\b/i.test(String(label)));
        if (affirmative !== undefined) {
          log("NOTIFY", `auto-chose "${affirmative}"`);
          return choices[labels.indexOf(affirmative)];
        }
        return undefined;
      },
      showErrorMessage: async (message, ...items) => {
        notifications.push({ level: "error", message, items });
        finding("error_notification", preview(message, 400));
        return undefined;
      },
      showQuickPick: async (items, options) => {
        const resolved = await items;
        quickPicks.push({ options, items: resolved });
        const labels = (resolved || []).map((item) => item.label);
        log("QUICKPICK", preview(options && options.title), "items=", preview(labels, 300));
        const selectable = (resolved || []).filter((item) => item && item.kind !== -1);
        if (selectable.length === 0) return undefined;
        // Answer like an engaged user would: every task when asked to choose tasks, otherwise the
        // first real choice. Anything destructive is still behind a warning-message modal.
        if (options && options.canPickMany) {
          log("QUICKPICK", `auto-chose all ${selectable.length}`);
          return selectable;
        }
        log("QUICKPICK", `auto-chose "${selectable[0].label}"`);
        return selectable[0];
      },
      showInputBox: async (options) => {
        inputBoxes.push(options);
        log("INPUTBOX", preview(options && options.title), "-> cancelled");
        return undefined;
      }
    },
    { get: unstubbed("window") }
  ),
  commands: new Proxy(
    {
      registerCommand: (id, handler) => {
        registeredCommands.set(id, handler);
        return { dispose: () => registeredCommands.delete(id) };
      },
      executeCommand: async (id, ...args) => {
        executedCommands.push({ id, args });
        if (registeredCommands.has(id)) {
          return registeredCommands.get(id)(...args);
        }
        if (id === "setContext") {
          contextKeys.set(args[0], args[1]);
          return undefined;
        }
        if (id === "vscode.diff") {
          log("DIFF", "vscode.diff", preview(args.map((arg) => (arg && arg.toString ? arg.toString() : arg)), 400));
          return undefined;
        }
        log("COMMAND", `executeCommand ${id}`, preview(args, 160), "(no handler; ignored)");
        return undefined;
      },
      getCommands: async () => [...registeredCommands.keys()]
    },
    { get: unstubbed("commands") }
  )
};

// Install the stub before the extension is loaded.
const moduleLoader = Module;
const originalLoad = moduleLoader._load;
moduleLoader._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return vscodeStub;
  return originalLoad.call(this, request, parent, isMain);
};

// ----------------------------------------------------------------------------------------------
// Extension context
// ----------------------------------------------------------------------------------------------
const secretStore = new Map();
const secretEvents = new EventEmitter();
const packageJson = JSON.parse(fs.readFileSync(path.join(extRoot, "package.json"), "utf8"));
const context = {
  extensionUri: Uri.file(extRoot),
  extensionPath: extRoot,
  extensionMode: 2,
  subscriptions: [],
  secrets: {
    get: async (key) => secretStore.get(key),
    store: async (key, value) => {
      secretStore.set(key, value);
      secretEvents.fire({ key });
    },
    delete: async (key) => {
      secretStore.delete(key);
      secretEvents.fire({ key });
    },
    onDidChange: secretEvents.event
  },
  globalState: memento(),
  workspaceState: memento(),
  globalStorageUri: Uri.file(path.join(storageDir, "global")),
  storageUri: Uri.file(path.join(storageDir, "workspace")),
  logUri: Uri.file(path.join(storageDir, "logs")),
  extension: { id: `${packageJson.publisher}.${packageJson.name}`, packageJSON: packageJson, extensionUri: Uri.file(extRoot) },
  asAbsolutePath: (relative) => path.join(extRoot, relative)
};
fs.mkdirSync(context.globalStorageUri.fsPath, { recursive: true });
fs.mkdirSync(context.storageUri.fsPath, { recursive: true });

// ----------------------------------------------------------------------------------------------
// Fake sidebar webview
// ----------------------------------------------------------------------------------------------
const received = new EventEmitter();
const visibility = new EventEmitter();
const disposed = new EventEmitter();
const states = [];
const webviewMessages = [];
let latestState = null;
const stateWaiters = [];
const fakeView = {
  viewType: "alysis.start",
  title: "Alysis Code",
  visible: true,
  webview: {
    html: "",
    options: {},
    cspSource: "headless",
    asWebviewUri: (uri) => uri,
    onDidReceiveMessage: received.event,
    postMessage: async (message) => {
      webviewMessages.push(message);
      if (message && message.type === "state") {
        latestState = message.state;
        states.push(message.state);
        for (const waiter of [...stateWaiters]) waiter(message.state);
      } else if (message && message.type !== "browser.state") {
        log("WEBVIEW<-", preview(message, 300));
      }
      return true;
    }
  },
  onDidChangeVisibility: visibility.event,
  onDidDispose: disposed.event,
  show() {}
};
function postToHost(message) {
  log("WEBVIEW->", preview(message, 200));
  received.fire(message);
}
function onNextState(predicate, deadlineMs) {
  return new Promise((resolve, reject) => {
    if (latestState && predicate(latestState)) return resolve(latestState);
    const timer = setTimeout(() => {
      stateWaiters.splice(stateWaiters.indexOf(waiter), 1);
      reject(new Error(`timed out after ${deadlineMs}ms waiting for state`));
    }, deadlineMs);
    const waiter = (state) => {
      if (predicate(state)) {
        clearTimeout(timer);
        stateWaiters.splice(stateWaiters.indexOf(waiter), 1);
        resolve(state);
      }
    };
    stateWaiters.push(waiter);
  });
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ----------------------------------------------------------------------------------------------
// Scenario
// ----------------------------------------------------------------------------------------------
function describeConversation(state) {
  const conversation = state.conversation || {};
  return `job=${conversation.jobStatus} running=${conversation.running} items=${(conversation.items || []).length}`;
}
function pendingApprovals(state) {
  return (state.conversation && state.conversation.items ? state.conversation.items : []).filter(
    (item) => item.approvalId && /pending|waiting|awaiting/i.test(String(item.status || ""))
  );
}
const seenItemIds = new Set();
function logNewItems(state) {
  for (const item of state.conversation && state.conversation.items ? state.conversation.items : []) {
    const key = `${item.id}:${item.status}`;
    if (seenItemIds.has(key)) continue;
    seenItemIds.add(key);
    const bits = [`kind=${item.kind}`, item.status ? `status=${item.status}` : "", item.toolName ? `tool=${item.toolName}` : "", item.approvalId ? `approval=${item.approvalId}` : "", item.errorKind ? `error=${item.errorKind}:${item.errorTitle}` : ""].filter(Boolean);
    log("ITEM", bits.join(" "), item.text ? `:: ${preview(item.text, 240)}` : "");
  }
}

async function main() {
  log("HOST", `ext=${extRoot}`);
  log("HOST", `workspace=${workspaceDir}`);
  log("HOST", `cli=${cliPath || "(PATH)"} mode=${mode}`);
  // Tap the live bridge event stream (the same one ChatController consumes) so every protocol
  // event is on record, whether or not the transcript chose to render it.
  const bridgeModule = require(path.join(extRoot, "out", "src", "client", "AlysisBridgeClient.js"));
  const BridgeClient = bridgeModule.AlysisBridgeClient;
  if (BridgeClient && BridgeClient.prototype && typeof BridgeClient.prototype.on === "function") {
    const originalOn = BridgeClient.prototype.on;
    let tapped = false;
    BridgeClient.prototype.on = function tappedOn(...args) {
      if (!tapped) {
        tapped = true;
        originalOn.call(this, "event", (event) => {
          const payload = (event && event.payload) || {};
          const bits = {};
          for (const key of ["tool_name", "tool", "name", "status", "operation", "kind", "display_title", "summary", "approval_id", "call_id", "message", "reason", "exit_code", "error", "blocked"]) {
            if (payload[key] !== undefined && payload[key] !== null && payload[key] !== "") bits[key] = payload[key];
          }
          log("EVENT", `${event && event.type} #${event && event.sequence}`, preview(bits, 360));
        });
      }
      return originalOn.apply(this, args);
    };
  }
  const extension = require(path.join(extRoot, "out", "src", "extension.js"));
  const activatedAt = Date.now();
  await extension.activate(context);
  log("HOST", `activate() resolved in ${Date.now() - activatedAt}ms; commands=${registeredCommands.size}`);

  const declared = new Set((packageJson.contributes.commands || []).map((entry) => entry.command));
  for (const id of declared) {
    if (!registeredCommands.has(id)) finding("command_not_registered", id);
  }

  if (!webviewViewProvider) throw new Error("alysis.start webview provider was never registered");
  webviewViewProvider.resolveWebviewView(fakeView);
  if (!fakeView.webview.html.includes("startView.js")) finding("webview_html", "sidebar html does not reference media/startView.js");
  postToHost({ type: "ready" });

  const ready = await onNextState((state) => state.models && state.models.loaded, 120_000).catch((error) => {
    finding("models_never_loaded", error.message);
    return latestState;
  });
  if (ready) {
    log("STATE", `ready=${ready.ready} reason="${ready.readyReason}" provider=${ready.providerName} model=${ready.modelName} engine=${preview(ready.engine, 160)}`);
    log("STATE", `models: status=${ready.models.selectionStatus} active=${ready.models.activeProfile}/${ready.models.activeModel} connections=${(ready.models.connections || []).map((c) => `${c.profile}${c.active ? "*" : ""}[${(c.models || []).length}]`).join(",")}`);
    if (ready.readiness && ready.readiness.blockers && ready.readiness.blockers.length) {
      log("STATE", "blockers:", preview(ready.readiness.blockers, 600));
    }
  }

  // Doctor / check-setup through the real command, like the palette would.
  if (registeredCommands.has("alysis.showBridgeHealth")) {
    await Promise.race([registeredCommands.get("alysis.showBridgeHealth")(), sleep(60_000)]).catch((error) => finding("bridge_health_threw", String(error)));
  }

  if (task) {
    const requestId = `headless-${Date.now()}`;
    postToHost({ type: "task.submit", instruction: String(task), mode, requestId });
    const findResult = () => webviewMessages.find((message) => message.type === "task.result" && message.requestId === requestId);
    const resultDeadline = Date.now() + 90_000;
    while (!findResult() && Date.now() < resultDeadline) {
      await sleep(200);
    }
    const taskResult = findResult();
    log("TASK", "task.result:", preview(taskResult), `after ${Date.now() - (resultDeadline - 90_000)}ms`);
    if (!taskResult || !taskResult.started) finding("task_not_started", preview(taskResult));

    const deadline = Date.now() + timeoutMs;
    let lastLogged = "";
    while (Date.now() < deadline) {
      const state = latestState;
      if (state) {
        logNewItems(state);
        const summary = describeConversation(state);
        if (summary !== lastLogged) {
          log("STATE", summary);
          lastLogged = summary;
        }
        for (const item of pendingApprovals(state)) {
          if (!seenItemIds.has(`approved:${item.approvalId}`)) {
            seenItemIds.add(`approved:${item.approvalId}`);
            log("APPROVAL", `${approveDecision} -> ${item.approvalId}`, preview(item.text, 200));
            postToHost({ type: "approval", approvalId: item.approvalId, decision: approveDecision });
          }
        }
        const status = state.conversation ? state.conversation.jobStatus : "";
        if (taskResult && taskResult.started && !state.conversation.running && ["completed", "failed", "cancelled", "interrupted"].includes(status)) {
          log("TASK", `terminal status=${status}`);
          break;
        }
      }
      await sleep(400);
    }
    if (latestState) {
      logNewItems(latestState);
      const final = latestState.conversation;
      const failures = (final.items || []).filter((item) => item.errorKind);
      for (const failure of failures) finding("conversation_error", `${failure.errorKind}: ${failure.errorTitle} — ${failure.errorDetail}`);
      if (final.jobStatus !== "completed") finding("job_not_completed", `jobStatus=${final.jobStatus}`);
    }
  }

  // Tear down like Reload Window would.
  const deactivateAt = Date.now();
  if (typeof extension.deactivate === "function") await extension.deactivate();
  log("HOST", `deactivate() resolved in ${Date.now() - deactivateAt}ms`);
  for (const item of context.subscriptions) {
    try {
      item && item.dispose && item.dispose();
    } catch (error) {
      finding("dispose_threw", String(error));
    }
  }
}

main()
  .catch((error) => finding("harness_crashed", error && error.stack ? error.stack : String(error)))
  .finally(() => {
    log("SUMMARY", `states=${states.length} notifications=${notifications.length} quickPicks=${quickPicks.length} findings=${findings.length}`);
    for (const entry of findings) log("SUMMARY", `- ${entry.kind}: ${entry.detail}`);
    if (logStream) logStream.end();
    // Give the bridge a moment to exit, then leave regardless of stray handles.
    setTimeout(() => process.exit(findings.some((entry) => entry.kind === "harness_crashed") ? 2 : 0), 1500).unref();
  });
