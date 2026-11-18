import { readFileSync } from "node:fs";

import { createHash, randomBytes } from "node:crypto";

import * as vscode from "vscode";

import {
  BrowserCockpitState,
  emptyBrowserCockpitState
} from "../browser/BrowserCockpitController";
import type { BrowserPreview } from "../browser/BrowserPreviewStore";

import {
  ActionResultEntry,
  CockpitState,
  PersonasSurfaceState,
  ProviderProfileState,
  clampPersonaMode
} from "../chat/CockpitState";
import { CockpitRuntimeSnapshot } from "../chat/CockpitRuntimeState";
import {
  CockpitAction,
  CockpitReadiness,
  TRUST_MANAGEMENT_COMMAND,
  buildCockpitReadiness
} from "../chat/CockpitReadiness";
import { ChatItemKind } from "../chat/ChatTranscript";
import type { ChatErrorKind } from "../chat/ChatErrorTaxonomy";
import { ChatPanelMessage, isChatPanelMessage } from "../chat/webviewMessages";
import { BACKEND_ACTIONS, BACKEND_ACTION_GROUPS } from "../backend/BackendActionMetadata";
import { AlysisConfig, redactForDisplay } from "../client/CliDiscovery";
import { SessionSummary, AlysisMode } from "../client/AlysisProtocol";
import type { CompatibilityFeatureId } from "../client/compatibility";
import { COMMANDS } from "../commands/registry";
import { activeWorkspaceFolder, workspaceScopeRequiredMessage } from "../workspace/activeWorkspace";
import { savePastedImage } from "../workspace/pastedImages";
import type { GitWorkspaceState } from "../workspace/GitWorktrees";
import { resolveWorkspaceFileReference } from "../workspace/workspaceFileLinks";
import { availableSlashCommands } from "../slash/SlashCommandRegistry";
import {
  emptyModelsSurfaceState,
  providerSelectionReadiness,
  type ModelsSurfaceState
} from "../providers/ProviderCatalogController";

type StartViewTone = "ready" | "attention" | "neutral";
export type StartViewSurface = "task" | "browser" | "history" | "settings" | "activity" | "models";

export const START_VIEW_SURFACES: readonly StartViewSurface[] = [
  "task",
  "browser",
  "history",
  "settings",
  "activity",
  "models"
];

/** Host-side operations the Models surface and composer pickers dispatch to. */
export interface StartViewProviderActions {
  getWorktreeState?(): GitWorkspaceState;
  worktreeAction?(action: "new" | "move" | "review"): Promise<void>;
  refreshModels(): Promise<void>;
  connectProvider(request: { presetKey: string; model?: string; baseUrl?: string }): Promise<void>;
  useProvider(profile: string): Promise<void>;
  updateProviderKey(profile: string): Promise<void>;
  forgetProviderKey(profile: string): Promise<void>;
  searchMentions(query: string): Promise<StartViewMention[]>;
  attachContext?(): Promise<void>;
  attachFiles?(): Promise<void>;
  getImages?(sessionId: string): Promise<unknown>;
}

/** One workspace path offered by the composer's @ autocomplete. */
export interface StartViewMention {
  label: string;
  detail: string;
  insert: string;
  kind: "file" | "folder";
}

interface StartViewStatus {
  label: string;
  detail: string;
  tone: StartViewTone;
  action: "engine" | "workspace" | "provider";
}


interface StartViewRecentTask {
  sessionId: string;
  title: string;
  detail: string;
  current: boolean;
  canResume: boolean;
}

/** One persona picker row, using the agent's definition and actual active permissions. */
export interface StartViewPersonaRow {
  name: string;
  description: string;
  /** Only the active row has authoritative session permissions; other rows use permissionHint. */
  effectiveMode: AlysisMode;
  permissionHint: string;
  writeScoped: boolean;
  active: boolean;
}

export interface StartViewPersonasState {
  supported: boolean;
  /** Standard capability affordance when an older CLI lacks the persona methods. */
  reason: string | null;
  enabled: boolean;
  active: string;
  activeSource: string;
  options: StartViewPersonaRow[];
}

export interface StartViewState {
  mode: AlysisMode;
  providerName: string;
  modelName: string;
  engine: StartViewStatus;
  workspace: StartViewStatus;
  provider: StartViewStatus;
  recentTasks: StartViewRecentTask[];
  conversation: StartViewConversationState;
  actionResults: ActionResultEntry[];
  runtimeEvents: CockpitRuntimeSnapshot["events"];
  commands: StartViewCommand[];
  slashCommands: StartViewSlashCommand[];
  models: ModelsSurfaceState;
  personas: StartViewPersonasState;
  lastAcceptedTaskRequestId: string | null;
  /** False until a task can actually run: engine reachable, folder trusted, provider selected. */
  ready: boolean;
  readyReason: string;
  /** Everything blocking a run, rendered as a persistent strip above the composer. */
  readiness: CockpitReadiness;
}

interface StartViewCommand {
  id: string;
  title: string;
  description: string;
  category: string;
  command: string;
  mutates: boolean;
  available: boolean;
  unavailableReason: string;
}

/** One row of the composer's "/" menu: a real slash command the chat controller can route. */
export interface StartViewSlashCommand {
  /** The exact spelling to insert, e.g. `/status` or `/persona`. */
  command: string;
  title: string;
  description: string;
  /** Usage line, e.g. `/persona [name]`; equals `command` when the command takes no arguments. */
  usage: string;
  takesArgs: boolean;
}

interface StartViewConversationItem {
  id: string;
  kind: ChatItemKind;
  title: string;
  text: string;
  status: string;
  toolName: string | null;
  toolInput: string | null;
  semanticActivity: boolean;
  /** True for the bridge's turn-spanning activity; rendered as progress, never as a step. */
  turnLevel: boolean;
  approvalId: string | null;
  allowForSession: boolean;
  /** Structured approval request for the card renderer; null for every other item. */
  approval: StartViewApproval | null;
  /** Typed failure classification. Empty for anything that is not a failure. */
  errorKind: string;
  /** Plain-language heading for a typed failure; falls back to the generic label when absent. */
  errorTitle: string;
  errorDetail: string;
  errorActions: CockpitAction[];
  /** Present on rate limits so the card can count down instead of guessing. */
  retryAfterSeconds: number | null;
}

interface StartViewApproval {
  /** The tool or operation asking (e.g. `write_file`, `shell`, `verify_run`); "approval" when unknown. */
  kind: string;
  reason: string;
  preview: string;
  command: string | null;
  files: string[];
  /** ISO timestamp the request expires at, or null. */
  expiresAt: string | null;
  scope: string;
  warning: string | null;
}

interface StartViewConversationState {
  sessionId: string | null;
  mode: string | null;
  jobStatus: string;
  running: boolean;
  items: StartViewConversationItem[];
}

interface StartViewSessionSnapshot {
  sessions: SessionSummary[];
  activeSessionId: string | undefined;
  /** Session id -> first prompt, remembered by the sessions view. Optional for older callers. */
  titles?: Record<string, string>;
}

type StartViewMessage =
  | { type: "worktree"; action: "new" | "move" | "review" }
  | { type: "ready" }
  | { type: "task.submit"; instruction: string; mode: AlysisMode; requestId: string }
  | { type: "task.cancel" }
  | { type: "approval"; approvalId: string; decision: "allow_once" | "allow_for_session" | "deny" }
  | { type: "session"; action: "resume" | "show"; sessionId: string }
  | { type: "command"; command: string; args?: string[] }
  | { type: "cockpit"; message: ChatPanelMessage }
  | { type: "editor.insert"; text: string }
  | { type: "editor.applyCode"; text: string; language?: string }
  | { type: "clipboard.copy"; text: string }
  | { type: "open.external"; url: string }
  | { type: "open.file"; path: string }
  | { type: "image.attach"; uris: string[] }
  | { type: "image.paste"; name: string; mime: string; data: string }
  | { type: "models.refresh" }
  | { type: "provider.connect"; presetKey: string; model?: string; baseUrl?: string }
  | { type: "provider.use"; profile: string }
  | { type: "provider.key"; profile: string; keyAction: "update" | "forget" }
  | { type: "model.set"; model: string }
  | { type: "mention.search"; query: string; token: number }
  | {
      type: "action";
      action:
        | "plan"
        | "recent"
        | "new"
        | "engine"
        | "workspace"
        | "provider"
        | "mcp"
        | "settings"
        | "addContext"
        | "addFiles";
    };

const MAX_PRESET_KEY_LENGTH = 96;
const MAX_MODEL_NAME_LENGTH = 200;
const MAX_BASE_URL_LENGTH = 2048;
const MAX_MENTION_QUERY_LENGTH = 200;
const MAX_CODE_BLOCK_LENGTH = 100_000;
const MAX_COMMAND_ARGS = 4;
const MAX_COMMAND_ARG_LENGTH = 2048;
const MAX_PASTED_IMAGE_BYTES = 10 * 1024 * 1024;
/** Only raster formats the CLI accepts, matched by magic bytes before anything is written. */
const PASTED_IMAGE_TYPES: ReadonlyMap<string, { extension: string; magic: readonly number[] }> = new Map([
  ["image/png", { extension: "png", magic: [0x89, 0x50, 0x4e, 0x47] }],
  ["image/jpeg", { extension: "jpg", magic: [0xff, 0xd8, 0xff] }],
  ["image/gif", { extension: "gif", magic: [0x47, 0x49, 0x46, 0x38] }],
  ["image/webp", { extension: "webp", magic: [0x52, 0x49, 0x46, 0x46] }]
]);
const IMAGE_EXTENSIONS: readonly string[] = ["png", "jpg", "jpeg", "gif", "webp"];

export class StartViewProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  private view: vscode.WebviewView | undefined;
  private viewDisposables: vscode.Disposable[] = [];
  private viewReady = false;
  private pendingPrefill: string | undefined;
  private pendingSurface: StartViewSurface = "task";
  private readonly acceptedTaskRequestIds = new Map<string, string>();
  private readonly pendingTaskRequests = new Map<string, { signature: string; result: Promise<boolean> }>();
  private lastAcceptedTaskRequestId: string | null = null;
  private browserPostInFlight = false;
  private browserPostPending = false;
  private imageBasketKey = "";
  private imageBasketRequest = 0;

  public constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly getRuntime: () => CockpitRuntimeSnapshot,
    private readonly getConfig: () => AlysisConfig,
    private readonly getProfiles: () => ProviderProfileState,
    private readonly isWorkspaceTrusted: () => boolean,
    private readonly getCockpit: () => CockpitState,
    private readonly getSessions: () => StartViewSessionSnapshot,
    private readonly startTask: (instruction: string, mode: AlysisMode, requestId?: string) => Promise<boolean>,
    private readonly cancelTask: () => Promise<void>,
    private readonly respondToApproval: (
      approvalId: string,
      decision: "allow_once" | "allow_for_session" | "deny"
    ) => Promise<void>,
    private readonly handleCockpitAction: (message: ChatPanelMessage) => Promise<void>,
    private readonly getModels: () => ModelsSurfaceState = emptyModelsSurfaceState,
    private readonly actions: StartViewProviderActions | undefined = undefined,
    private readonly getBrowserState: () => BrowserCockpitState = () => emptyBrowserCockpitState(),
    private readonly getBrowserPreview: () => BrowserPreview | null = () => null,
    private readonly browserPreviewRoot: vscode.Uri | undefined = undefined,
    private readonly isBackendActionSupported: (actionId: string) => boolean = () => true
  ) {}

  public resolveWebviewView(view: vscode.WebviewView): void {
    this.disposeViewListeners();
    this.view = view;
    this.viewReady = false;
    this.imageBasketKey = "";
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(this.extensionUri, "media"),
        vscode.Uri.joinPath(this.extensionUri, "resources"),
        ...(this.browserPreviewRoot ? [this.browserPreviewRoot] : [])
      ]
    };
    view.webview.html = this.html(view.webview);
    this.viewDisposables = [
      view.webview.onDidReceiveMessage((message: unknown) => {
        if (isStartViewMessage(message)) {
          void this.handleMessageSafely(message, view);
        }
      }),
      view.onDidChangeVisibility(() => {
        if (view.visible) {
          this.update();
        }
      }),
      view.onDidDispose(() => {
        this.view = undefined;
        this.viewReady = false;
        this.browserPostPending = false;
        this.disposeViewListeners();
      })
    ];
  }

  public async reveal(surface: StartViewSurface = "task"): Promise<void> {
    this.pendingSurface = surface;
    await vscode.commands.executeCommand("workbench.view.extension.alysis");
    await vscode.commands.executeCommand("alysis.start.focus");
    this.view?.show(false);
    if (this.viewReady && this.view) {
      await this.view.webview.postMessage({ type: "surface.show", surface });
    }
  }

  public async showSurface(surface: StartViewSurface): Promise<void> {
    await this.reveal(surface);
  }

  public async prefill(text: string): Promise<void> {
    this.pendingPrefill = text;
    this.pendingSurface = "task";
    if (this.viewReady && this.view) {
      await this.view.webview.postMessage({ type: "surface.show", surface: "task" });
      await this.view.webview.postMessage({ type: "composer.prefill", text });
      this.pendingPrefill = undefined;
    }
  }

  public update(): void {
    if (!this.view || !this.viewReady) {
      return;
    }
    const cockpit = this.getCockpit();
    void this.view.webview.postMessage({
      type: "state",
      state: { ...buildStartViewState(
        this.getRuntime(),
        this.getConfig(),
        this.getProfiles(),
        this.isWorkspaceTrusted(),
        cockpit,
        this.getSessions(),
        this.getModels(),
        this.lastAcceptedTaskRequestId,
        this.isBackendActionSupported
      ), gitWorkspace: this.actions?.getWorktreeState?.() }
    });
    // Read the actual pending basket after session/turn transitions and image actions from
    // any entry point. Restore it when a webview reopens; never infer success from a click.
    const imageActions = cockpit.cockpit.actionResults?.filter((entry) => entry.actionId.startsWith("session.images."))
      .map((entry) => `${entry.id}:${entry.status}`).join(",") ?? "";
    const key = JSON.stringify([cockpit.sessionId, cockpit.jobStatus, this.lastAcceptedTaskRequestId, imageActions,
      this.isBackendActionSupported("session.images.list")]);
    if (key !== this.imageBasketKey) {
      this.imageBasketKey = key;
      void this.refreshImageBasket(cockpit.sessionId);
    }
  }

  /** Latest-wins browser channel; large bounded diagnostics never ride chat state deltas. */
  public updateBrowser(): void {
    if (!this.view || !this.viewReady) {
      return;
    }
    this.browserPostPending = true;
    if (!this.browserPostInFlight) {
      void this.flushBrowserState();
    }
  }

  public dispose(): void {
    this.view = undefined;
    this.viewReady = false;
    this.browserPostPending = false;
    this.disposeViewListeners();
  }

  private async flushBrowserState(): Promise<void> {
    if (this.browserPostInFlight) {
      return;
    }
    this.browserPostInFlight = true;
    try {
      while (this.browserPostPending && this.view && this.viewReady) {
        this.browserPostPending = false;
        const view = this.view;
        const preview = this.getBrowserPreview();
        const previewUri = preview
          ? String(view.webview.asWebviewUri(vscode.Uri.file(preview.path)))
          : null;
        await view.webview.postMessage({
          type: "browser.state",
          state: {
            ...this.getBrowserState(),
            previewUri,
            workspaceTrusted: this.isWorkspaceTrusted()
          }
        });
        if (this.view !== view) {
          return;
        }
      }
    } finally {
      this.browserPostInFlight = false;
      if (this.browserPostPending && this.view && this.viewReady) {
        void this.flushBrowserState();
      }
    }
  }

  private async handleMessageSafely(message: StartViewMessage, sourceView: vscode.WebviewView): Promise<void> {
    try {
      await this.handleMessage(message, sourceView);
    } catch (error) {
      if (message.type === "task.submit" && this.view === sourceView) {
        await sourceView.webview.postMessage({
          type: "task.result",
          started: false,
          ...(message.requestId ? { requestId: message.requestId } : {})
        }).then(undefined, () => undefined);
      }
      const detail = redactForDisplay(error instanceof Error ? error.message : String(error));
      await vscode.window.showErrorMessage(`Alysis Code could not complete that action: ${detail}`);
    }
  }

  private async handleMessage(message: StartViewMessage, sourceView: vscode.WebviewView): Promise<void> {
    if (this.view !== sourceView) {
      return;
    }
    if (message.type === "ready") {
      this.viewReady = true;
      this.update();
      this.updateBrowser();
      if (!this.getModels().loaded) {
        // Readiness fails closed while unknown, so start the authoritative bridge-backed refresh as
        // soon as the webview is alive and unlock the composer only after it completes.
        void this.actions?.refreshModels();
      }
      await sourceView.webview.postMessage({ type: "surface.show", surface: this.pendingSurface });
      if (this.pendingPrefill && this.view === sourceView) {
        await sourceView.webview.postMessage({ type: "composer.prefill", text: this.pendingPrefill });
        this.pendingPrefill = undefined;
      }
      return;
    }
    if (message.type === "task.submit") {
      const signature = startViewTaskSignature(message);
      const started = await this.runCorrelatedTaskRequest(message.requestId, signature, async () => {
        const readiness = providerSelectionReadiness(this.getConfig(), this.getModels());
        if (readiness.status !== "ready") {
          void vscode.window.showWarningMessage(readiness.reason);
          this.pendingSurface = "models";
          if (this.view === sourceView) {
            await sourceView.webview.postMessage({ type: "surface.show", surface: "models" });
          }
          if (readiness.status === "loading") {
            void this.actions?.refreshModels();
          }
          return false;
        }
        return this.startTask(message.instruction.trim(), message.mode, message.requestId);
      });
      if (this.view === sourceView) {
        await sourceView.webview.postMessage({
          type: "task.result",
          started,
          ...(message.requestId ? { requestId: message.requestId } : {})
        });
      }
      return;
    }
    if (message.type === "task.cancel") {
      await this.cancelTask();
      return;
    }
    if (message.type === "worktree") {
      await this.actions?.worktreeAction?.(message.action);
      return;
    }
    if (message.type === "approval") {
      await this.respondToApproval(message.approvalId, message.decision);
      return;
    }
    if (message.type === "session") {
      await vscode.commands.executeCommand(
        message.action === "resume" ? "alysis.session.resume" : "alysis.session.show",
        { sessionId: message.sessionId }
      );
      return;
    }
    if (message.type === "command") {
      const args = Array.isArray(message.args) ? message.args : [];
      // Recovery commands are the way out of a broken setup, so they stay reachable even when the
      // catalog marks everything else unavailable. They are still confined to the allowlist.
      if (RECOVERY_COMMAND_IDS.has(message.command)) {
        await vscode.commands.executeCommand(message.command, ...args);
        return;
      }
      const current = buildCommandCatalog(
        this.getCockpit(),
        this.isWorkspaceTrusted(),
        this.isBackendActionSupported
      ).find((item) => item.command === message.command);
      if (!current || !current.available) {
        void vscode.window.showWarningMessage(
          current?.unavailableReason || "That action is not available with the connected Alysis Code CLI."
        );
        return;
      }
      await vscode.commands.executeCommand(current.command, ...args);
      return;
    }
    if (message.type === "clipboard.copy") {
      await vscode.env.clipboard.writeText(message.text);
      return;
    }
    if (message.type === "editor.insert") {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        void vscode.window.showWarningMessage("Open a file in the editor before inserting this code.");
        return;
      }
      await editor.insertSnippet(new vscode.SnippetString().appendText(message.text));
      await vscode.window.showTextDocument(editor.document, editor.viewColumn);
      return;
    }
    if (message.type === "editor.applyCode") {
      // Never blind-write a user's file: the proposed content opens as a diff against the file they
      // are looking at, so applying is their explicit editor gesture.
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        void vscode.window.showWarningMessage("Open the file you want to compare this code against.");
        return;
      }
      const proposed = await vscode.workspace.openTextDocument({
        content: message.text,
        language: message.language || editor.document.languageId
      });
      await vscode.commands.executeCommand(
        "vscode.diff",
        editor.document.uri,
        proposed.uri,
        `${basename(editor.document.uri.path)} ↔ Alysis Code suggestion`
      );
      return;
    }
    if (message.type === "open.external") {
      const target = safeExternalUri(message.url);
      if (!target) {
        void vscode.window.showWarningMessage("Alysis Code only opens https links.");
        return;
      }
      await vscode.env.openExternal(target);
      return;
    }
    if (message.type === "open.file") {
      const target = await this.resolveWorkspaceFile(message.path);
      if (!target) {
        void vscode.window.showWarningMessage(`Alysis Code could not find ${message.path} in this workspace.`);
        return;
      }
      await vscode.window.showTextDocument(target.uri, { selection: target.selection, preview: true });
      return;
    }
    if (message.type === "image.attach") {
      await this.attachImagePaths(message.uris);
      return;
    }
    if (message.type === "image.paste") {
      await this.attachPastedImage(message);
      return;
    }
    if (message.type === "cockpit") {
      await this.handleCockpitAction(message.message);
      return;
    }
    if (message.type === "models.refresh") {
      await this.actions?.refreshModels();
      return;
    }
    if (message.type === "provider.connect") {
      await this.actions?.connectProvider({
        presetKey: message.presetKey,
        model: message.model,
        baseUrl: message.baseUrl
      });
      return;
    }
    if (message.type === "provider.use") {
      await this.actions?.useProvider(message.profile);
      return;
    }
    if (message.type === "provider.key") {
      await (message.keyAction === "forget"
        ? this.actions?.forgetProviderKey(message.profile)
        : this.actions?.updateProviderKey(message.profile));
      return;
    }
    if (message.type === "model.set") {
      // Share the command picker path so the current conversation receives the change too.
      await vscode.commands.executeCommand(COMMANDS.pickModel, message.model);
      return;
    }
    if (message.type === "mention.search") {
      const results = (await this.actions?.searchMentions(message.query)) ?? [];
      if (this.view === sourceView) {
        // The token lets the webview drop answers to superseded keystrokes instead of flashing
        // stale paths when a slower search resolves after a faster one.
        await sourceView.webview.postMessage({
          type: "mention.results",
          token: message.token,
          results
        });
      }
      return;
    }
    switch (message.action) {
      case "recent":
        await this.showSurface("history");
        return;
      case "new":
        await vscode.commands.executeCommand("alysis.newSession");
        return;
      case "engine": {
        const engine = this.getRuntime();
        await vscode.commands.executeCommand(
          engine.cliHealth.status === "missing" || engine.cliHealth.status === "unreachable"
            ? "alysis.locateCli"
            : "alysis.showBridgeHealth"
        );
        return;
      }
      case "workspace":
        await vscode.commands.executeCommand("workbench.trust.manage");
        return;
      case "provider":
        // The in-sidebar catalog is the primary path; the QuickPick chain stays reachable as the
        // alysis.configureProvider command for keyboard users and untrusted-workspace fallback.
        await this.showSurface("models");
        await this.actions?.refreshModels();
        return;
      case "mcp":
        await vscode.commands.executeCommand("alysis.manageMcpHooks");
        return;
      case "settings":
        await vscode.commands.executeCommand("workbench.action.openSettings", "@ext:alysisai.vscode-alysis");
        return;
      case "addContext":
        if (this.actions?.attachContext) {
          await this.actions.attachContext();
        } else {
          await vscode.commands.executeCommand("alysis.addSelection");
        }
        return;
      case "addFiles":
        if (this.actions?.attachFiles) {
          await this.actions.attachFiles();
        } else {
          void vscode.window.showWarningMessage("File attachments are unavailable until Alysis Code finishes starting.");
        }
    }
  }

  private runCorrelatedTaskRequest(
    requestId: string | undefined,
    signature: string,
    start: () => Promise<boolean>
  ): Promise<boolean> {
    if (!requestId) {
      return start();
    }
    const acceptedSignature = this.acceptedTaskRequestIds.get(requestId);
    if (acceptedSignature) {
      return Promise.resolve(acceptedSignature === signature);
    }
    const existing = this.pendingTaskRequests.get(requestId);
    if (existing) {
      return existing.signature === signature ? existing.result : Promise.resolve(false);
    }
    const pending = start().then((accepted) => {
      if (accepted) {
        this.acceptedTaskRequestIds.delete(requestId);
        this.acceptedTaskRequestIds.set(requestId, signature);
        while (this.acceptedTaskRequestIds.size > 64) {
          const oldest = this.acceptedTaskRequestIds.keys().next().value as string | undefined;
          if (!oldest) {
            break;
          }
          this.acceptedTaskRequestIds.delete(oldest);
        }
        this.lastAcceptedTaskRequestId = requestId;
        this.update();
      }
      return accepted;
    }).finally(() => {
      if (this.pendingTaskRequests.get(requestId)?.result === pending) {
        this.pendingTaskRequests.delete(requestId);
      }
    });
    this.pendingTaskRequests.set(requestId, { signature, result: pending });
    return pending;
  }

  private html(webview: vscode.Webview): string {
    const nonce = randomBytes(24).toString("base64url");
    // A value that appears in readable attributes is not a nonce: the cache-buster is generated
    // independently so the script nonce never leaks into an asset URL.
    const assetVersion = randomBytes(8).toString("hex");
    const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "startView.css"));
    const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "startView.js"));
    const markUri = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "resources", "alysis-logo.png"));
    const csp = [
      "default-src 'none'",
      `img-src ${webview.cspSource}`,
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}'`,
      "base-uri 'none'",
      "form-action 'none'",
      "frame-src 'none'"
    ].join("; ");
    // Located through the extension root, never __dirname: the compiled layout differs between
    // the tsc output the tests load (out/src/views/) and the single bundled file VS Code loads
    // (dist/), and only the extension URI is stable across both.
    const template = readFileSync(vscode.Uri.joinPath(this.extensionUri, "media", "startView.html").fsPath, "utf8");
    const values: Record<string, string> = { csp, styleUri: String(styleUri), scriptUri: String(scriptUri), markUri: String(markUri), nonce, assetVersion };
    return template.replace(/\$\{(\w+)\}/g, (_match, key: string) => {
      if (!(key in values)) throw new Error(`Unknown sidebar template field: ${key}`);
      return values[key].replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
    });
  }

  /** Resolve a path the agent mentioned to a real workspace file, with an optional `:line` suffix. */
  private async resolveWorkspaceFile(
    reference: string
  ): Promise<{ uri: vscode.Uri; selection?: vscode.Range } | undefined> {
    return resolveWorkspaceFileReference(reference);
  }

  /** Attach image files dragged in from the explorer or the OS to the next chat turn. */
  private async attachImagePaths(uris: readonly string[]): Promise<void> {
    if (!this.isWorkspaceTrusted()) {
      void vscode.window.showWarningMessage("Trust this folder before attaching images to a task.");
      return;
    }
    const accepted: string[] = [];
    for (const raw of uris.slice(0, 8)) {
      let parsed: vscode.Uri;
      try {
        parsed = vscode.Uri.parse(raw.trim(), true);
      } catch {
        continue;
      }
      if (parsed.scheme !== "file" || !IMAGE_EXTENSIONS.includes(extensionOf(parsed.path))) {
        continue;
      }
      accepted.push(parsed.fsPath);
    }
    if (accepted.length === 0) {
      void vscode.window.showWarningMessage("Drop a PNG, JPEG, GIF, or WebP image to attach it.");
      return;
    }
    for (const imagePath of accepted) {
      // The backend action re-validates that the path stays inside the open workspace folder.
      await vscode.commands.executeCommand("alysis.backend.session.images.add", imagePath);
    }
  }

  /** Persist a pasted screenshot inside the workspace's Alysis Code scratch folder, then attach it. */
  private async attachPastedImage(message: Extract<StartViewMessage, { type: "image.paste" }>): Promise<void> {
    if (!this.isWorkspaceTrusted()) {
      void vscode.window.showWarningMessage("Trust this folder before attaching images to a task.");
      return;
    }
    const folder = activeWorkspaceFolder();
    if (!folder) {
      void vscode.window.showWarningMessage(workspaceScopeRequiredMessage("pasting an image into Alysis Code"));
      return;
    }
    const format = PASTED_IMAGE_TYPES.get(message.mime);
    if (!format) {
      void vscode.window.showWarningMessage("Alysis Code accepts pasted PNG, JPEG, GIF, and WebP images.");
      return;
    }
    let bytes: Buffer;
    try {
      bytes = Buffer.from(message.data, "base64");
    } catch {
      return;
    }
    const magicMatches = format.magic.every((byte, index) => bytes[index] === byte);
    if (bytes.length === 0 || bytes.length > MAX_PASTED_IMAGE_BYTES || !magicMatches) {
      void vscode.window.showWarningMessage("That image could not be read as a supported image file.");
      return;
    }
    const target = await savePastedImage(folder.uri.fsPath, bytes, format.extension);
    await vscode.commands.executeCommand("alysis.backend.session.images.add", target);
  }

  private async refreshImageBasket(sessionId: string | null): Promise<void> {
    const request = ++this.imageBasketRequest;
    const view = this.view;
    if (!sessionId || !this.actions?.getImages) return;
    let result: unknown;
    try {
      result = await this.actions.getImages(sessionId);
    } catch {
      // The initiating action reports errors. Background reads must not interrupt the user.
      return;
    }
    if (request !== this.imageBasketRequest || view !== this.view || !this.viewReady) return;
    if (!result || typeof result !== "object") return;
    const basket = result as { session_id?: unknown; images?: unknown };
    if (basket.session_id !== this.getCockpit().sessionId || !Array.isArray(basket.images)) return;
    const names = basket.images.slice(0, 8).flatMap((entry: unknown) => {
      if (!entry || typeof entry !== "object") return [];
      const image = entry as { relpath?: unknown; path?: unknown };
      const path = typeof image.relpath === "string" ? image.relpath : typeof image.path === "string" ? image.path : "";
      return path ? [redactForDisplay(basename(path.replaceAll("\\", "/"))).slice(0, 120)] : [];
    });
    void this.view?.webview.postMessage({ type: "image.basket", sessionId: basket.session_id, names });
  }

  private disposeViewListeners(): void {
    for (const disposable of this.viewDisposables) {
      disposable.dispose();
    }
    this.viewDisposables = [];
  }
}

function extensionOf(value: string): string {
  const index = value.lastIndexOf(".");
  return index >= 0 ? value.slice(index + 1).toLowerCase() : "";
}

function basename(value: string): string {
  const parts = value.split("/").filter(Boolean);
  return parts[parts.length - 1] || value;
}

/** Only https links leave the webview. command:, file:, and data: URLs are never opened. */
function safeExternalUri(value: string): vscode.Uri | undefined {
  try {
    const parsed = vscode.Uri.parse(value, true);
    return parsed.scheme === "https" ? parsed : undefined;
  } catch {
    return undefined;
  }
}

export function buildStartViewState(
  runtime: CockpitRuntimeSnapshot,
  config: AlysisConfig,
  profiles: ProviderProfileState,
  workspaceTrusted: boolean,
  cockpitState: CockpitState,
  sessionSnapshot: StartViewSessionSnapshot = { sessions: [], activeSessionId: undefined },
  models: ModelsSurfaceState = emptyModelsSurfaceState(),
  lastAcceptedTaskRequestId: string | null = null,
  isBackendActionSupported: (actionId: string) => boolean = () => true
): StartViewState {
  // The profile snapshot store is filled from session.modelInfo, i.e. only once a session exists;
  // before the first task the authoritative name is the models surface's active connection.
  const providerName =
    profiles.activeProfile.trim() ||
    models.activeProfile.trim() ||
    config.provider.trim() ||
    (config.baseUrl.trim() ? "Custom provider" : "Provider");
  const modelName = models.activeModel.trim() || config.defaultModel.trim() || "Choose model";
  const cockpit = cockpitState.cockpit;
  const readiness = providerSelectionReadiness(config, models);
  // The host owns the readiness model; this view only renders it. cliTrusted mirrors the
  // executable-origin guard the runtime snapshot already reports.
  const cockpitReadiness = buildCockpitReadiness({
    runtime,
    workspaceTrusted,
    cliTrusted: runtime.cliOrigin.status !== "blocked",
    provider: readiness
  });
  return {
    mode: sessionModeOf(cockpitState, config),
    providerName,
    modelName,
    engine: engineStatus(runtime),
    workspace: workspaceTrusted
      ? { label: "Folder access", detail: "Trusted for planning and reviewed changes", tone: "ready", action: "workspace" }
      : { label: "Folder access", detail: "Limited until you trust this folder", tone: "attention", action: "workspace" },
    provider: providerStatus(config, profiles, readiness, models),
    recentTasks: buildRecentTasks(sessionSnapshot),
    conversation: buildConversationState(cockpitState),
    actionResults: cockpit.actionResults,
    runtimeEvents: runtime.events,
    commands: buildCommandCatalog(cockpitState, workspaceTrusted, isBackendActionSupported),
    slashCommands: buildSlashCommandCatalog(cockpitState, workspaceTrusted, isBackendActionSupported),
    models,
    personas: buildPersonasState(cockpitState.personas, sessionModeOf(cockpitState, config)),
    lastAcceptedTaskRequestId: lastAcceptedTaskRequestId ?? cockpitState.lastAcceptedTaskRequestId ?? null,
    // Ready now means a task can actually run — engine reachable, folder trusted, provider chosen —
    // not merely that a provider is selected.
    ready: cockpitReadiness.ok,
    readyReason: readinessReason(cockpitReadiness, readiness.reason),
    readiness: cockpitReadiness
  };
}

function readinessReason(readiness: CockpitReadiness, providerReason: string): string {
  if (readiness.ok) {
    return providerReason;
  }
  const first = readiness.blockers[0];
  return first ? `${first.title}. ${first.detail}` : providerReason;
}

/** The live session mode when a session exists, else the configured default the next session gets. */
function sessionModeOf(cockpitState: CockpitState, config: AlysisConfig): AlysisMode {
  const mode = cockpitState.mode;
  return mode === "readonly" || mode === "review" || mode === "auto" ? mode : config.defaultMode;
}

/**
 * Use the actual permissions for the active persona and constraint hints for inactive rows.
 * The bridge does not expose the underlying base mode that may be restored when switching.
 * Tolerates an older host projection without a personas surface by failing closed to hidden.
 */
function buildPersonasState(
  personas: PersonasSurfaceState | undefined,
  sessionMode: AlysisMode
): StartViewPersonasState {
  if (!personas) {
    return { supported: false, reason: null, enabled: false, active: "", activeSource: "", options: [] };
  }
  return {
    supported: personas.supported,
    reason: personas.reason,
    enabled: personas.enabled,
    active: personas.active,
    activeSource: personas.activeSource,
    options: personas.available.map((persona): StartViewPersonaRow => ({
      name: persona.name,
      description: persona.description,
      effectiveMode: persona.name === personas.active ? sessionMode : clampPersonaMode(persona.defaultExecMode, sessionMode),
      // Switching away from Ask can restore earlier base permissions. The list protocol does
      // not expose that restore value, so never predict an inactive persona's resulting mode.
      permissionHint: persona.defaultExecMode === "readonly" ? "Read-only"
        : persona.writeScoped ? "Limited writes, with review"
          : persona.defaultExecMode === "review" ? "Review changes" : "Uses your permissions",
      writeScoped: persona.writeScoped,
      active: persona.name === personas.active
    }))
  };
}


export function isReadyToStart(
  config: AlysisConfig,
  _profiles: ProviderProfileState,
  models: ModelsSurfaceState
): boolean {
  return providerSelectionReadiness(config, models).status === "ready";
}

export function isStartViewMessage(value: unknown): value is StartViewMessage {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  if (record.type === "worktree") {
    return record.action === "new" || record.action === "move" || record.action === "review";
  }
  if (record.type === "ready" || record.type === "task.cancel") {
    return true;
  }
  if (record.type === "task.submit") {
    return typeof record.instruction === "string"
      && record.instruction.trim().length > 0
      && record.instruction.length <= 20000
      && typeof record.mode === "string"
      && ["readonly", "review", "auto"].includes(record.mode)
      && boundedString(record.requestId, 128);
  }
  if (record.type === "approval") {
    return typeof record.approvalId === "string"
      && record.approvalId.trim().length > 0
      && record.approvalId.length <= 1024
      && typeof record.decision === "string"
      && ["allow_once", "allow_for_session", "deny"].includes(record.decision);
  }
  if (record.type === "session") {
    return typeof record.sessionId === "string"
      && record.sessionId.trim().length > 0
      && record.sessionId.length <= 1024
      && typeof record.action === "string"
      && ["resume", "show"].includes(record.action);
  }
  if (record.type === "command") {
    return typeof record.command === "string"
      && START_VIEW_COMMAND_IDS.has(record.command)
      && commandArguments(record.args);
  }
  if (record.type === "clipboard.copy" || record.type === "editor.insert") {
    return boundedString(record.text, MAX_CODE_BLOCK_LENGTH);
  }
  if (record.type === "editor.applyCode") {
    return boundedString(record.text, MAX_CODE_BLOCK_LENGTH)
      && optionalBoundedString(record.language, 40);
  }
  if (record.type === "open.external") {
    return boundedString(record.url, MAX_BASE_URL_LENGTH) && /^https:\/\//i.test(record.url as string);
  }
  if (record.type === "open.file") {
    return boundedString(record.path, 1024) && !(record.path as string).includes("\0");
  }
  if (record.type === "image.attach") {
    return Array.isArray(record.uris)
      && record.uris.length > 0
      && record.uris.length <= 8
      && record.uris.every((uri) => boundedString(uri, 4096));
  }
  if (record.type === "image.paste") {
    return boundedString(record.name, 256)
      && typeof record.mime === "string"
      && PASTED_IMAGE_TYPES.has(record.mime)
      && typeof record.data === "string"
      // base64 grows by 4/3, so the encoded ceiling tracks the byte ceiling.
      && record.data.length > 0
      && record.data.length <= Math.ceil(MAX_PASTED_IMAGE_BYTES / 3) * 4
      && /^[A-Za-z0-9+/=]+$/.test(record.data);
  }
  if (record.type === "cockpit") {
    return isChatPanelMessage(record.message);
  }
  if (record.type === "models.refresh") {
    return true;
  }
  if (record.type === "provider.connect") {
    return boundedString(record.presetKey, MAX_PRESET_KEY_LENGTH)
      && boundedString(record.model, MAX_MODEL_NAME_LENGTH)
      && optionalBoundedString(record.baseUrl, MAX_BASE_URL_LENGTH);
  }
  if (record.type === "provider.use") {
    return boundedString(record.profile, MAX_PRESET_KEY_LENGTH);
  }
  if (record.type === "provider.key") {
    return boundedString(record.profile, MAX_PRESET_KEY_LENGTH)
      && (record.keyAction === "update" || record.keyAction === "forget");
  }
  if (record.type === "model.set") {
    return boundedString(record.model, MAX_MODEL_NAME_LENGTH);
  }
  if (record.type === "mention.search") {
    return typeof record.query === "string"
      && record.query.length <= MAX_MENTION_QUERY_LENGTH
      && typeof record.token === "number"
      && Number.isFinite(record.token);
  }
  return record.type === "action"
    && typeof record.action === "string"
    && [
      "plan",
      "recent",
      "new",
      "engine",
      "workspace",
      "provider",
      "mcp",
      "settings",
      "addContext",
      "addFiles"
    ].includes(record.action);
}

function boundedString(value: unknown, max: number): boolean {
  return typeof value === "string" && value.trim().length > 0 && value.length <= max;
}

/** Command arguments are optional, few, and strings only — never objects the host would trust. */
function commandArguments(value: unknown): boolean {
  if (value === undefined) {
    return true;
  }
  return Array.isArray(value)
    && value.length <= MAX_COMMAND_ARGS
    && value.every((entry) => typeof entry === "string" && entry.length <= MAX_COMMAND_ARG_LENGTH);
}

function optionalBoundedString(value: unknown, max: number): boolean {
  return value === undefined || (typeof value === "string" && value.length <= max);
}

function startViewTaskSignature(message: Extract<StartViewMessage, { type: "task.submit" }>): string {
  return createHash("sha256")
    .update(message.mode)
    .update("\0")
    .update(message.instruction.trim())
    .digest("hex");
}

function buildConversationState(chat: CockpitState): StartViewConversationState {
  const running = ["queued", "starting", "running", "cancellation_requested"].includes(chat.jobStatus);
  return {
    sessionId: chat.sessionId,
    mode: chat.mode,
    jobStatus: chat.jobStatus,
    running,
    items: chat.items.map((item) => {
      // The transcript attaches the classification as item.error and mirrors the readable fields on
      // the item root; metadata is read last so an older host shape still reaches the card renderer.
      const raw = item as unknown as Record<string, unknown>;
      const metadata = (item.metadata ?? {}) as Record<string, unknown>;
      const failure = isRecordLike(raw.error)
        ? raw.error
        : isRecordLike(metadata.error)
          ? metadata.error
          : {};
      return {
        id: item.id,
        kind: item.kind,
        title: item.title ?? item.kind,
        text: item.text,
        status: item.status ?? "",
        toolName: metadataPreview(metadata.toolName ?? metadata.tool_name, 160),
        toolInput: metadataPreview(metadata.toolInput ?? metadata.input_preview, 4_000),
        semanticActivity: typeof metadata.activityId === "string",
        turnLevel: metadata.turnLevel === true,
        approvalId: typeof metadata.approval_id === "string" ? (metadata.approval_id as string) : null,
        allowForSession: metadata.allow_for_session_supported === true,
        approval: item.kind === "approval" ? approvalProjection(metadata) : null,
        errorKind: errorKindOf(failure.kind ?? raw.errorKind ?? metadata.errorKind),
        errorTitle: metadataPreview(failure.title ?? raw.errorTitle ?? metadata.errorTitle, 120) ?? "",
        errorDetail: metadataPreview(failure.detail ?? raw.detail ?? metadata.errorDetail, 400) ?? "",
        errorActions: cockpitActions(failure.actions ?? raw.actions ?? metadata.actions),
        retryAfterSeconds: retrySeconds(failure.retryAfterSeconds ?? raw.retryAfterSeconds)
      };
    })
  };
}

const MAX_APPROVAL_PREVIEW_CHARS = 6_000;
const MAX_APPROVAL_FILES = 24;

/** Bounded, string-only copy of the approval fields the card renders. */
function approvalProjection(metadata: Record<string, unknown>): StartViewApproval {
  const files = Array.isArray(metadata.approval_files)
    ? metadata.approval_files.filter((file): file is string => typeof file === "string").slice(0, MAX_APPROVAL_FILES)
    : [];
  return {
    kind: metadataPreview(metadata.approval_kind, 80) ?? "approval",
    reason: metadataPreview(metadata.approval_reason, 400) ?? "",
    preview: metadataPreview(metadata.approval_preview, MAX_APPROVAL_PREVIEW_CHARS) ?? "",
    command: metadataPreview(metadata.approval_command, 2_000),
    files,
    expiresAt: metadataPreview(metadata.approval_expires_at, 40),
    scope: metadataPreview(metadata.approval_scope, 120) ?? "",
    warning: metadataPreview(metadata.allow_for_session_warning, 300)
  };
}

/** Exhaustive by construction: a new taxonomy kind fails to compile until it is listed here. */
const ERROR_KINDS: Readonly<Record<ChatErrorKind, true>> = {
  auth_invalid: true,
  rate_limited: true,
  quota_exhausted: true,
  network_unreachable: true,
  context_overflow: true,
  runtime_missing: true,
  cli_outdated: true,
  extension_outdated: true,
  workspace_untrusted: true,
  sandbox_unavailable: true,
  checkpoint_unavailable: true,
  cancelled: true,
  interrupted: true,
  unknown: true
};

function errorKindOf(value: unknown): string {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(ERROR_KINDS, value) ? value : "";
}

function retrySeconds(value: unknown): number | null {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric > 0 ? Math.min(Math.round(numeric), 86_400) : null;
}

function isRecordLike(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Only host-authored actions whose command is already on the allowlist reach the webview. */
function cockpitActions(value: unknown): CockpitAction[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const actions: CockpitAction[] = [];
  for (const entry of value.slice(0, 4)) {
    if (!isRecordLike(entry)) {
      continue;
    }
    const label = typeof entry.label === "string" ? entry.label.slice(0, 60) : "";
    const command = typeof entry.command === "string" ? entry.command : "";
    if (!label || !START_VIEW_COMMAND_IDS.has(command)) {
      continue;
    }
    const args = Array.isArray(entry.args)
      ? entry.args.filter((arg): arg is string => typeof arg === "string").slice(0, MAX_COMMAND_ARGS)
      : undefined;
    actions.push({ label, command, ...(args && args.length > 0 ? { args } : {}), primary: entry.primary === true });
  }
  return actions;
}

function metadataPreview(value: unknown, maxLength: number): string | null {
  return typeof value === "string" && value.length > 0 ? value.slice(0, maxLength) : null;
}

const CORE_COMMANDS: readonly Omit<StartViewCommand, "available" | "unavailableReason">[] = [
  command(COMMANDS.newSession, "New task", "Start a clean Alysis Code conversation.", "Start"),
  command(COMMANDS.showHistory, "Task history", "Resume or inspect earlier Alysis Code work.", "Start"),
  command(COMMANDS.showSettings, "Alysis Code settings", "Open the unified settings and tools hub.", "Configure"),
  command(COMMANDS.showModels, "Models and providers", "Connect a provider, store its key, and pick a model.", "Configure"),
  command(COMMANDS.showBrowser, "Browser Cockpit", "Open the public-only browser shared with the Alysis Code agent.", "Tools"),
  command(COMMANDS.cancelCurrentRun, "Stop current run", "Request safe cancellation at the next checkpoint.", "Task", true),
  command(COMMANDS.addSelection, "Add selected code", "Bring the current editor selection into the composer.", "Context"),
  command(COMMANDS.sessionActions, "Current task tools", "Status, usage, context, terminals, trace, and maintenance.", "Task"),
  command(COMMANDS.manageProfiles, "Provider profiles", "Inspect profiles or switch the active provider.", "Configure"),
  command(COMMANDS.configureProvider, "Configure provider", "Store a provider API key safely in SecretStorage.", "Configure", true),
  command(COMMANDS.manageToolsSkills, "Tools and skills", "Inspect, validate, install, or toggle capabilities.", "Configure"),
  command(COMMANDS.manageMcpHooks, "MCP, hooks, and conventions", "Manage servers, project hooks, and rules.", "Configure"),
  command(COMMANDS.manageExtensions, "Alysis Code extensions", "Search, install, enable, or remove packages.", "Configure"),
  command(COMMANDS.runDoctor, "Check setup", "Run the guided local setup check.", "Support"),
  command(COMMANDS.healthDiagnostics, "Troubleshooting", "Open detailed provider, sandbox, and diagnostic checks.", "Support"),
  command(COMMANDS.showBridgeHealth, "Check connection", "Inspect the local CLI and IDE bridge.", "Support"),
  command(COMMANDS.checkForUpdates, "Check for updates", "Check compatibility and available updates.", "Support"),
  command(COMMANDS.locateCli, "Use development CLI override", "Use a local executable instead of the release-verified managed runtime.", "Support"),
  command(COMMANDS.copyCliInstallCommand, "Copy development CLI install command", "Copy the non-production CLI installation command.", "Support"),
  command(COMMANDS.copyCliUpgradeCommand, "Copy development CLI upgrade command", "Copy the non-production CLI upgrade command.", "Support"),
  command(COMMANDS.openSetupGuide, "Open setup guide", "Walk through the first-time setup.", "Support")
];

/**
 * The way out of a broken setup. These stay dispatchable even when the command catalog marks
 * everything unavailable, because a blocker card is exactly the moment the user needs them. The
 * list is an allowlist: the webview can never ask the host to run an arbitrary command.
 */
const RECOVERY_COMMAND_IDS = new Set<string>([
  COMMANDS.locateCli,
  COMMANDS.configureProvider,
  COMMANDS.showModels,
  COMMANDS.runDoctor,
  COMMANDS.showBridgeHealth,
  COMMANDS.openSetupGuide,
  COMMANDS.copyCliInstallCommand,
  COMMANDS.copyCliUpgradeCommand,
  COMMANDS.checkForUpdates,
  COMMANDS.healthDiagnostics,
  // Offered by the context-overflow and cancelled error cards.
  COMMANDS.newSession,
  COMMANDS.sessionCompact,
  TRUST_MANAGEMENT_COMMAND
]);

const START_VIEW_COMMAND_IDS = new Set<string>([
  ...CORE_COMMANDS.map((item) => item.command),
  ...BACKEND_ACTIONS.map((action) => action.commandId),
  ...RECOVERY_COMMAND_IDS
]);

function command(
  id: string,
  title: string,
  description: string,
  category: string,
  mutates = false
): Omit<StartViewCommand, "available" | "unavailableReason"> {
  return { id, command: id, title, description, category, mutates };
}

export function buildCommandCatalog(
  state: CockpitState,
  workspaceTrusted: boolean,
  isBackendActionSupported: (actionId: string) => boolean = () => true
): StartViewCommand[] {
  const activeSession = Boolean(state.sessionId);
  const activeRun = ["queued", "starting", "running", "cancellation_requested"].includes(state.jobStatus);
  const core = CORE_COMMANDS.map((item): StartViewCommand => {
    let unavailableReason = "";
    const featureByCommand: Record<string, CompatibilityFeatureId> = {
      [COMMANDS.checkForUpdates]: "update"
    };
    const feature = featureByCommand[item.command];
    const compatibility = state.cockpit.status?.runtime?.compatibility;
    const group = BACKEND_ACTION_GROUPS.find((candidate) => candidate.commandId === item.command);
    if (feature && compatibility && (!compatibility.protocol.compatible || !compatibility.features[feature]?.supported)) {
      unavailableReason = compatibility.features[feature]?.disabledReason || "The connected agent does not support this action.";
    } else if (group && !BACKEND_ACTIONS.some((action) => action.group === group.id && isBackendActionSupported(action.id))) {
      unavailableReason = "The connected agent does not support these tools.";
    } else if (item.command === COMMANDS.cancelCurrentRun && !activeRun) {
      unavailableReason = "There is no running task to stop.";
    } else if (item.command === COMMANDS.sessionActions && !activeSession) {
      unavailableReason = "Start or resume a task first.";
    }
    return { ...item, available: unavailableReason.length === 0, unavailableReason };
  });
  const backend = BACKEND_ACTIONS.filter((action) => isBackendActionSupported(action.id)).map((action): StartViewCommand => {
    let unavailableReason = "";
    if (action.requiresActiveSession && !activeSession) {
      unavailableReason = "Start or resume a task first.";
    } else if (action.workspaceTrustRequired && !workspaceTrusted) {
      unavailableReason = "Trust this folder before using this action.";
    }
    return {
      id: action.id,
      command: action.commandId,
      title: action.title,
      description: action.description,
      category: BACKEND_ACTION_GROUPS.find((group) => group.id === action.group)?.title ?? "More",
      mutates: action.mayMutate,
      available: unavailableReason.length === 0,
      unavailableReason
    };
  });
  const seen = new Set<string>();
  return [...core, ...backend].filter((item) => {
    if (seen.has(item.command)) {
      return false;
    }
    seen.add(item.command);
    return true;
  });
}

/**
 * The composer's "/" menu lists the slash commands the controller actually routes — the same
 * catalog `/help` prints — filtered to what is dispatchable right now. Aliases are parsed but not
 * listed, so the menu stays short.
 */
export function buildSlashCommandCatalog(
  state: CockpitState,
  workspaceTrusted: boolean,
  isBackendActionSupported: (actionId: string) => boolean = () => true
): StartViewSlashCommand[] {
  const activeSession = Boolean(state.sessionId);
  const activeRun = ["queued", "starting", "running", "cancellation_requested"].includes(state.jobStatus);
  const seen = new Set<string>();
  const rows: StartViewSlashCommand[] = [];
  for (const definition of availableSlashCommands({
    workspaceTrusted,
    activeSession,
    activeRun,
    personasAvailable: state.personas?.supported && state.personas?.enabled,
    compatibility: state.cockpit.status?.runtime?.compatibility,
    isBackendActionSupported
  })) {
    const command = definition.command.trim();
    if (!command || seen.has(command.toLowerCase())) {
      continue;
    }
    seen.add(command.toLowerCase());
    const usage = definition.usage.trim() || command;
    rows.push({
      command,
      title: definition.title,
      description: definition.description,
      usage,
      takesArgs: usage.toLowerCase() !== command.toLowerCase()
    });
  }
  return rows;
}

function buildRecentTasks(snapshot: StartViewSessionSnapshot, now = Date.now()): StartViewRecentTask[] {
  const titles = snapshot.titles ?? {};
  const rows = snapshot.sessions.slice(0, 20).map((session) => {
    const current = snapshot.activeSessionId === session.session_id && !session.closed;
    const job = session.active_job ?? session.last_job;
    const stamp = latestJobTimestamp(job);
    const title = titles[session.session_id]?.trim() || (current ? "Current task" : "Untitled task");
    const parts = [
      modeLabel(session.mode),
      friendlySessionState(job?.status, session.closed),
      ...(stamp ? [relativeTime(stamp, now)] : [])
    ];
    return {
      sessionId: session.session_id,
      title,
      detail: parts.join(" · "),
      current,
      canResume: !current,
      sortKey: current ? Number.POSITIVE_INFINITY : (stamp ?? 0)
    };
  });
  // Current task first, then most recently active.
  rows.sort((left, right) => right.sortKey - left.sortKey);
  return rows.map(({ sortKey: _sortKey, ...row }) => row);
}

/** Epoch millis of the job's latest lifecycle timestamp, or undefined when the bridge sent none. */
function latestJobTimestamp(job: { completed_at?: unknown; updated_at?: unknown; started_at?: unknown; created_at?: unknown } | null | undefined): number | undefined {
  for (const value of [job?.completed_at, job?.updated_at, job?.started_at, job?.created_at]) {
    if (typeof value === "number" && Number.isFinite(value)) {
      // Seconds vs milliseconds: anything before 1973 in ms is really a seconds stamp.
      return value < 100_000_000_000 ? value * 1000 : value;
    }
    if (typeof value === "string" && value.trim()) {
      const parsed = Date.parse(value);
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }
  return undefined;
}

/** "just now", "5m ago", "2h ago", "yesterday", "3d ago", else a short date. */
export function relativeTime(stamp: number, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - stamp) / 1000));
  if (seconds < 45) {
    return "just now";
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return `${hours}h ago`;
  }
  const days = Math.round(hours / 24);
  if (days === 1) {
    return "yesterday";
  }
  if (days < 7) {
    return `${days}d ago`;
  }
  return new Date(stamp).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function engineStatus(runtime: CockpitRuntimeSnapshot): StartViewStatus {
  if (["ready", "active", "approval_needed"].includes(runtime.bridgeProcess.status)) {
    return { label: "Alysis Code engine", detail: "Connected and ready", tone: "ready", action: "engine" };
  }
  switch (runtime.cliHealth.status) {
    case "ok":
      return { label: "Alysis Code engine", detail: "Connected and ready", tone: "ready", action: "engine" };
    case "checking":
      return { label: "Alysis Code engine", detail: "Checking the local connection...", tone: "neutral", action: "engine" };
    case "missing":
    case "unreachable":
      return { label: "Alysis Code engine", detail: "Locate the CLI on this computer", tone: "attention", action: "engine" };
    case "incompatible":
      return { label: "Alysis Code engine", detail: "An update is needed", tone: "attention", action: "engine" };
    case "extension_outdated":
      return { label: "Alysis Code engine", detail: "This extension is older than the CLI", tone: "attention", action: "engine" };
    case "broken":
      return { label: "Alysis Code engine", detail: "Setup needs attention", tone: "attention", action: "engine" };
    case "unknown":
      return { label: "Alysis Code engine", detail: "Not checked yet", tone: "neutral", action: "engine" };
  }
}

function providerStatus(
  config: AlysisConfig,
  profiles: ProviderProfileState,
  readiness: ReturnType<typeof providerSelectionReadiness>,
  models: ModelsSurfaceState
): StartViewStatus {
  const activeProfile = profiles.activeProfile.trim();
  const configuredProvider = config.provider.trim();
  const configuredModel = config.defaultModel.trim();
  if (readiness.status !== "ready") {
    return {
      label: "AI connection",
      detail: readiness.reason,
      tone: readiness.status === "loading" ? "neutral" : "attention",
      action: "provider"
    };
  }
  const modelName = models.activeModel.trim() || configuredModel;
  const providerLabel = providerDisplayName(activeProfile || models.activeProfile.trim() || configuredProvider);
  const detail = providerLabel && modelName
    ? `${providerLabel} · ${modelName}`
    : providerLabel
      ? `Using ${providerLabel}`
      : modelName
        ? `Model: ${modelName}`
        : "Custom connection configured";
  return { label: "AI connection", detail, tone: "ready", action: "provider" };
}

/**
 * Human name for a provider profile slug ("openai-responses" → "OpenAI"), mirroring the webview's
 * chip naming so the Settings row and the composer never disagree.
 */
function providerDisplayName(slug: string): string {
  const value = slug.trim();
  if (!value) {
    return "";
  }
  const lower = value.toLowerCase();
  const known: Array<[RegExp, string]> = [
    [/^openai/, "OpenAI"],
    [/^anthropic|claude/, "Anthropic"],
    [/^google|gemini/, "Google Gemini"],
    [/^azure/, "Azure OpenAI"],
    [/^deepseek/, "DeepSeek"],
    [/^mistral/, "Mistral AI"],
    [/^groq/, "Groq"],
    [/^xai|grok/, "xAI"],
    [/^ollama/, "Ollama"],
    [/^openrouter/, "OpenRouter"],
    [/^perplexity/, "Perplexity"],
    [/^alysis/, "Alysis Code Pro"]
  ];
  for (const [pattern, label] of known) {
    if (pattern.test(lower)) {
      return label;
    }
  }
  return value
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function modeLabel(mode: string): string {
  if (mode === "readonly") {
    return "Read-only";
  }
  if (mode === "auto") {
    return "Auto-approve";
  }
  return "Review changes";
}

function friendlySessionState(status: string | undefined, closed: boolean): string {
  if (closed) {
    return "Closed";
  }
  switch ((status ?? "").toLowerCase()) {
    case "queued":
    case "starting":
    case "running":
      return "Working";
    case "completed":
    case "done":
    case "passed":
      return "Completed";
    case "failed":
      return "Needs attention";
    default:
      return "Ready to continue";
  }
}
