import * as vscode from "vscode";
import { createHash, randomUUID } from "node:crypto";
import path from "node:path";

import { BridgeProfileStartResult, BridgeStartOptions, ProtocolClientError } from "../client/AlysisBridgeClient";
import { AlysisConfig, redactForDisplay } from "../client/CliDiscovery";
import {
  BridgeHealth,
  PersonaChangedPayload,
  PromptQueueListResult,
  ProtocolEventEnvelope,
  SessionModelInfoResult,
  SessionPersonaSetResult,
  SessionPersonasListResult,
  SessionResumeResult,
  AlysisMode,
  isValidPersonaName,
  parsePersonaChangedPayload
} from "../client/AlysisProtocol";
import {
  CompatibilityFeatureId,
  FeatureCompatibilityError,
  assertFeatureCompatible,
  cliHealthStatusForCompatibilityError,
  evaluateBridgeCompatibility,
  featureGateReason
} from "../client/compatibility";
import { COMMANDS } from "../commands/registry";
import type {
  ContextUri,
  ExplicitTerminalSelection,
  IdeContextCollectionRequest,
  IdeContextCollectionResult
} from "../context/IdeContextTypes";
import { AlysisSecretStore } from "../secrets/secretStorage";
import { evaluateProcessExecutionCommand } from "../security/commandGuards";
import { evaluateWorkspaceTrust } from "../security/workspaceTrust";
import { SlashCommandRouter } from "../slash/SlashCommandRouter";
import { AlysisStatusBar } from "../status/statusBar";
import { SessionsViewProvider } from "../views/sessionsView";
import type {
  DiagnosticBaseline,
  DiagnosticsVerificationScope,
  DiagnosticsVerificationReport
} from "../verification/DiagnosticsVerificationGate";
import { activeWorkspaceFolder, activeWorkspaceRoot, workspaceScopeRequiredMessage } from "../workspace/activeWorkspace";
import {
  assertNoDirtyWorkspaceDocuments,
  isDirtyWorkspaceDocumentsError
} from "../workspace/DirtyWorkspaceGuard";
import { ChatErrorContext, ClassifiedChatError, classifyChatError } from "./ChatErrorTaxonomy";
import { buildCockpitReadiness } from "./CockpitReadiness";
import { CockpitRuntimeState } from "./CockpitRuntimeState";
import { ChatState, ChatTranscript } from "./ChatTranscript";
import {
  CockpitState,
  PersonasSurfaceState,
  SessionModelState,
  emptyCockpitState,
  emptyPersonasState,
  emptySessionModelState
} from "./CockpitState";
import { ChatPanelMessage } from "./webviewMessages";

export interface BridgeClientLike {
  on(eventName: string | symbol, listener: (...args: any[]) => void): unknown;
  off?(eventName: string | symbol, listener: (...args: any[]) => void): unknown;
  health?(config: AlysisConfig): Promise<BridgeHealth>;
  supportsMethod?(method: string): boolean;
  supportsEvent?(eventType: string): boolean;
  supportsFeature?(path: readonly string[]): boolean;
  ensureStarted(config: AlysisConfig, options?: { apiKey?: string }): Promise<void>;
  ensureStartedForProfile?(
    config: AlysisConfig,
    options?: { apiKey?: string },
    requiredProfile?: { credentialsRequired?: boolean }
  ): Promise<BridgeProfileStartResult | void>;
  restartIfSafe?(config: AlysisConfig, options?: { apiKey?: string; stripApiKey?: boolean }): Promise<boolean>;
  createSession(params: Record<string, unknown>): Promise<{
    session_id: string;
    workspace_root: string;
    mode: "readonly" | "review" | "auto";
    sandbox_mode?: "strict" | "warn" | "off";
  }>;
  sendChat(
    sessionId: string,
    message: string,
    options?: { idempotency_key?: string; context_blocks?: Record<string, unknown>[] }
  ): Promise<{ session_id: string; job_id: string; status: string }>;
  startRun?(params: Record<string, unknown>): Promise<{ session_id: string; job_id: string; status: string }>;
  sessionSetModel?(sessionId: string, model: string): Promise<unknown>;
  sessionModelInfo?(sessionId: string, options?: { model?: string }): Promise<SessionModelInfoResult>;
  sessionPersonasList?(params: { session_id: string }): Promise<SessionPersonasListResult>;
  sessionPersonaSet?(params: { session_id: string; persona: string }): Promise<SessionPersonaSetResult>;
  sessionResume?(sessionId: string, targetSessionId: string, options?: { emitHistory?: boolean }): Promise<SessionResumeResult>;
  sessionFork?(sessionId: string, sourceSessionId: string): Promise<void>;
  chatQueueList?(
    sessionId: string,
    options?: { states?: string[]; limit?: number; after_sequence?: number }
  ): Promise<PromptQueueListResult>;
  cancelSession(
    sessionId: string,
    options?: { reason?: string; closeWhenIdle?: boolean }
  ): Promise<Record<string, unknown>>;
  respondApproval(params: {
    session_id: string;
    approval_id: string;
    allow: boolean;
    allow_for_session: boolean;
  }): Promise<{
    allow: boolean;
    allow_for_session: boolean;
    allow_for_session_warning: string | null;
  }>;
  jobStatus(jobId: string): Promise<{
    job_id: string;
    session_id: string;
    status: string;
    created_at?: string | null;
    started_at?: string | null;
    completed_at?: string | null;
    exit_code?: number | null;
    error?: string | null;
  }>;
  sessionList(): Promise<{ sessions: any[] }>;
  getEvents(
    sessionId: string,
    afterSequence?: number,
    maxEvents?: number
  ): Promise<{
    events: any[];
    truncated: boolean;
  }>;
  artifactList(sessionId: string): Promise<{
    artifacts: any[];
    truncated: boolean;
  }>;
  artifactRead(sessionId: string, artifactId: string): Promise<any>;
  shutdown(code?: string): Promise<void>;
}

export interface CockpitActions {
  cancel(): Promise<void>;
  // Re-run CLI/bridge health detection against the live config (re-resolving alysis.cliPath and
  // re-running discovery) and apply it to the cockpit. Optional so older wirings still compile.
  reconnect?(): Promise<void>;
  // Switch the active provider profile (profile.use) — keeps Workspace-Trust + confirm + the FE-11
  // result lifecycle by routing through BackendActionController.executeAction. Optional for the same.
  useProfile?(name: string): Promise<void>;
  browserRefresh?(): Promise<void>;
  browserStart?(): Promise<void>;
  browserStartLocal?(): Promise<void>;
  browserSelect?(browserSessionId: string): void;
  browserNavigate?(url: string): Promise<void>;
  browserSnapshot?(kind: "semantic" | "accessibility" | "dom" | "text"): Promise<void>;
  browserScreenshot?(fullPage?: boolean): Promise<void>;
  browserDiagnostics?(): Promise<void>;
  browserClick?(selector: string): Promise<void>;
  browserType?(selector: string, text: string, replace?: boolean): Promise<void>;
  browserClose?(): Promise<void>;
  browserSaveScreenshot?(): Promise<void>;
  browserOwnerChanged?(sessionId: string | undefined): Promise<void>;
  browserDisconnected?(reason: string): Promise<void>;
}

export interface PrimaryChatView {
  reveal(): Promise<void>;
  prefill(text: string): Promise<void>;
}

export interface ChatWorkspaceIdentity {
  readonly root: string;
  readonly scheme: string;
  readonly authority: string;
}

export interface ChatSessionReference {
  readonly sessionId: string;
  readonly workspace: ChatWorkspaceIdentity;
  readonly mode?: AlysisMode;
}

export interface ChatSessionPersistence {
  get(): string | ChatSessionReference | undefined;
  update(reference: ChatSessionReference | undefined): PromiseLike<void> | void;
}

export interface ChatProviderReadiness {
  status: "loading" | "ready" | "incomplete";
  reason: string;
}

export interface IdeContextCollectorLike {
  collect(request?: IdeContextCollectionRequest): Promise<IdeContextCollectionResult>;
  collectFiles?(uris: readonly ContextUri[], workspaceRoot?: string): Promise<IdeContextCollectionResult>;
  collectTerminalExcerpt?(
    selection: ExplicitTerminalSelection,
    workspaceRoot?: string
  ): Promise<IdeContextCollectionResult>;
}

export interface DiagnosticsVerificationGateLike {
  captureBaseline(
    requiredFingerprints?: readonly string[],
    scope?: DiagnosticsVerificationScope
  ): DiagnosticBaseline;
  verifyAfterChanges(
    baseline: DiagnosticBaseline,
    request?: { settleMs?: number; timeoutMs?: number; signal?: AbortSignal }
  ): Promise<DiagnosticsVerificationReport>;
}

interface CollectedIdeContext {
  readonly blocks: Record<string, unknown>[];
  readonly attachedDiagnosticFingerprints: string[];
  readonly pendingAttachmentIds: string[];
}

interface PendingContextBlock {
  readonly id: string;
  readonly block: Record<string, unknown>;
  readonly bytes: number;
}

interface EventReplayBarrier {
  readonly sessionId: string;
  readonly buffered: ProtocolEventEnvelope[];
  overflowed: boolean;
  promise?: Promise<void>;
}

export class ChatController implements vscode.Disposable {
  private workspaceTransition = false;
  private readonly transcript = new ChatTranscript();
  private sessionId: string | undefined;
  private sessionSandbox: { sessionId: string; mode: AlysisConfig["sandboxProfile"] } | undefined;
  private recoverableSessionId: string | undefined;
  private retainedSessionMode: AlysisMode | undefined;
  private permissionChangePending = false;
  /** Immutable workspace owner for the live/recoverable chat session. */
  private sessionWorkspace: ChatWorkspaceIdentity | undefined;
  private sessionStartPromise: Promise<void> | undefined;
  private sessionStartGeneration: number | undefined;
  private runStartPromise: Promise<boolean> | undefined;
  private runStartGeneration: number | undefined;
  /** Generation owning an in-flight follow-up send, so two UI surfaces cannot start two jobs. */
  private chatSendGeneration: number | undefined;
  private sessionIntentGeneration = 0;
  private activeJobId: string | undefined;
  private readonly trackedJobIds = new Set<string>();
  private readonly queuedJobIds = new Set<string>();
  private readonly ownedSessionIds = new Set<string>();
  private readonly ownedJobIds = new Set<string>();
  private readonly cancellationRequestedJobIds = new Set<string>();
  private readonly unsupportedCancellationJobIds = new Set<string>();
  private readonly diagnosticBaselines = new Map<string, DiagnosticBaseline>();
  private readonly pendingApprovalResponses = new Set<string>();
  private readonly pendingContextBlocks: PendingContextBlock[] = [];
  private eventReplayBarrier: EventReplayBarrier | undefined;
  private readonly acceptedTaskRequestIds = new Map<string, string>();
  private readonly pendingTaskRequests = new Map<string, { signature: string; result: Promise<boolean> }>();
  private lastAcceptedTaskRequestId: string | null = null;
  private disposed = false;
  private shutdownPromise: Promise<void> | undefined;
  private persistenceWritePromise: Promise<void> = Promise.resolve();
  private slashRouter: SlashCommandRouter | undefined;
  private providerReadiness: (() => ChatProviderReadiness) | undefined;
  private cockpitProvider: (() => CockpitState["cockpit"]) | undefined;
  private cockpitActions: CockpitActions | undefined;
  private primaryChatView: PrimaryChatView | undefined;
  private readonly stateListeners = new Set<(state: CockpitState) => void>();
  /** Pending trailing flush for coalesced publishes; undefined when nothing is scheduled. */
  private publishTimer: ReturnType<typeof setTimeout> | undefined;
  /**
   * The last projected state. Cleared the moment a publish is requested, so every read after a
   * mutation rebuilds exactly once no matter how many surfaces ask for it during the same frame.
   */
  private cachedCockpitState: CockpitState | undefined;
  /** Cancels an in-flight diagnostics verification when the session or extension goes away. */
  private readonly verificationCancellations = new Map<string, vscode.CancellationTokenSource>();
  /** Session intent that already heard "verification was skipped"; keeps the notice from repeating. */
  private verificationSkipNoticeGeneration: number | undefined;
  private sessionModel: SessionModelState = emptySessionModelState();
  private sessionModelRefreshGeneration = 0;
  private personas: PersonasSurfaceState = emptyPersonasState();
  private personasRefreshGeneration = 0;
  private readonly onBridgeEvent = (event: ProtocolEventEnvelope): void => this.handleBridgeEvent(event);
  private readonly onBridgeStderr = (text: string): void =>
    this.output.appendLine(`bridge stderr: ${redactForDisplay(text)}`);
  private readonly onBridgeError = (error: Error): void => {
    if (!this.disposed) {
      this.showError(error);
    }
  };
  private readonly onBridgeExit = (code: number | null): void => {
    if (!this.disposed) {
      const message = `Alysis Code IDE bridge exited with code ${code ?? "unknown"}.`;
      // The Cockpit timeline keeps every bridge exit, whatever the chat was doing.
      this.runtime?.recordEvent({ severity: "error", source: "bridge", title: "Bridge exited", message, exitCode: code });
      this.handleBridgeDisconnect(message);
    }
  };
  private readonly onBridgeReset = (reason: string): void => {
    if (!this.disposed) {
      // A credential profile upgrade is initiated by the current high-level action. The bridge
      // still has to discard its old protocol sessions, but the message that requested the
      // upgrade remains current and must continue once the replacement process is initialized.
      // That restart is routine plumbing the user did not ask about, so it is logged rather than
      // carded; every other reset is worth a plain-language notice.
      const profileUpgrade = reason === "bridge_profile_upgrade";
      this.output.appendLine(`Bridge restart: ${reason}.`);
      this.runtime?.recordEvent({
        severity: "warning",
        source: "bridge",
        title: "Bridge restarting",
        message: `The Alysis Code engine restarted (${humanizeReason(reason)}).`
      });
      this.handleBridgeDisconnect(
        profileUpgrade
          ? undefined
          : `The Alysis Code engine restarted (${humanizeReason(reason)}). Reconnecting…`,
        !profileUpgrade
      );
    }
  };

  public constructor(
    private readonly bridge: BridgeClientLike,
    private readonly getConfig: () => AlysisConfig,
    private readonly secrets: AlysisSecretStore,
    private readonly statusBar: AlysisStatusBar,
    private readonly sessionsView: SessionsViewProvider,
    private readonly output: vscode.OutputChannel,
    private readonly runtime?: CockpitRuntimeState,
    private readonly sessionPersistence?: ChatSessionPersistence,
    private readonly contextCollector?: IdeContextCollectorLike,
    private readonly diagnosticsVerification?: DiagnosticsVerificationGateLike
  ) {
    const persisted = normalizedSessionReference(this.sessionPersistence?.get());
    this.recoverableSessionId = persisted?.sessionId;
    // Older references do not record permissions. Resume conservatively instead
    // of silently inheriting a potentially more permissive global default.
    this.retainedSessionMode = persisted ? persisted.mode ?? "readonly" : undefined;
    this.sessionWorkspace = persisted?.workspace ?? workspaceIdentityForLegacyReference(persisted?.sessionId);
    if (this.recoverableSessionId && !this.sessionWorkspace) {
      // A legacy id-only reference is ambiguous in a multi-root window. Do not
      // resume it under whichever folder happens to have focus after reload.
      this.recoverableSessionId = undefined;
    }
    if (this.recoverableSessionId && this.sessionWorkspace) {
      this.sessionsView.rememberSession({
        session_id: this.recoverableSessionId, workspace_root: this.sessionWorkspace.root,
        mode: this.retainedSessionMode ?? "readonly", closed: true
      });
    }
    this.bridge.on("event", this.onBridgeEvent);
    this.bridge.on("stderr", this.onBridgeStderr);
    this.bridge.on("error", this.onBridgeError);
    this.bridge.on("exit", this.onBridgeExit);
    this.bridge.on("reset", this.onBridgeReset);
  }

  private handleBridgeDisconnect(message: string | undefined, invalidateSessionIntent = true): void {
    if (this.sessionId) {
      this.recoverableSessionId = this.sessionId;
      this.persistSessionReference(this.sessionId);
    }
    if (invalidateSessionIntent) {
      this.sessionIntentGeneration += 1;
    }
    this.cancelPendingVerifications();
    if (message) {
      this.showNotice(message);
    }
    this.runtime?.setBridgeProcess("stopped", message ?? "The Alysis Code engine is restarting.");
    this.runtime?.setBridgeProtocol("unknown", null);
    this.sessionId = undefined;
    this.activeJobId = undefined;
    this.ownedSessionIds.clear();
    this.ownedJobIds.clear();
    this.trackedJobIds.clear();
    this.queuedJobIds.clear();
    this.cancellationRequestedJobIds.clear();
    this.unsupportedCancellationJobIds.clear();
    this.diagnosticBaselines.clear();
    this.pendingApprovalResponses.clear();
    this.clearSessionModel();
    this.disconnectBrowser(message ?? "The Alysis Code engine is restarting.");
    this.transcript.clearSession("bridge disconnected");
    this.sessionsView.setSessions([]);
    this.statusBar.setIdle();
    // Losing the bridge is terminal for the turn in flight; publish the truth without delay.
    this.publishImmediately();
  }

  /**
   * The primary sidebar is the only Alysis Code chat surface. Revealing it is always allowed — an
   * untrusted folder or a blocked CLI is reported inside it through readiness blockers, which is far
   * more useful than refusing to open the view that explains the problem.
   */
  public async openChat(): Promise<void> {
    await this.primaryChatView?.reveal();
    if (this.recoverableSessionId && !this.sessionId && vscode.workspace.isTrusted
        && evaluateProcessExecutionCommand(this.getConfig(), "openChat").allowed) {
      try {
        await this.ensureBridgeAndSession();
      } catch (error) {
        this.showNotice(`Could not restore the previous task: ${protocolErrorMessage(error)}`);
      }
    }
    await this.replayEvents();
  }

  // Start a fresh session: reveal (or open) the cockpit and reset to the empty onboarding state.
  // A new protocol session is created lazily on the next sent message, mirroring sendUserMessage —
  // this just clears any prior session so the user gets a clean slate. Best-effort closes the live
  // session if one is attached; the reset proceeds regardless so the UI never gets stuck.
  public async newSession(): Promise<void> {
    if (this.workspaceTransition) return;
    if (this.permissionChangePending) {
      this.showNotice("Wait for the permission change to finish before starting a new session.");
      return;
    }
    const gate = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "readonlyPlaceholder");
    if (!gate.allowed) {
      void vscode.window.showWarningMessage(gate.reason ?? "Workspace Trust is required.");
      return;
    }
    const processGate = evaluateProcessExecutionCommand(this.getConfig(), "openChat");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return;
    }
    const sessionToClose = this.sessionId;
    // Invalidate create/run requests already awaiting the backend before doing any
    // asynchronous cleanup. A late response must never resurrect the task that the
    // user just replaced with New Session.
    this.sessionIntentGeneration += 1;
    this.cancelPendingVerifications();
    this.sessionId = undefined;
    this.syncBrowserOwner(undefined);
    this.recoverableSessionId = undefined;
    this.sessionWorkspace = undefined;
    this.retainedSessionMode = undefined;
    this.persistSessionReference(undefined);
    this.activeJobId = undefined;
    this.ownedSessionIds.clear();
    this.ownedJobIds.clear();
    this.trackedJobIds.clear();
    this.queuedJobIds.clear();
    this.cancellationRequestedJobIds.clear();
    this.unsupportedCancellationJobIds.clear();
    this.pendingApprovalResponses.clear();
    this.pendingContextBlocks.splice(0);
    this.clearSessionModel();
    this.transcript.reset("new session");
    this.statusBar.setIdle();
    this.publishImmediately();
    if (sessionToClose) {
      try {
        const cleanup = await this.bridge.cancelSession(sessionToClose, {
          reason: "new_session_requested",
          closeWhenIdle: true
        });
        if (cleanup.status !== "closed" && cleanup.close_when_idle !== true) {
          throw new Error("The bridge did not acknowledge close-after-settle cleanup.");
        }
      } catch {
        const warning =
          "New session opened, but Alysis Code could not confirm cleanup of the previous session. Restart the bridge if its resources remain active.";
        this.output.appendLine(warning);
        this.transcript.addNotice(warning);
        this.publish();
        void vscode.window.showWarningMessage(warning);
        // The reset still succeeds, while the warning keeps cleanup failure visible and actionable.
      }
    }
    await this.primaryChatView?.reveal();
    this.publishImmediately();
    await this.refreshSessionsView();
  }

  public async runTask(): Promise<void> {
    if (!await this.canStartRunTask()) {
      return;
    }
    const prompt = await vscode.window.showInputBox({
      title: "Start a task",
      prompt: "What would you like Alysis Code to do?",
      ignoreFocusOut: true,
      validateInput: (value) => (value.trim().length === 0 ? "Describe what you would like help with." : undefined)
    });
    if (prompt === undefined) {
      return;
    }
    await this.primaryChatView?.reveal();
    await this.startRunTask(prompt.trim());
  }

  public async runTaskWithInstruction(instruction: string, mode: AlysisMode): Promise<boolean> {
    const trimmed = instruction.trim();
    if (!trimmed || !await this.canStartRunTask()) {
      return false;
    }
    return this.startRunTask(trimmed, mode);
  }

  public async submitPrimaryMessage(
    instruction: string,
    mode: AlysisMode,
    requestId?: string
  ): Promise<boolean> {
    return this.runCorrelatedTaskRequest(
      requestId,
      taskRequestSignature(`start:${mode}`, instruction.trim()),
      (correlatedRequestId) =>
      this.submitPrimaryMessageOnce(instruction, mode, correlatedRequestId)
    );
  }

  private async submitPrimaryMessageOnce(
    instruction: string,
    mode: AlysisMode,
    requestId: string | undefined
  ): Promise<boolean> {
    const trimmed = instruction.trim();
    if (!trimmed) {
      return false;
    }
    if (await this.tryHandleSlashCommand(trimmed)) {
      return true;
    }
    if (!await this.canStartRunTask()) {
      return false;
    }
    if (this.sessionId || this.recoverableSessionId) {
      return this.sendUserMessage(trimmed, false, requestId);
    }
    return this.startRunTask(trimmed, mode, requestId);
  }

  public async addSelectionToTask(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      void vscode.window.showInformationMessage("Open a file and select the text you want to add to Alysis Code.");
      return;
    }
    if (editor.selection.isEmpty) {
      void vscode.window.showInformationMessage("Select some text first, then choose Add Selection to Alysis Code.");
      return;
    }
    if (!this.contextCollector || !vscode.workspace.isTrusted) {
      void vscode.window.showWarningMessage("Workspace Trust is required before editor context can be attached.");
      return;
    }
    const workspace = this.sessionWorkspace?.root ?? activeWorkspaceRoot();
    if (!workspace) {
      void vscode.window.showWarningMessage(workspaceScopeRequiredMessage("attaching editor context"));
      return;
    }
    const result = await this.contextCollector.collect({
      workspaceRoot: workspace,
      includeSelection: true,
      includeOpenEditors: false,
      includeDiagnostics: false,
      includeGitDiff: "none",
      includeTerminalSelection: false,
      includeDocumentSymbols: true,
      includeDefinitions: true,
      includeTypeDefinitions: true,
      includeImplementations: true,
      includeHover: true,
      includeReferences: true,
      includeIncomingCalls: true,
      includeOutgoingCalls: true
    });
    await this.attachCollectedContext(result, "selection and language context", "Help me with the attached editor context.");
  }

  public async chooseContextToAttach(): Promise<void> {
    const picked = await vscode.window.showQuickPick(
      [
        { label: "Editor selection", detail: "Attach the current selection plus bounded language-service context", id: "selection" },
        { label: "Copied terminal excerpt", detail: "Attach terminal text you explicitly copied to the clipboard", id: "terminal" }
      ],
      { title: "Add context to Alysis Code", ignoreFocusOut: true }
    );
    if (picked?.id === "selection") {
      await this.addSelectionToTask();
    } else if (picked?.id === "terminal") {
      await this.addTerminalExcerptToTask();
    }
  }

  public async addFilesToTask(): Promise<void> {
    if (!this.contextCollector?.collectFiles || !vscode.workspace.isTrusted) {
      void vscode.window.showWarningMessage("Workspace Trust is required before files can be attached.");
      return;
    }
    const workspaceFolder = this.sessionWorkspace
      ? workspaceFolderForIdentity(this.sessionWorkspace)
      : activeWorkspaceFolder();
    if (!workspaceFolder) {
      void vscode.window.showWarningMessage(
        this.sessionWorkspace
          ? "Reopen the workspace folder owned by this Alysis Code session before attaching files."
          : workspaceScopeRequiredMessage("attaching files")
      );
      return;
    }
    const selected = await vscode.window.showOpenDialog({
      canSelectFiles: true,
      canSelectFolders: false,
      canSelectMany: true,
      defaultUri: workspaceFolder.uri,
      openLabel: "Attach to Alysis Code",
      title: "Choose workspace files to attach"
    });
    if (!selected?.length) {
      return;
    }
    const result = await this.contextCollector.collectFiles(selected.slice(0, 20), workspaceFolder.uri.fsPath);
    await this.attachCollectedContext(result, "file", "Use the attached workspace files as context.");
  }

  public async addTerminalExcerptToTask(): Promise<void> {
    if (!this.contextCollector?.collectTerminalExcerpt || !vscode.workspace.isTrusted) {
      void vscode.window.showWarningMessage("Workspace Trust is required before terminal text can be attached.");
      return;
    }
    const workspace = this.sessionWorkspace?.root ?? activeWorkspaceRoot();
    if (!workspace) {
      void vscode.window.showWarningMessage(workspaceScopeRequiredMessage("attaching terminal context"));
      return;
    }
    const approved = await vscode.window.showWarningMessage(
      "Alysis Code will read the terminal excerpt you explicitly copied to the clipboard, redact common secrets, and attach it to the next message.",
      { modal: true },
      "Read Clipboard"
    );
    if (approved !== "Read Clipboard") {
      return;
    }
    const text = await vscode.env.clipboard.readText();
    if (!text.trim()) {
      void vscode.window.showInformationMessage("Copy a terminal selection first, then try again.");
      return;
    }
    const result = await this.contextCollector.collectTerminalExcerpt({ text }, workspace);
    await this.attachCollectedContext(result, "terminal excerpt", "Help me with the attached terminal excerpt.");
  }

  public async attachContextBlock(block: Record<string, unknown>, label = "context"): Promise<void> {
    if (block.type !== "past_session") {
      throw new Error("Only validated past-task context can be attached through this action.");
    }
    if (!this.queueContextBlocks([block])) {
      void vscode.window.showWarningMessage(`The ${label} could not be attached because the context limit was reached.`);
      return;
    }
    await this.prefillPrimaryChat("Use the attached past-task context.");
    void vscode.window.showInformationMessage("Past-task context attached to the next message.");
  }

  public async cancelCurrentRun(): Promise<void> {
    if (!this.sessionId) {
      void vscode.window.showInformationMessage("No active Alysis Code session is attached.");
      return;
    }
    if (this.activeJobId && this.cancellationRequestedJobIds.has(this.activeJobId)) {
      void vscode.window.showInformationMessage("Cancellation is already requested for the active Alysis Code job.");
      return;
    }
    if (this.activeJobId && this.unsupportedCancellationJobIds.has(this.activeJobId)) {
      void vscode.window.showWarningMessage(
        "The active Alysis Code job is still running; active cancellation was already reported as unsupported or non-cancellable by the backend."
      );
      return;
    }
    if (this.activeJobId && this.bridge.supportsFeature?.(["cancellation", "active_jobs"]) !== true) {
      const message =
        "The active Alysis Code job cannot be cancelled because this bridge does not advertise cooperative active-job cancellation.";
      this.unsupportedCancellationJobIds.add(this.activeJobId);
      this.transcript.addNotice(message);
      this.publish();
      void vscode.window.showWarningMessage(message);
      return;
    }
    try {
      const result = await this.bridge.cancelSession(this.sessionId);
      const status = typeof result.status === "string" ? result.status : "closed";
      if (status === "cancellation_requested") {
        if (this.activeJobId) {
          this.cancellationRequestedJobIds.add(this.activeJobId);
        }
        // The "stopping" state is carried by the job status (the composer shows Stopping…); the
        // terminal "Task stopped" notice is added once the job actually reaches cancelled.
        if (this.activeJobId) {
          this.transcript.setJobStatus({
            job_id: this.activeJobId,
            session_id: this.sessionId,
            status: "cancellation_requested",
            state: "cancellation_requested",
            cancellable: false
          });
        }
        this.runtime?.setBridgeProcess("ready", "Cancellation requested.");
        // Cancellation is terminal for the current turn: flush now instead of at frame cadence.
        this.publishImmediately();
        await this.refreshSessionsView();
        return;
      }
      if (status === "non_cancellable") {
        if (this.activeJobId) {
          this.unsupportedCancellationJobIds.add(this.activeJobId);
        }
        this.transcript.addNotice("The active Alysis Code job cannot be cancelled by the backend.");
        this.publishImmediately();
        await this.refreshSessionsView();
        return;
      }
      this.transcript.addNotice(status === "closed" ? "Task stopped." : `Session ${humanizeReason(status)}.`);
      this.sessionId = undefined;
      this.syncBrowserOwner(undefined);
      this.recoverableSessionId = undefined;
      this.sessionWorkspace = undefined;
      this.retainedSessionMode = undefined;
      this.persistSessionReference(undefined);
      this.activeJobId = undefined;
      this.ownedSessionIds.clear();
      this.ownedJobIds.clear();
      this.trackedJobIds.clear();
      this.queuedJobIds.clear();
      this.cancellationRequestedJobIds.clear();
      this.unsupportedCancellationJobIds.clear();
      this.pendingApprovalResponses.clear();
      this.clearSessionModel();
      this.transcript.clearSession(status);
      this.statusBar.setIdle();
      this.publishImmediately();
      await this.refreshSessionsView();
    } catch (error) {
      const message = protocolErrorMessage(error);
      if (this.activeJobId && isCancellationUnsupportedError(error)) {
        this.unsupportedCancellationJobIds.add(this.activeJobId);
      }
      this.transcript.addNotice(message);
      this.publishImmediately();
      void vscode.window.showWarningMessage(message);
    }
  }

  public dispose(): void {
    void this.shutdown();
  }

  public shutdown(options: { shutdownBridge?: boolean } = { shutdownBridge: true }): Promise<void> {
    if (!this.shutdownPromise) {
      this.shutdownPromise = this.performShutdown(options);
    }
    return this.shutdownPromise;
  }

  private async performShutdown(options: { shutdownBridge?: boolean }): Promise<void> {
    this.disposed = true;
    this.sessionIntentGeneration += 1;
    this.detachBridgeListeners();
    this.cancelPendingVerifications();
    await this.denyPendingApprovals();
    await this.persistenceWritePromise;
    this.cancelScheduledPublish();
    this.stateListeners.clear();
    if (options.shutdownBridge !== false) {
      await this.bridge.shutdown("bridge_stopped");
    }
  }

  public testState(): {
    workspaceRoot: string | null;
    sessionId: string | null;
    activeJobId: string | null;
    items: Array<{ kind: string; title: string | undefined; text: string }>;
    readiness: CockpitState["readiness"];
    cockpit: CockpitState["cockpit"];
  } {
    const state = this.cockpitState();
    return {
      sessionId: this.sessionId ?? null,
      workspaceRoot: this.sessionWorkspace?.root ?? null,
      activeJobId: this.activeJobId ?? null,
      items: state.items.map((item) => ({ kind: item.kind, title: item.title, text: item.text })),
      readiness: state.readiness,
      cockpit: state.cockpit
    };
  }

  public setSlashCommandRouter(router: SlashCommandRouter): void {
    this.slashRouter = router;
  }

  /** Host-owned provider catalog gate shared by the sidebar and Details composers. */
  public setProviderReadiness(readiness: () => ChatProviderReadiness): void {
    this.providerReadiness = readiness;
  }

  public setCockpitProvider(provider: () => CockpitState["cockpit"]): void {
    this.cockpitProvider = provider;
    this.publish();
  }

  public setCockpitActions(actions: CockpitActions): void {
    this.cockpitActions = actions;
    this.syncBrowserOwner(this.sessionId);
  }

  public setPrimaryChatView(view: PrimaryChatView): void {
    this.primaryChatView = view;
  }

  public onDidChangeState(listener: (state: CockpitState) => void): vscode.Disposable {
    this.stateListeners.add(listener);
    return { dispose: () => this.stateListeners.delete(listener) };
  }

  public conversationState(): ChatState {
    return this.transcript.state();
  }

  /**
   * The primary sidebar is the complete Alysis Code cockpit and its only chat surface. The projection
   * stays owned by ChatController so every consumer (sidebar, slash catalog, tests) reads the exact
   * same redacted state, readiness gate, and capability gates — and rebuilds it at most once per
   * mutation rather than once per caller.
   */
  public primaryCockpitState(): CockpitState {
    return this.cockpitState();
  }

  /** Route one validated sidebar cockpit action through the controller. */
  public async handlePrimaryCockpitAction(message: ChatPanelMessage): Promise<void> {
    await this.handleCockpitMessage(message);
  }

  public async respondToPrimaryApproval(
    approvalId: string,
    decision: "allow_once" | "allow_for_session" | "deny"
  ): Promise<void> {
    await this.respondToApproval(approvalId, decision);
  }

  public refreshCockpit(): void {
    this.publish();
  }

  public async testSubmit(text: string): Promise<void> {
    await this.sendUserMessage(text);
  }

  public hasActiveJob(): boolean {
    return this.activeJobId !== undefined;
  }

  /** Replay explicitly selected retained context and keep the visible transcript/queue in sync. */
  public async resumeRetainedContext(sessionId: string, targetSessionId: string): Promise<SessionResumeResult> {
    if (!vscode.workspace.isTrusted || this.workspaceTransitionBlocked() || this.transcript.pendingApprovals().length > 0) {
      throw new Error("Finish or stop the current task and resolve approvals before restoring a conversation.");
    }
    if (this.sessionId !== sessionId || !this.bridge.sessionResume) {
      throw new Error("The active conversation changed. Open the task again before restoring its context.");
    }
    this.workspaceTransition = true;
    const generation = this.sessionIntentGeneration;
    try {
      const result = await this.bridge.sessionResume(sessionId, targetSessionId, { emitHistory: true });
      this.assertSessionIntentCurrent(generation);
      if (this.sessionId !== sessionId) throw sessionStartSupersededError();
      await this.replayEvents();
      await this.adoptRecoveredChatQueue(sessionId, result);
      const previousTitle = this.sessionsView.titleFor?.(targetSessionId);
      if (previousTitle) this.sessionsView.rememberTitle?.(sessionId, previousTitle);
      await this.refreshSessionsView();
      return result;
    } finally {
      this.workspaceTransition = false;
      this.publishImmediately();
    }
  }

  public workspaceTransitionBlocked(): boolean {
    return this.workspaceTransition || this.permissionChangePending || this.hasActiveJob() || Boolean(this.sessionStartPromise || this.runStartPromise)
      || this.chatSendGeneration !== undefined || this.queuedJobIds.size > 0;
  }

  public canMoveToWorktree(): boolean {
    return Boolean(this.sessionId && this.bridge.sessionFork && this.bridge.supportsMethod?.("session.fork") !== false);
  }

  /** Hold the send gate across the Git copy and session adoption, including host awaits. */
  public async withWorkspaceTransition(operation: () => Promise<void>): Promise<void> {
    if (this.workspaceTransitionBlocked()) throw new Error("Finish or stop the current task before changing worktrees.");
    this.workspaceTransition = true;
    try { await operation(); } finally { this.workspaceTransition = false; this.publishImmediately(); }
  }

  public async prepareWorktreeHandoff(root: string): Promise<ChatSessionReference> {
    if (!this.workspaceTransition || !vscode.workspace.isTrusted) throw new Error("A trusted workspace transition is required.");
    if (!this.canMoveToWorktree()) throw new Error("Upgrade the Alysis Code CLI to move conversations to worktrees.");
    const source = this.sessionId!;
    const config = this.getConfig();
    const created = await this.bridge.createSession({ workspace: root, mode: this.currentSessionMode(),
      sandbox_profile: config.sandboxProfile,
      ...(this.sessionModel.model || config.defaultModel ? { model: this.sessionModel.model || config.defaultModel } : {}),
      ...(config.baseUrl ? { base_url: config.baseUrl } : {}) });
    try {
      await this.bridge.sessionFork!(created.session_id, source);
      // Close the destination's writer before another VS Code window reopens its retained log.
      const closed = await this.bridge.cancelSession(created.session_id, { reason: "worktree_handoff", closeWhenIdle: true });
      if (closed.status !== "closed") throw new Error("The worktree conversation could not be saved and closed.");
      return { sessionId: created.session_id, workspace: { root, scheme: "file", authority: "" }, mode: this.currentSessionMode() };
    } catch (error) {
      await this.closeSupersededSession(created.session_id);
      throw error;
    }
  }
  public hasSession(): boolean {
    return this.sessionId !== undefined;
  }

  public activeSessionId(): string | undefined {
    return this.sessionId;
  }

  public activeSessionWorkspaceRoot(): string | undefined {
    return this.sessionWorkspace?.root;
  }

  public async ensureLiveSession(): Promise<string> {
    await this.ensureBridgeAndSession();
    if (!this.sessionId) {
      throw new Error("Alysis Code session was not created.");
    }
    return this.sessionId;
  }

  /** Keep sends and session changes out of a credential restart/provider switch. */
  public async withProviderTransition(
    prepare: () => Promise<void>,
    apply: (sessionId: string | undefined) => Promise<void>
  ): Promise<void> {
    if (!vscode.workspace.isTrusted) throw new Error("Trust this workspace before switching providers.");
    if (this.workspaceTransitionBlocked()) throw new Error("Finish or stop the current task before switching providers.");
    const hadConversation = Boolean(this.sessionId || this.recoverableSessionId);
    this.workspaceTransition = true;
    try {
      await prepare();
      const sessionId = hadConversation ? await this.ensureLiveSession() : undefined;
      await apply(sessionId);
      if (sessionId) await this.refreshSessionModelInfo();
    } finally {
      this.workspaceTransition = false;
      this.publishImmediately();
    }
  }

  private async handleCockpitMessage(message: ChatPanelMessage): Promise<void> {
    switch (message.type) {
      case "submit":
        await this.runCorrelatedTaskRequest(
          message.requestId,
          taskRequestSignature("cockpit:chat", message.text),
          (requestId) => this.sendUserMessage(message.text, true, requestId)
        );
        return;
      case "slash.quickAction":
        await this.runCorrelatedTaskRequest(
          message.requestId,
          taskRequestSignature("cockpit:slash", message.command),
          (requestId) => this.sendUserMessage(message.command, true, requestId)
        );
        return;
      case "cancel":
        if (this.cockpitActions) {
          await this.cockpitActions.cancel();
        } else {
          await this.cancelCurrentRun();
        }
        return;
      case "approval":
        await this.respondToApproval(message.approvalId, message.decision);
        return;
      case "artifact.refresh":
        await this.refreshArtifacts();
        return;
      case "artifact.open":
        await this.openArtifact(message.artifactId);
        return;
      case "command.openSetupGuide":
        await vscode.commands.executeCommand(COMMANDS.openSetupGuide);
        return;
      case "command.openOutput":
        this.output.show(true);
        return;
      case "command.setCliPath":
        await vscode.commands.executeCommand(COMMANDS.locateCli);
        return;
      case "cockpit.retryStatus":
        await vscode.commands.executeCommand(COMMANDS.showBridgeHealth);
        return;
      case "mode.set":
        await this.handleModeSet(message.mode);
        return;
      case "session.model.set":
        await this.handleSessionModelSet(message.model);
        return;
      case "persona.set":
        await this.setPersona(message.name);
        return;
      case "bridge.restart":
        await this.restartBridgeFromCockpit();
        return;
      case "profile.use":
        await this.cockpitActions?.useProfile?.(message.name);
        return;
      case "browser.refresh":
        await this.cockpitActions?.browserRefresh?.();
        return;
      case "browser.start":
        await this.cockpitActions?.browserStart?.();
        return;
      case "browser.startLocal":
        await this.cockpitActions?.browserStartLocal?.();
        return;
      case "browser.select":
        this.cockpitActions?.browserSelect?.(message.browserSessionId);
        return;
      case "browser.navigate":
        await this.cockpitActions?.browserNavigate?.(message.url);
        return;
      case "browser.snapshot":
        await this.cockpitActions?.browserSnapshot?.(message.kind);
        return;
      case "browser.screenshot":
        await this.cockpitActions?.browserScreenshot?.(message.fullPage);
        return;
      case "browser.diagnostics":
        await this.cockpitActions?.browserDiagnostics?.();
        return;
      case "browser.click":
        await this.cockpitActions?.browserClick?.(message.selector);
        return;
      case "browser.type":
        await this.cockpitActions?.browserType?.(message.selector, message.text, message.replace);
        return;
      case "browser.close":
        await this.cockpitActions?.browserClose?.();
        return;
      case "browser.screenshot.save":
        await this.cockpitActions?.browserSaveScreenshot?.();
        return;
    }
  }

  private runCorrelatedTaskRequest(
    requestId: string | undefined,
    signature: string,
    submit: (requestId: string | undefined) => Promise<boolean>
  ): Promise<boolean> {
    const normalized = normalizedTaskRequestId(requestId);
    if (!normalized) {
      return submit(undefined);
    }
    const acceptedSignature = this.acceptedTaskRequestIds.get(normalized);
    if (acceptedSignature) {
      return Promise.resolve(acceptedSignature === signature);
    }
    const existing = this.pendingTaskRequests.get(normalized);
    if (existing) {
      return existing.signature === signature ? existing.result : Promise.resolve(false);
    }
    const pending = submit(normalized).then((accepted) => {
      if (accepted) {
        this.rememberAcceptedTaskRequest(normalized, signature);
      }
      return accepted;
    }).finally(() => {
      if (this.pendingTaskRequests.get(normalized)?.result === pending) {
        this.pendingTaskRequests.delete(normalized);
      }
    });
    this.pendingTaskRequests.set(normalized, { signature, result: pending });
    return pending;
  }

  private rememberAcceptedTaskRequest(requestId: string, signature: string): void {
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
    this.publish();
  }

  private async handleModeSet(mode: "readonly" | "review" | "auto" | "fullaccess"): Promise<void> {
    if (mode === "fullaccess") {
      this.showNotice("Fullaccess mode is advertised by the Alysis Code CLI but is not yet enabled in the VS Code extension.");
      return;
    }
    if (!this.slashRouter) {
      return;
    }
    const hadSession = Boolean(this.sessionId);
    const outcome = await this.slashRouter.execute(`/permissions ${mode}`);
    // The composer's own switch is the feedback for a default-mode change: the strip flips
    // immediately. Carding "default mode set to review" on the empty start surface would open a
    // conversation with nothing in it but that notice. Warnings/errors are still shown.
    if (!hadSession && outcome.severity === "info") {
      this.publish();
      return;
    }
    this.showNotice(outcome.notice);
  }

  /** Serialize permission changes against task startup and workspace handoffs. */
  public async changePermissions(mode: AlysisMode, apply: () => Promise<string | void>): Promise<string | void> {
    if (this.workspaceTransitionBlocked()) {
      throw new Error("Finish or stop the current task before changing permissions.");
    }
    if (!vscode.workspace.isTrusted && mode !== "readonly") {
      throw new Error(untrustedModeMessage(mode));
    }
    this.permissionChangePending = true;
    try {
      // A reload retains a conversation even before it has a live bridge id.
      // Change that conversation's permissions, not the default for new tasks.
      if (this.recoverableSessionId && !this.sessionId) {
        await this.ensureBridgeAndSession();
      }
      return await apply();
    } finally {
      this.permissionChangePending = false;
      this.publishImmediately();
    }
  }

  public async refreshSessionModelInfo(): Promise<void> {
    const sessionId = this.sessionId;
    const modelInfoSupported = this.supportsBridgeMethod("session.modelInfo") && typeof this.bridge.sessionModelInfo === "function";
    const switchSupported = this.supportsBridgeMethod("session.setModel") && typeof this.bridge.sessionSetModel === "function";
    const generation = ++this.sessionModelRefreshGeneration;
    if (!sessionId) {
      this.sessionModel = { ...emptySessionModelState(), supported: modelInfoSupported, switchSupported };
      this.publish();
      return;
    }
    if (!modelInfoSupported || !this.bridge.sessionModelInfo) {
      this.sessionModel = {
        ...emptySessionModelState(),
        supported: false,
        switchSupported,
        sessionId,
        error: "Live model details are unavailable from this Alysis Code bridge."
      };
      this.publish();
      return;
    }
    try {
      const info = await this.bridge.sessionModelInfo(sessionId);
      if (generation !== this.sessionModelRefreshGeneration || this.sessionId !== sessionId) {
        return;
      }
      this.sessionModel = sessionModelFromInfo(info, switchSupported);
      this.publish();
    } catch (error) {
      if (generation !== this.sessionModelRefreshGeneration || this.sessionId !== sessionId) {
        return;
      }
      this.sessionModel = {
        ...emptySessionModelState(),
        supported: true,
        switchSupported,
        sessionId,
        error: redactForDisplay(protocolErrorMessage(error))
      };
      this.publish();
    }
  }

  private async handleSessionModelSet(model: string): Promise<void> {
    const normalized = redactForDisplay(model.trim());
    if (!this.sessionId) {
      this.showNotice("Start an Alysis Code chat session before switching the live model.");
      return;
    }
    if (!normalized) {
      return;
    }
    if (!this.supportsBridgeMethod("session.setModel") || !this.bridge.sessionSetModel) {
      this.showNotice("Live model switching is unavailable from this bridge. Configure Provider changes the default profile model.");
      return;
    }
    const sessionId = this.sessionId;
    const intentGeneration = this.sessionIntentGeneration;
    try {
      await this.bridge.sessionSetModel(sessionId, normalized);
      if (
        this.disposed ||
        this.sessionIntentGeneration !== intentGeneration ||
        this.sessionId !== sessionId
      ) {
        return;
      }
      this.showNotice(`Session model set to ${normalized}.`);
      await this.refreshSessionModelInfo();
    } catch (error) {
      if (
        this.disposed ||
        this.sessionIntentGeneration !== intentGeneration ||
        this.sessionId !== sessionId
      ) {
        return;
      }
      this.showError(error);
    }
  }

  /**
   * The persona surface lights up only when the CLI advertises BOTH persona methods: a list without
   * a setter (or the reverse) is a control that cannot keep its promise. Personas ride the clamp
   * rule CLI-side — a persona can only narrow the session's execution mode, never widen it — so,
   * exactly like session.setModel, no Workspace-Trust gate is added here.
   */
  private personasSupported(): boolean {
    return (
      this.supportsBridgeMethod("session.personas.list") &&
      this.supportsBridgeMethod("session.persona.set") &&
      typeof this.bridge.sessionPersonasList === "function" &&
      typeof this.bridge.sessionPersonaSet === "function"
    );
  }

  /** The standard capability affordance for an older CLI, sourced from the compatibility snapshot. */
  private personaCapabilityReason(): string {
    const feature = this.runtime?.snapshot().compatibility.features.personas;
    if (feature && !feature.supported) {
      return featureGateReason(feature);
    }
    return "Needs a newer Alysis Code CLI — session.personas.list, session.persona.set";
  }

  public async refreshPersonas(): Promise<void> {
    const sessionId = this.sessionId;
    const generation = ++this.personasRefreshGeneration;
    if (!this.personasSupported() || !this.bridge.sessionPersonasList) {
      this.personas = { ...emptyPersonasState(), reason: this.personaCapabilityReason() };
      this.publish();
      return;
    }
    if (!sessionId) {
      this.personas = { ...emptyPersonasState(), supported: true };
      this.publish();
      return;
    }
    try {
      const result = await this.bridge.sessionPersonasList({ session_id: sessionId });
      if (generation !== this.personasRefreshGeneration || this.sessionId !== sessionId) {
        return;
      }
      this.personas = personasSurfaceFromList(result);
      this.publish();
    } catch (error) {
      if (generation !== this.personasRefreshGeneration || this.sessionId !== sessionId) {
        return;
      }
      // A failed list is not a capability gap: keep the control present but empty, and log why.
      this.personas = { ...emptyPersonasState(), supported: true };
      this.output.appendLine(`persona list failed: ${protocolErrorMessage(error)}`);
      this.publish();
    }
  }

  private clearPersonas(): void {
    this.personasRefreshGeneration += 1;
    this.personas = this.personas.supported
      ? { ...emptyPersonasState(), supported: true }
      : { ...emptyPersonasState(), reason: this.personas.reason };
  }

  /** Webview path: apply the persona and surface the outcome as a transcript notice. */
  public async setPersona(name: string): Promise<void> {
    const outcome = await this.applyPersona(name);
    if (outcome.notice) {
      this.showNotice(outcome.notice);
    }
  }

  /**
   * Slash path. `/persona <name>` sets directly; bare `/persona` opens a picker of the loaded
   * personas (each row shows the mode the clamp would land on) and falls back to a plain listing
   * when no QuickPick host is available.
   */
  public async routeSlashPersona(name?: string): Promise<string> {
    const requested = name?.trim() ?? "";
    if (requested) {
      return (await this.applyPersona(requested)).notice;
    }
    if (!this.personasSupported()) {
      return this.personas.reason ?? this.personaCapabilityReason();
    }
    if (!this.sessionId) {
      return "Start an Alysis Code chat session before switching personas.";
    }
    await this.refreshPersonas();
    if (!this.personas.enabled || this.personas.available.length === 0) {
      return "Personas are not enabled for this Alysis Code session.";
    }
    const quickPick = (vscode.window as { showQuickPick?: unknown }).showQuickPick;
    if (typeof quickPick !== "function") {
      const rows = this.personas.available.map((persona) =>
        `${persona.name}${persona.name === this.personas.active ? " (active)" : ""} — ${persona.description}`
      );
      return `Active persona: ${this.personas.active || "code"}. Available: ${rows.join(", ")}.`;
    }
    const picked = await vscode.window.showQuickPick(
      this.personas.available.map((persona) => ({
        label: persona.name,
        description: persona.name === this.personas.active ? `${this.currentSessionMode()} · active`
          : persona.defaultExecMode === "readonly" ? "Read-only"
            : persona.writeScoped ? "Limited writes, with review" : "Uses your permissions",
        detail: persona.description
      })),
      { placeHolder: "Choose a persona (a persona can narrow what the agent may do, never widen it)" }
    );
    if (!picked) {
      return "";
    }
    return (await this.applyPersona(picked.label)).notice;
  }

  private async applyPersona(rawName: string): Promise<{ notice: string }> {
    const name = rawName.trim();
    if (!isValidPersonaName(name)) {
      return { notice: "Persona names use letters, numbers, dots, dashes, and underscores (64 characters max)." };
    }
    if (!this.personasSupported() || !this.bridge.sessionPersonaSet) {
      return { notice: this.personas.reason ?? this.personaCapabilityReason() };
    }
    if (!this.sessionId) {
      return { notice: "Start an Alysis Code chat session before switching personas." };
    }
    // Personas switch between tasks. Never queue a persona change behind a running job — the CLI
    // would reject it with persona_change_busy anyway, so fail fast with the same guidance.
    if (this.hasActiveJob()) {
      return { notice: "Finish or stop the current task first." };
    }
    const sessionId = this.sessionId;
    const intentGeneration = this.sessionIntentGeneration;
    try {
      const result = await this.bridge.sessionPersonaSet({ session_id: sessionId, persona: name });
      if (this.disposed || this.sessionIntentGeneration !== intentGeneration || this.sessionId !== sessionId) {
        return { notice: "" };
      }
      this.personas = { ...this.personas, active: result.persona, activeSource: "user" };
      this.transcript.setSessionMode(result.effective_mode);
      this.persistSessionReference(sessionId);
      this.publish();
      // Sticky persona models can swap the live model; refresh both surfaces in the background.
      void this.refreshPersonas();
      void this.refreshSessionModelInfo();
      return {
        notice: result.changed
          ? `Persona set: ${result.persona} (${result.effective_mode}).`
          : `Persona ${result.persona} is already active (${result.effective_mode}).`
      };
    } catch (error) {
      if (this.disposed || this.sessionIntentGeneration !== intentGeneration || this.sessionId !== sessionId) {
        return { notice: "" };
      }
      const code = error instanceof ProtocolClientError ? error.code : "";
      switch (code) {
        case "persona_change_busy":
          return { notice: "Finish or stop the current task first." };
        case "persona_modes_disabled":
          this.personas = { ...this.personas, enabled: false, available: [] };
          this.publish();
          return { notice: "Personas are turned off for this Alysis Code CLI." };
        case "invalid_persona":
          void this.refreshPersonas();
          return { notice: `No persona named ${name}. Run /persona to see what is available.` };
        case "persona_change_blocked":
          void this.refreshPersonas();
          return { notice: "The Alysis Code CLI declined this persona change. Try again once the session settles." };
        default:
          this.showError(error);
          return { notice: "" };
      }
    }
  }

  private handlePersonaChangedEvent(event: ProtocolEventEnvelope): void {
    let payload: PersonaChangedPayload;
    try {
      payload = parsePersonaChangedPayload(event.payload);
    } catch {
      this.output.appendLine("Ignored a malformed persona_changed event.");
      return;
    }
    this.personas = {
      ...this.personas,
      active: payload.persona,
      activeSource: payload.source || this.personas.activeSource
    };
    this.transcript.setSessionMode(payload.effective_mode);
    this.persistSessionReference(this.sessionId);
    // A change this controller did not initiate (e.g. a model-proposed switch approved in the CLI)
    // still deserves a line in the transcript so the mode chip never changes silently.
    if (payload.source && payload.source !== "user") {
      this.transcript.addNotice(`Persona changed to ${payload.persona} (${payload.effective_mode}).`);
    }
    // A persona change is a terminal-ish state change: never leave it behind a coalesced publish.
    this.publishImmediately();
    void this.refreshSessionModelInfo();
  }

  private currentSessionMode(): AlysisMode {
    const transcriptMode = this.transcript.state().mode;
    return isAlysisMode(transcriptMode) ? transcriptMode : this.retainedSessionMode ?? this.getConfig().defaultMode;
  }

  private async restartBridgeFromCockpit(): Promise<void> {
    if (!this.bridge.restartIfSafe) {
      this.showNotice("Bridge restart is unavailable for this Alysis Code bridge client.");
      return;
    }
    try {
      const previousSessionId = this.sessionId ?? this.recoverableSessionId;
      this.runtime?.setBridgeProcess("starting", "Restarting Alysis Code IDE bridge.");
      const restarted = await this.bridge.restartIfSafe(this.getConfig(), { stripApiKey: true });
      if (!restarted) {
        const message = "Alysis Code bridge was not restarted because a job, request, or approval is active.";
        this.runtime?.recordEvent({
          severity: "warning",
          source: "bridge",
          title: "Bridge restart blocked",
          message
        });
        this.showNotice(message);
        return;
      }
      this.sessionId = undefined;
      this.disconnectBrowser("Managed browsers ended when the Alysis Code bridge restarted.");
      this.recoverableSessionId = previousSessionId;
      this.persistSessionReference(previousSessionId);
      this.activeJobId = undefined;
      this.ownedSessionIds.clear();
      this.ownedJobIds.clear();
      this.trackedJobIds.clear();
      this.queuedJobIds.clear();
      this.cancellationRequestedJobIds.clear();
      this.unsupportedCancellationJobIds.clear();
      this.clearSessionModel();
      this.transcript.clearSession("bridge restarted");
      this.showNotice(
        previousSessionId
          ? "Alysis Code IDE bridge restarted. The retained conversation context will be restored with your next message."
          : "Alysis Code IDE bridge restarted."
      );
      // Re-run health detection against the live config so a newly-set alysis.cliPath is honored
      // and stale recovery cards clear without a window reload. Falls back to an optimistic "ready"
      // only when no reconnect action is wired (older host wiring / tests).
      if (this.cockpitActions?.reconnect) {
        await this.cockpitActions.reconnect();
      } else {
        this.runtime?.setBridgeProcess("ready", "Alysis Code IDE bridge restarted.");
      }
    } catch (error) {
      this.showError(error);
    }
  }

  private async sendUserMessage(
    text: string,
    routeSlashCommand = true,
    requestId?: string
  ): Promise<boolean> {
    if (this.workspaceTransition) return false;
    if (this.permissionChangePending) {
      this.showNotice("Wait for the permission change to finish before sending a message.");
      return false;
    }
    if (routeSlashCommand && await this.tryHandleSlashCommand(text)) {
      return true;
    }
    if (!await this.canUseSelectedProvider()) {
      return false;
    }
    const transcriptState = this.transcript.state();
    const sessionMode = this.currentSessionMode();
    const sessionWorkspace = this.sessionWorkspace?.root ?? transcriptState.workspaceRoot ?? activeWorkspaceRoot();
    if (
      sessionWorkspace
      && !await this.canUseWorkspaceWithDirtyDocuments(sessionMode, sessionWorkspace, "sending this write-capable message")
    ) {
      return false;
    }
    const hadActiveJob = this.activeJobId !== undefined;
    const intentGeneration = this.sessionIntentGeneration;
    if (this.permissionChangePending) {
      this.showNotice("Wait for the permission change to finish before sending a message.");
      return false;
    }
    // The sidebar normally prevents a second submit locally, but the Details panel, commands, and
    // programmatic callers share this controller. Until chat.send resolves there is no active job
    // id to guard on, so concurrent surfaces could otherwise start two follow-up jobs and append
    // two user messages to the same session.
    if (this.chatSendGeneration === intentGeneration) {
      this.showNotice("Alysis Code is already sending a message. Wait for it to start before trying again.");
      return false;
    }
    this.chatSendGeneration = intentGeneration;
    let userMessageAdded = false;
    try {
      await this.ensureBridgeAndSession();
      this.assertSessionIntentCurrent(intentGeneration);
      if (!this.sessionId) {
        throw new Error("Alysis Code session was not created.");
      }
      const sessionId = this.sessionId;
      const context = await this.collectIdeContextBlocks(sessionWorkspace);
      this.assertSessionIntentCurrent(intentGeneration);
      const diagnosticBaseline = this.captureDiagnosticBaseline(
        context.attachedDiagnosticFingerprints,
        this.sessionWorkspace?.root ?? this.transcript.state().workspaceRoot ?? activeWorkspaceRoot()
      );
      const preparedState = this.transcript.state();
      const preparedMode = isAlysisMode(preparedState.mode) ? preparedState.mode : sessionMode;
      const mutationWorkspace = this.sessionWorkspace?.root ?? preparedState.workspaceRoot ?? sessionWorkspace ?? activeWorkspaceRoot();
      if (
        mutationWorkspace
        && !await this.canUseWorkspaceWithDirtyDocuments(
          preparedMode,
          mutationWorkspace,
          "sending this write-capable message"
        )
      ) {
        return false;
      }
      // Make the submitted prompt visible only after all asynchronous host
      // preparation and the final dirty-buffer check have succeeded.
      this.transcript.addUserMessage(text);
      userMessageAdded = true;
      this.sessionsView.rememberTitle?.(sessionId, text);
      this.publish();
      const job = await this.bridge.sendChat(sessionId, text, {
        idempotency_key: requestId ?? randomUUID(),
        context_blocks: context.blocks
      });
      if (
        this.disposed ||
        this.sessionIntentGeneration !== intentGeneration ||
        this.sessionId !== sessionId
      ) {
        await this.closeSupersededSession(job.session_id);
        throw sessionStartSupersededError();
      }
      if (job.session_id !== sessionId) {
        await this.closeSupersededSession(job.session_id);
        throw new ProtocolClientError(
          "session_mismatch",
          "Alysis Code returned a chat job for a different session. The response was discarded."
        );
      }
      this.consumePendingContext(context.pendingAttachmentIds);
      this.ownedSessionIds.add(job.session_id);
      this.ownedJobIds.add(job.job_id);
      this.trackedJobIds.add(job.job_id);
      if (job.status === "queued" || hadActiveJob) {
        this.queuedJobIds.add(job.job_id);
      } else {
        this.queuedJobIds.delete(job.job_id);
      }
      this.cancellationRequestedJobIds.delete(job.job_id);
      this.unsupportedCancellationJobIds.delete(job.job_id);
      if (diagnosticBaseline) {
        this.diagnosticBaselines.set(job.job_id, diagnosticBaseline);
      }
      if (job.status === "queued" || hadActiveJob) {
        this.transcript.addNotice("Follow-up queued. Alysis Code will start it after the current request finishes.");
      } else if (job.status === "completed" || job.status === "failed" || job.status === "cancelled") {
        this.trackedJobIds.delete(job.job_id);
        this.ownedJobIds.delete(job.job_id);
        await this.verifyCompletedJob(job.job_id, job.status);
      } else {
        this.activeJobId = job.job_id;
        this.transcript.setJob(job.job_id, job.status || "running");
        this.statusBar.setActiveRun(job.job_id);
      }
      this.publish();
      await this.refreshSessionsView();
      this.assertSessionIntentCurrent(intentGeneration);
      if (this.trackedJobIds.has(job.job_id)) {
        void this.pollJob(job.job_id);
      }
      return true;
    } catch (error) {
      if (isSessionStartSupersededError(error)) {
        return false;
      }
      if (!userMessageAdded) {
        this.transcript.addUserMessage(text);
        this.publish();
      }
      this.showError(error);
      return false;
    } finally {
      // New Session advances the intent generation and may start a newer send while this one is
      // still settling. Do not let the stale operation clear the newer operation's lock.
      if (this.chatSendGeneration === intentGeneration) {
        this.chatSendGeneration = undefined;
      }
    }
  }

  private async canStartRunTask(): Promise<boolean> {
    if (this.permissionChangePending) {
      this.showNotice("Wait for the permission change to finish before sending a message.");
      return false;
    }
    if (this.workspaceTransition) return false;
    const gate = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "readonlyPlaceholder");
    if (!gate.allowed) {
      void vscode.window.showWarningMessage(gate.reason ?? "Workspace Trust is required.");
      return false;
    }
    const processGate = evaluateProcessExecutionCommand(this.getConfig(), "runTask");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return false;
    }
    return this.canUseSelectedProvider();
  }

  private async canUseSelectedProvider(): Promise<boolean> {
    const readiness = this.providerReadiness?.();
    if (!readiness || readiness.status === "ready") {
      return true;
    }
    void vscode.window.showWarningMessage(readiness.reason);
    return false;
  }

  private async collectIdeContextBlocks(workspaceRoot?: string): Promise<CollectedIdeContext> {
    const pending = [...this.pendingContextBlocks];
    if (!this.contextCollector || !vscode.workspace.isTrusted) {
      const merged = mergeContextBlocks(pending, []);
      return { ...prepareIdeContextForJob(merged.blocks), pendingAttachmentIds: merged.pendingAttachmentIds };
    }
    try {
      const contextStartedAt = Date.now();
      const result = await this.contextCollector.collect({
        workspaceRoot: workspaceRoot ?? this.sessionWorkspace?.root ?? activeWorkspaceRoot(),
        timeoutMs: 2_000,
        includeSelection: true,
        includeOpenEditors: true,
        includeDiagnostics: true,
        includeGitDiff: "working",
        includeSymbols: true,
        includeReferences: false,
        includeTerminalSelection: false
      });
      const reasons = [...new Set(result.skipped.map((item) => item.reason))]
        .sort()
        .join(", ");
      this.output.appendLine(
        `IDE context: ${result.blocks.length} block(s), ${result.totalBytes} byte(s), ${Date.now() - contextStartedAt}ms` +
          (result.truncated ? ", bounded by context limits" : "") +
          (reasons ? `; skipped sources: ${redactForDisplay(reasons)}` : "")
      );
      const merged = mergeContextBlocks(pending, result.blocks);
      return { ...prepareIdeContextForJob(merged.blocks), pendingAttachmentIds: merged.pendingAttachmentIds };
    } catch (error) {
      this.output.appendLine(
        `IDE context collection was skipped: ${redactForDisplay(
          error instanceof Error ? error.message : String(error)
        )}`
      );
      const merged = mergeContextBlocks(pending, []);
      return { ...prepareIdeContextForJob(merged.blocks), pendingAttachmentIds: merged.pendingAttachmentIds };
    }
  }

  private async startRunTask(
    instruction: string,
    modeOverride?: AlysisMode,
    requestId?: string
  ): Promise<boolean> {
    if (this.permissionChangePending) {
      this.showNotice("Wait for the permission change to finish before sending a message.");
      return false;
    }
    if (!instruction) {
      return false;
    }
    if (this.activeJobId) {
      this.showNotice("Alysis Code is still working on the current task. Wait for it to finish or stop it before starting another task.");
      return false;
    }
    const intentGeneration = this.sessionIntentGeneration;
    if (this.runStartPromise && this.runStartGeneration === intentGeneration) {
      this.showNotice("Alysis Code is already starting a task. Wait for it to start or choose New Task before trying again.");
      return false;
    }
    const startPromise = this.startRunTaskOnce(instruction, modeOverride, intentGeneration, requestId);
    this.runStartPromise = startPromise;
    this.runStartGeneration = intentGeneration;
    try {
      return await startPromise;
    } finally {
      if (this.runStartPromise === startPromise) {
        this.runStartPromise = undefined;
        this.runStartGeneration = undefined;
      }
    }
  }

  private async startRunTaskOnce(
    instruction: string,
    modeOverride: AlysisMode | undefined,
    intentGeneration: number,
    requestId: string | undefined
  ): Promise<boolean> {
    let userMessageAdded = false;
    let provisionalSessionId: string | undefined;
    try {
      const config = this.getConfig();
      const mode = modeOverride ?? config.defaultMode;
      this.runtime?.applyConfig(config);
      const processGate = evaluateProcessExecutionCommand(config, "runTask");
      if (!processGate.allowed) {
        throw new Error(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      }
      const workspaceIdentity = this.sessionWorkspace ?? activeWorkspaceIdentity();
      const workspace = workspaceIdentity?.root;
      if (!workspace || !workspaceIdentity) {
        throw new Error(workspaceScopeRequiredMessage("starting Alysis Code"));
      }
      if (!await this.canUseWorkspaceWithDirtyDocuments(mode, workspace, "starting this write-capable task")) {
        return false;
      }
      if (!vscode.workspace.isTrusted && mode !== "readonly") {
        throw new Error(untrustedModeMessage(mode));
      }
      await this.preflightFeature(config, "chatRun", "Run Task");
      if (!this.bridge.startRun) {
        throw new ProtocolClientError(
          "method_not_found",
          "The installed Alysis Code bridge does not expose run.start. Upgrade the Alysis Code CLI."
        );
      }
      this.runtime?.setBridgeProcess("starting", "Starting or reusing Alysis Code IDE bridge.");
      const started = await this.ensureCredentialBridge(config);
      this.runtime?.setCliHealth("ok", "CLI started the IDE bridge.");
      this.runtime?.setBridgeProtocol("ok", "IDE bridge initialized.");
      this.runtime?.setBridgeProcess("ready", "Alysis Code IDE bridge is ready.");
      if (started?.restarted) {
        if (this.sessionId) {
          this.recoverableSessionId = this.sessionId;
          this.persistSessionReference(this.sessionId);
        }
        this.sessionId = undefined;
        this.disconnectBrowser("Managed browsers ended when the Alysis Code bridge restarted.");
        this.activeJobId = undefined;
        this.ownedSessionIds.clear();
        this.ownedJobIds.clear();
        this.trackedJobIds.clear();
        this.queuedJobIds.clear();
        this.cancellationRequestedJobIds.clear();
        this.unsupportedCancellationJobIds.clear();
        this.clearSessionModel();
        this.transcript.clearSession("bridge restarted with credentials");
      }
      this.assertSessionIntentCurrent(intentGeneration);
      // The session is created first so the bridge can negotiate host capabilities against a
      // known session id; run.start then joins that session. The bridge accepts only TURN params
      // on an existing session (`unsupported_turn_option` otherwise) — workspace, mode, model, and
      // base_url belong to session.create below and must not be repeated here.
      const params: Record<string, unknown> = {
        instruction,
        idempotency_key: requestId ?? randomUUID()
      };
      const context = await this.collectIdeContextBlocks(workspace);
      params.context_blocks = context.blocks;
      this.assertSessionIntentCurrent(intentGeneration);
      const diagnosticBaseline = this.captureDiagnosticBaseline(context.attachedDiagnosticFingerprints, workspace);
      if (!await this.canUseWorkspaceWithDirtyDocuments(mode, workspace, "starting this write-capable task")) {
        return false;
      }
      const created = await this.bridge.createSession({
        workspace,
        mode,
        sandbox_profile: config.sandboxProfile,
        ...(config.defaultModel ? { model: config.defaultModel } : {}),
        ...(config.baseUrl ? { base_url: config.baseUrl } : {})
      });
      provisionalSessionId = created.session_id;
      if (created.sandbox_mode) this.sessionSandbox = { sessionId: created.session_id, mode: created.sandbox_mode };
      if (this.sessionIntentGeneration !== intentGeneration) {
        await this.closeSupersededSession(created.session_id);
        provisionalSessionId = undefined;
        throw sessionStartSupersededError();
      }
      params.session_id = created.session_id;
      // No awaited host work remains between this final guard and run.start.
      this.transcript.addUserMessage(instruction);
      userMessageAdded = true;
      this.publish();
      const job = await this.bridge.startRun(params);
      if (job.session_id !== created.session_id) {
        throw new Error("The bridge started the task under a different session than the negotiated IDE host tools.");
      }
      if (this.sessionIntentGeneration !== intentGeneration) {
        await this.closeSupersededSession(job.session_id);
        provisionalSessionId = undefined;
        throw sessionStartSupersededError();
      }
      provisionalSessionId = undefined;
      this.consumePendingContext(context.pendingAttachmentIds);
      this.sessionId = job.session_id;
      this.sessionsView.rememberTitle?.(job.session_id, instruction);
      this.sessionWorkspace = workspaceIdentity;
      this.syncBrowserOwner(job.session_id);
      this.recoverableSessionId = undefined;
      this.persistSessionReference(job.session_id);
      this.activeJobId = job.job_id;
      this.ownedSessionIds.add(job.session_id);
      this.ownedJobIds.add(job.job_id);
      this.trackedJobIds.add(job.job_id);
      this.queuedJobIds.delete(job.job_id);
      this.cancellationRequestedJobIds.delete(job.job_id);
      this.unsupportedCancellationJobIds.delete(job.job_id);
      if (diagnosticBaseline) {
        this.diagnosticBaselines.set(job.job_id, diagnosticBaseline);
      }
      this.transcript.setSession({
        session_id: job.session_id,
        workspace_root: workspace,
        mode
      });
      this.transcript.setJob(job.job_id, "running");
      this.statusBar.setActiveRun(job.job_id);
      await this.refreshSessionModelInfo();
      await this.refreshPersonas();
      this.assertSessionIntentCurrent(intentGeneration);
      this.publish();
      await this.refreshSessionsView();
      this.assertSessionIntentCurrent(intentGeneration);
      void this.pollJob(job.job_id);
      return true;
    } catch (error) {
      if (provisionalSessionId) {
        try {
          await this.closeSupersededSession(provisionalSessionId);
        } catch (cleanupError) {
          this.output.appendLine(
            `Task session cleanup failed: ${redactForDisplay(
              cleanupError instanceof Error ? cleanupError.message : String(cleanupError)
            )}`
          );
        }
      }
      if (isSessionStartSupersededError(error)) {
        return false;
      }
      if (!userMessageAdded) {
        this.transcript.addUserMessage(instruction);
        this.publish();
      }
      this.showError(error);
      return false;
    }
  }

  private async tryHandleSlashCommand(text: string): Promise<boolean> {
    const commandText = text.trim();
    if (!commandText.startsWith("/")) {
      return false;
    }
    const safeCommandText = redactForDisplay(commandText);
    const parsed =
      this.slashRouter && typeof (this.slashRouter as { parse?: unknown }).parse === "function"
        ? this.slashRouter.parse(commandText)
        : undefined;
    this.runtime?.recordEvent({
      severity: "info",
      source: "slash",
      title: "Slash command started",
      message: safeCommandText,
      details: parsed ? `Route: ${slashRouteLabel(parsed.command, parsed.id)}` : "Route: slash command router unavailable."
    });
    this.publish();
    try {
      if (!this.slashRouter) {
        this.transcript.addNotice("Slash commands are unavailable until the Alysis Code extension finishes initialization.");
        this.runtime?.recordEvent({
          severity: "warning",
          source: "slash",
          title: "Slash command unavailable",
          message: safeCommandText,
          details: "The slash command router is not initialized."
        });
      } else {
        const result = await this.slashRouter.execute(commandText);
        const safeNotice = result.notice ? redactForDisplay(result.notice) : result.notice;
        // FE-18: a backend-action slash returns an empty notice when its result card is the canonical
        // feedback (no "X completed." duplicate). Only add a notice when there is one to show.
        if (safeNotice) {
          this.transcript.addNotice(safeNotice);
        }
        const severity = result.severity ?? "info";
        this.runtime?.recordEvent({
          severity,
          source: "slash",
          title: severity === "error" ? "Slash command failed" : severity === "warning" ? "Slash command warning" : "Slash command routed",
          message: safeNotice,
          details: `Command: ${redactForDisplay(result.command || commandText)}`
        });
      }
      this.publish();
    } catch (error) {
      this.showError(error);
    }
    return true;
  }

  private async ensureBridgeAndSession(): Promise<void> {
    if (this.sessionStartPromise) {
      const pending = this.sessionStartPromise;
      const pendingGeneration = this.sessionStartGeneration;
      try {
        await pending;
      } catch (error) {
        if (
          pendingGeneration === this.sessionIntentGeneration ||
          !isSessionStartSupersededError(error)
        ) {
          throw error;
        }
      }
      if (pendingGeneration !== this.sessionIntentGeneration) {
        if (this.sessionStartPromise === pending) {
          this.sessionStartPromise = undefined;
          this.sessionStartGeneration = undefined;
        }
        await this.ensureBridgeAndSession();
      }
      return;
    }
    const startPromise = this.ensureBridgeAndSessionOnce();
    this.sessionStartPromise = startPromise;
    this.sessionStartGeneration = this.sessionIntentGeneration;
    try {
      await startPromise;
    } finally {
      if (this.sessionStartPromise === startPromise) {
        this.sessionStartPromise = undefined;
        this.sessionStartGeneration = undefined;
      }
    }
  }

  private async ensureBridgeAndSessionOnce(): Promise<void> {
    const intentGeneration = this.sessionIntentGeneration;
    const config = this.getConfig();
    this.runtime?.applyConfig(config);
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      throw new Error(processGate.reason ?? "Alysis Code CLI execution is blocked.");
    }
    const workspaceIdentity = this.sessionWorkspace ?? activeWorkspaceIdentity();
    const workspace = workspaceIdentity?.root;
    if (!workspaceIdentity || !workspace) {
      throw new Error(workspaceScopeRequiredMessage("starting Alysis Code"));
    }
    if (!workspaceFolderForIdentity(workspaceIdentity)) {
      throw new Error(
        "Reopen the original workspace folder for this Alysis Code session before reconnecting; " +
        "the extension will not resume it under a different multi-root folder or remote authority."
      );
    }
    const sessionMode = this.currentSessionMode();
    if (!vscode.workspace.isTrusted && sessionMode !== "readonly") {
      throw new Error(untrustedModeMessage(sessionMode));
    }
    await this.preflightFeature(config, "chatRun", "Chat and Run");
    this.runtime?.setBridgeProcess("starting", "Starting or reusing Alysis Code IDE bridge.");
    const started = await this.ensureCredentialBridge(config);
    this.runtime?.setCliHealth("ok", "CLI started the IDE bridge.");
    this.runtime?.setBridgeProtocol("ok", "IDE bridge initialized.");
    this.runtime?.setBridgeProcess("ready", "Alysis Code IDE bridge is ready.");
    if (started?.restarted) {
      if (this.sessionId) {
        this.recoverableSessionId = this.sessionId;
        this.persistSessionReference(this.sessionId);
      }
      this.sessionId = undefined;
      this.disconnectBrowser("Managed browsers ended when the Alysis Code bridge restarted.");
      this.activeJobId = undefined;
      this.ownedSessionIds.clear();
      this.ownedJobIds.clear();
      this.trackedJobIds.clear();
      this.queuedJobIds.clear();
      this.cancellationRequestedJobIds.clear();
      this.unsupportedCancellationJobIds.clear();
      this.clearSessionModel();
      this.transcript.clearSession("bridge restarted with credentials");
    }
    this.assertSessionIntentCurrent(intentGeneration);
    if (this.sessionId) {
      if (this.sessionModel.sessionId !== this.sessionId) {
        await this.refreshSessionModelInfo();
        await this.refreshPersonas();
      }
      return;
    }
    const params: Record<string, unknown> = {
      workspace,
      mode: sessionMode,
      sandbox_profile: config.sandboxProfile
    };
    if (config.defaultModel) {
      params.model = config.defaultModel;
    }
    if (config.baseUrl) {
      params.base_url = config.baseUrl;
    }
    const session = await this.bridge.createSession(params);
    if (this.sessionIntentGeneration !== intentGeneration) {
      await this.closeSupersededSession(session.session_id);
      throw sessionStartSupersededError();
    }
    this.sessionId = session.session_id;
    if (session.sandbox_mode) this.sessionSandbox = { sessionId: session.session_id, mode: session.sandbox_mode };
    this.sessionWorkspace = workspaceIdentity;
    this.syncBrowserOwner(session.session_id);
    this.ownedSessionIds.add(session.session_id);
    this.transcript.setSession(session);
    this.statusBar.setBridgeOk();
    await this.restoreRetainedSessionIfAvailable(session.session_id, intentGeneration);
    this.assertSessionIntentCurrent(intentGeneration);
    this.persistSessionReference(session.session_id);
    await this.refreshSessionModelInfo();
    await this.refreshPersonas();
    this.publish();
    await this.refreshSessionsView();
  }

  private assertSessionIntentCurrent(generation: number): void {
    if (this.disposed || this.sessionIntentGeneration !== generation) {
      throw sessionStartSupersededError();
    }
  }

  private async closeSupersededSession(sessionId: string): Promise<void> {
    try {
      const cleanup = await this.bridge.cancelSession(sessionId, {
        reason: "session_start_superseded",
        closeWhenIdle: true
      });
      if (cleanup.status !== "closed" && cleanup.close_when_idle !== true) {
        throw new Error("The bridge did not acknowledge close-after-settle cleanup.");
      }
    } catch {
      // The bridge may itself be shutting down. The important invariant is that the
      // stale response is never installed as the controller's active session.
      this.output.appendLine(
        "Alysis Code could not confirm cleanup of a superseded session; restart the bridge if its resources remain active."
      );
    }
  }

  private async restoreRetainedSessionIfAvailable(liveSessionId: string, intentGeneration: number): Promise<void> {
    const targetSessionId = this.recoverableSessionId;
    if (!targetSessionId || targetSessionId === liveSessionId) {
      this.recoverableSessionId = undefined;
      return;
    }
    this.recoverableSessionId = undefined;
    if (!this.bridge.sessionResume || this.bridge.supportsMethod?.("session.resume") === false) {
      if (this.disposed || this.sessionIntentGeneration !== intentGeneration || this.sessionId !== liveSessionId) {
        return;
      }
      this.transcript.addNotice(
        "The previous Alysis Code process stopped. A new session was created, but this CLI cannot restore the retained conversation context."
      );
      return;
    }
    const hydrateColdTranscript = this.transcript.state().items.length === 0;
    try {
      const resume = await this.bridge.sessionResume(liveSessionId, targetSessionId, { emitHistory: hydrateColdTranscript });
      if (this.disposed || this.sessionIntentGeneration !== intentGeneration || this.sessionId !== liveSessionId) {
        return;
      }
      if (hydrateColdTranscript) {
        await this.replayEvents();
      }
      const previousTitle = this.sessionsView.titleFor?.(targetSessionId);
      if (previousTitle) this.sessionsView.rememberTitle?.(liveSessionId, previousTitle);
      await this.adoptRecoveredChatQueue(liveSessionId, resume);
      // Only worth saying when there was a conversation to bring back; on a cold first send the
      // "restore" is invisible and the notice would just be noise above the user's message.
      const restoredDialogue = this.transcript.state().items.some((item) => item.kind === "user" || item.kind === "assistant");
      if (!hydrateColdTranscript || restoredDialogue) {
        this.transcript.addNotice("Reconnected and restored your previous conversation.");
      }
    } catch (error) {
      if (this.disposed || this.sessionIntentGeneration !== intentGeneration || this.sessionId !== liveSessionId) {
        return;
      }
      this.transcript.addNotice(
        `Alysis Code reconnected, but the previous conversation context could not be restored: ${protocolErrorMessage(error)}`
      );
    }
  }

  private async adoptRecoveredChatQueue(
    liveSessionId: string,
    resume: SessionResumeResult
  ): Promise<void> {
    const recoveredCount = (resume.queued_prompts_rebound ?? 0)
      + (resume.expired_prompts_recovered ?? 0)
      + (resume.active_prompts_observed ?? 0);
    if (!this.bridge.chatQueueList || this.bridge.supportsMethod?.("chat.queue.list") === false) {
      if (recoveredCount > 0) {
        this.transcript.addNotice(
          "Recovered queued work exists, but this bridge cannot list it for immediate IDE tracking. Upgrade the Alysis Code CLI."
        );
      }
      return;
    }
    const listed = await this.bridge.chatQueueList(liveSessionId, {
      states: ["pending", "running"],
      limit: 100
    });
    if (listed.session_id !== liveSessionId || this.sessionId !== liveSessionId) {
      throw new Error("Recovered chat queue response did not match the resumed session.");
    }
    const outstanding = listed.items
      .filter((item) => item.session_id === liveSessionId && (item.state === "pending" || item.state === "running"))
      .sort((left, right) => left.sequence - right.sequence)
      .slice(0, 100);
    if (outstanding.length === 0) {
      return;
    }
    const running = outstanding.find((item) => item.state === "running");
    const foreground = running ?? outstanding[0];
    this.activeJobId = foreground.prompt_id;
    this.transcript.setJob(foreground.prompt_id, running ? "running" : "queued");
    this.statusBar.setActiveRun(foreground.prompt_id);
    this.runtime?.setBridgeProcess(
      running ? "active" : "ready",
      running
        ? "Alysis Code resumed active chat work after reconnecting."
        : "Alysis Code restored queued chat work after reconnecting."
    );
    for (const item of outstanding) {
      const jobId = item.prompt_id;
      const newlyTracked = !this.trackedJobIds.has(jobId);
      this.ownedJobIds.add(jobId);
      this.trackedJobIds.add(jobId);
      if (item.state === "pending") {
        this.queuedJobIds.add(jobId);
      } else {
        this.queuedJobIds.delete(jobId);
      }
      if (newlyTracked) {
        void this.pollJob(jobId);
      }
    }
    this.publish();
  }

  private async bridgeStartOptions(config: AlysisConfig): Promise<BridgeStartOptions> {
    if (config.security && !config.security.cliPath.apiKeyForwardingAllowed) {
      return {};
    }
    const options: BridgeStartOptions = {};
    const apiKey = await this.secrets.getApiKey();
    if (apiKey && apiKey.trim().length > 0) {
      options.apiKey = apiKey;
    }
    // Per-provider keys are what the CLI actually reads for a non-default profile; without them a
    // connected provider resolves as missing even though the extension holds its key.
    const providerCredentials = await this.secrets.listProviderCredentials?.();
    if (providerCredentials && providerCredentials.length > 0) {
      options.providerCredentials = providerCredentials;
    }
    return options;
  }

  private async ensureCredentialBridge(config: AlysisConfig): Promise<BridgeProfileStartResult | void> {
    const options = await this.bridgeStartOptions(config);
    let result: BridgeProfileStartResult | void = undefined;
    if (this.bridge.ensureStartedForProfile) {
      result = await this.bridge.ensureStartedForProfile(config, options, { credentialsRequired: true });
    } else {
      await this.bridge.ensureStarted(config, options);
    }
    const advertisedEvent = this.bridge.supportsEvent?.("activity_update");
    const semanticActivity = advertisedEvent
      ?? this.bridge.supportsFeature?.(["semantic_activity", "semantic_tool_identity"])
      ?? false;
    this.transcript.setSemanticActivityEnabled(semanticActivity);
    return result;
  }

  private async preflightFeature(
    config: AlysisConfig,
    feature: CompatibilityFeatureId,
    label: string
  ): Promise<void> {
    if (!this.bridge.health) {
      return;
    }
    this.runtime?.setCliHealth("checking", `Checking Alysis Code CLI compatibility for ${label}.`);
    try {
      const health = await this.bridge.health(config);
      const compatibility = this.runtime?.applyBridgeHealth(health) ?? evaluateBridgeCompatibility(health);
      assertFeatureCompatible(compatibility, feature);
      this.runtime?.setCliHealth("ok", `Alysis Code CLI supports ${label}.`);
    } catch (error) {
      const message = compatibilityErrorMessage(error);
      this.runtime?.setCliHealth(cliHealthStatusForCompatibilityError(error), message);
      this.runtime?.setBridgeProcess("error", message);
      this.runtime?.recordEvent({
        severity: "error",
        source: "compatibility",
        title: `${label} unavailable`,
        message,
        details: "Upgrade or install a compatible Alysis Code CLI before starting this workflow."
      });
      throw error;
    }
  }

  private async respondToApproval(
    approvalId: string,
    decision: "allow_once" | "allow_for_session" | "deny"
  ): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) {
      this.showError("Cannot respond to approval because no session is active.");
      return;
    }
    if (!this.transcript.pendingApprovals().some((approval) => approval.approvalId === approvalId)) {
      return;
    }
    const allow = decision === "allow_once" || decision === "allow_for_session";
    const responseKey = `${sessionId}:${approvalId}`;
    if (this.pendingApprovalResponses.has(responseKey)) {
      return;
    }
    const intentGeneration = this.sessionIntentGeneration;
    // Lock before asynchronous dirty-buffer validation so contradictory double
    // clicks cannot both reach the backend after the same validation yield.
    this.pendingApprovalResponses.add(responseKey);
    try {
      if (allow) {
        const state = this.transcript.state();
        const workspace = this.sessionWorkspace?.root ?? state.workspaceRoot ?? activeWorkspaceRoot();
        // An explicit approval authorizes a mutation even when the surrounding
        // chat session began in readonly mode, so never apply the readonly bypass.
        if (workspace && !await this.canUseWorkspaceWithDirtyDocuments("review", workspace, "approving this workspace change")) {
          return;
        }
      }
      const result = await this.bridge.respondApproval({
        session_id: sessionId,
        approval_id: approvalId,
        allow,
        allow_for_session: decision === "allow_for_session"
      });
      if (
        this.disposed ||
        this.sessionIntentGeneration !== intentGeneration ||
        this.sessionId !== sessionId
      ) {
        return;
      }
      const effectiveDecision =
        result.allow === true
          ? result.allow_for_session
            ? "allow_for_session"
            : "allow_once"
          : "deny";
      this.transcript.resolveApproval(approvalId, effectiveDecision);
      if (result.allow_for_session_warning) {
        this.transcript.addNotice(result.allow_for_session_warning);
      }
      this.statusBar.setActiveRun(this.activeJobId);
      // An answered approval unblocks the backend: publish the resolved state immediately.
      this.publishImmediately();
    } catch (error) {
      if (
        this.disposed ||
        this.sessionIntentGeneration !== intentGeneration ||
        this.sessionId !== sessionId
      ) {
        return;
      }
      this.transcript.resolveApproval(
        approvalId,
        error instanceof ProtocolClientError && error.code === "approval_expired" ? "expired" : "deny"
      );
      this.showError(error);
    } finally {
      this.pendingApprovalResponses.delete(responseKey);
    }
  }

  private async denyPendingApprovals(): Promise<void> {
    if (!this.sessionId) {
      return;
    }
    const pending = this.transcript.pendingApprovals();
    for (const approval of pending) {
      try {
        await this.bridge.respondApproval({
          session_id: this.sessionId,
          approval_id: approval.approvalId,
          allow: false,
          allow_for_session: false
        });
      } catch {
        // Fail closed: if denial cannot be sent, the backend approval times out and denies.
      }
      this.transcript.resolveApproval(approval.approvalId, "deny");
    }
    this.publishImmediately();
  }

  private async replayEvents(): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) {
      return;
    }
    const activeBarrier = this.eventReplayBarrier;
    if (activeBarrier) {
      if (activeBarrier.sessionId !== sessionId) {
        this.eventReplayBarrier = undefined;
        await this.replayEvents();
        return;
      }
      await activeBarrier.promise;
      return;
    }
    const barrier: EventReplayBarrier = { sessionId, buffered: [], overflowed: false };
    this.eventReplayBarrier = barrier;
    const replayPromise = this.replayEventsOnce(barrier);
    barrier.promise = replayPromise;
    try {
      await replayPromise;
    } finally {
      if (this.eventReplayBarrier === barrier) {
        this.eventReplayBarrier = undefined;
      }
    }
  }

  private async replayEventsOnce(barrier: EventReplayBarrier): Promise<void> {
    let replayed: ProtocolEventEnvelope[] = [];
    let truncated = false;
    let replayError: unknown;
    try {
      const replay = await this.bridge.getEvents(barrier.sessionId, this.transcript.lastSequence(barrier.sessionId));
      replayed = replay.events;
      truncated = replay.truncated;
    } catch (error) {
      replayError = error;
    }
    if (this.disposed || this.sessionId !== barrier.sessionId || this.eventReplayBarrier !== barrier) {
      return;
    }
    const combined = [...replayed, ...barrier.buffered]
      .filter((event) => event.session_id === barrier.sessionId)
      .sort((left, right) => left.sequence - right.sequence);
    if (truncated || barrier.overflowed) {
      this.transcript.markReplayTruncated();
    }
    for (const event of combined) {
      if (event.type === "persona_changed") {
        // Replay is followed by refreshPersonas(), which re-reads the authoritative persona state;
        // replaying stale switches one by one would only thrash the surface.
        continue;
      }
      this.applyTranscriptEvent(event);
    }
    if (this.transcript.pendingApprovals().length > 0) {
      this.statusBar.setApprovalNeeded();
      this.runtime?.setBridgeProcess("approval_needed", "Alysis Code is waiting for a chat approval decision.");
    }
    // A completed replay is the rebuilt truth for the whole transcript; do not delay it.
    this.publishImmediately();
    if (replayError !== undefined) {
      this.showNotice(`Could not replay Alysis Code events: ${protocolErrorMessage(replayError)}`);
    }
  }

  private async refreshArtifacts(): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) {
      this.showNotice("No Alysis Code session exists yet.");
      return;
    }
    try {
      const payload = await this.bridge.artifactList(sessionId);
      if (this.sessionId !== sessionId) {
        return;
      }
      this.transcript.setArtifacts(payload.artifacts, payload.truncated);
      this.publish();
    } catch (error) {
      if (this.disposed || this.sessionId !== sessionId) {
        return;
      }
      this.showError(error);
    }
  }

  private async openArtifact(artifactId: string): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) {
      return;
    }
    try {
      const artifact = await this.bridge.artifactRead(sessionId, artifactId);
      if (this.sessionId !== sessionId) {
        return;
      }
      const document = await vscode.workspace.openTextDocument({
        content: artifact.content,
        language: "text"
      });
      await vscode.window.showTextDocument(document, { preview: true });
    } catch (error) {
      if (this.disposed || this.sessionId !== sessionId) {
        return;
      }
      this.showError(error);
    }
  }

  private handleBridgeEvent(event: ProtocolEventEnvelope): void {
    if (this.disposed || !this.ownsEvent(event)) {
      return;
    }
    const barrier = this.eventReplayBarrier;
    if (barrier?.sessionId === event.session_id) {
      if (barrier.buffered.length >= MAX_BUFFERED_LIVE_EVENTS_DURING_REPLAY) {
        barrier.buffered.shift();
        barrier.overflowed = true;
      }
      barrier.buffered.push(event);
      return;
    }
    if (event.type === "persona_changed") {
      this.handlePersonaChangedEvent(event);
      return;
    }
    this.applyTranscriptEvent(event);
    if (event.type === "prompt_for_input" && event.payload.kind === "approval") {
      if (!this.primaryChatView) {
        const approvalId = typeof event.payload.approval_id === "string" ? event.payload.approval_id : undefined;
        if (approvalId) {
          void this.bridge
            .respondApproval({
              session_id: event.session_id,
              approval_id: approvalId,
              allow: false,
              allow_for_session: false
            })
            .catch(() => undefined);
          this.transcript.resolveApproval(approvalId, "deny");
        }
        this.output.appendLine("approval denied because no Alysis Code chat surface is attached");
        this.publishImmediately();
        return;
      }
      this.statusBar.setApprovalNeeded();
      this.runtime?.setBridgeProcess("approval_needed", "Alysis Code is waiting for a chat approval decision.");
    }
    // Streaming deltas are coalesced to frame cadence; anything that ends the turn (or needs a
    // decision before it can continue) flushes now so the final state can never be dropped.
    if (isTerminalBridgeEvent(event)) {
      this.publishImmediately();
      return;
    }
    this.publish();
  }

  private applyTranscriptEvent(event: ProtocolEventEnvelope): void {
    const previousMode = this.transcript.state().mode;
    if (this.transcript.applyEvent(event)) {
      if (event.session_id === this.sessionId && previousMode !== this.transcript.state().mode) {
        this.persistSessionReference(this.sessionId);
      }
      return;
    }
    const eventType = redactForDisplay(event.type)
      .replace(/[\r\n\t]+/g, " ")
      .slice(0, 160);
    this.output.appendLine(`Ignored unsupported Alysis Code event type: ${eventType || "(empty)"}`);
  }

  private ownsEvent(event: ProtocolEventEnvelope): boolean {
    if (this.ownedSessionIds.has(event.session_id)) {
      return true;
    }
    return typeof event.job_id === "string" && this.ownedJobIds.has(event.job_id);
  }

  private async pollJob(jobId: string): Promise<void> {
    let consecutiveStatusFailures = 0;
    while (!this.disposed && this.trackedJobIds.has(jobId)) {
      await sleep(1500);
      if (this.disposed || !this.trackedJobIds.has(jobId)) {
        return;
      }
      try {
        const status = await this.bridge.jobStatus(jobId);
        consecutiveStatusFailures = 0;
        if (
          this.disposed ||
          !this.trackedJobIds.has(jobId) ||
          this.sessionId !== status.session_id
        ) {
          return;
        }
        const foreground = this.activeJobId === undefined || this.activeJobId === jobId;
        if (status.status === "running" || status.status === "cancellation_requested") {
          this.queuedJobIds.delete(jobId);
          this.activeJobId = jobId;
          this.transcript.setJobStatus(status);
          this.statusBar.setActiveRun(jobId);
          this.runtime?.setBridgeProcess("active", "Alysis Code is working on a chat request.");
        } else if (status.status === "queued") {
          this.queuedJobIds.add(jobId);
          if (foreground) {
            this.activeJobId = jobId;
            this.transcript.setJobStatus(status);
            this.statusBar.setActiveRun(jobId);
          }
        } else if (foreground) {
          this.transcript.setJobStatus(status);
        }
        this.publish();
        if (this.disposed || !this.trackedJobIds.has(jobId)) {
          return;
        }
        if (status.status === "completed" || status.status === "failed" || status.status === "cancelled") {
          this.trackedJobIds.delete(jobId);
          this.queuedJobIds.delete(jobId);
          if (this.activeJobId === jobId) {
            this.activeJobId = undefined;
          }
          this.ownedJobIds.delete(jobId);
          this.cancellationRequestedJobIds.delete(jobId);
          this.unsupportedCancellationJobIds.delete(jobId);
          await this.verifyCompletedJob(jobId, status.status);
          this.adoptNextQueuedJobAsForeground();
          if (this.trackedJobIds.size === 0) {
            this.statusBar.setIdle();
            this.runtime?.setBridgeProcess("ready", "Chat queue is idle.");
          }
          // The job finished: publish the terminal state before the (slower) artifact refresh.
          this.publishImmediately();
          await this.refreshSessionsView();
          await this.refreshArtifacts();
          return;
        }
        await this.refreshSessionsView();
      } catch (error) {
        if (this.disposed || !this.trackedJobIds.has(jobId)) {
          return;
        }
        if (this.queuedJobIds.has(jobId) && this.activeJobId !== undefined && this.activeJobId !== jobId) {
          // A recovered pending queue row may not have a bridge job object until
          // the inherited active lease finishes. Keep observing it without
          // degrading the newer foreground job's global UI.
          continue;
        }
        consecutiveStatusFailures += 1;
        if (consecutiveStatusFailures <= MAX_TRANSIENT_JOB_STATUS_FAILURES) {
          if (consecutiveStatusFailures === 1) {
            this.showNotice("Alysis Code temporarily lost this job's status. Retrying automatically.");
          }
          continue;
        }
        const message = `Could not refresh job status after ${consecutiveStatusFailures} attempts: ${protocolErrorMessage(error)}`;
        this.trackedJobIds.delete(jobId);
        this.queuedJobIds.delete(jobId);
        this.ownedJobIds.delete(jobId);
        this.cancellationRequestedJobIds.delete(jobId);
        this.unsupportedCancellationJobIds.delete(jobId);
        const ownedGlobalState = this.activeJobId === undefined || this.activeJobId === jobId;
        if (this.activeJobId === jobId) {
          this.activeJobId = undefined;
        }
        this.adoptNextQueuedJobAsForeground();
        if (ownedGlobalState && this.activeJobId === undefined) {
          this.transcript.setJobStatus({
            job_id: jobId,
            session_id: this.sessionId ?? "unknown",
            status: "status_unknown",
            state: "status_unknown",
            cancellable: false
          });
          this.showNotice(message);
          this.statusBar.setError(message);
          this.runtime?.setBridgeProcess("error", message);
        } else {
          this.output.appendLine(`Background queued job status was lost without replacing the active chat state: ${message}`);
          this.showNotice("Alysis Code could not refresh an older queued request; the current active request is still being tracked.");
        }
        // Giving up on a job is terminal for it; flush now.
        this.publishImmediately();
        await this.refreshSessionsView();
        return;
      }
    }
  }

  private adoptNextQueuedJobAsForeground(): void {
    if (this.activeJobId !== undefined) {
      return;
    }
    const next = this.queuedJobIds.values().next().value as string | undefined;
    if (!next || !this.trackedJobIds.has(next)) {
      return;
    }
    this.activeJobId = next;
    this.transcript.setJob(next, "queued");
    this.statusBar.setActiveRun(next);
    this.runtime?.setBridgeProcess("ready", "A queued Alysis Code chat request is waiting to start.");
  }

  private captureDiagnosticBaseline(
    requiredFingerprints: readonly string[],
    workspaceRoot: string | null | undefined
  ): DiagnosticBaseline | undefined {
    if (!this.diagnosticsVerification) {
      return undefined;
    }
    // Silently skipping verification is how a task quietly loses its safety net, so say so.
    if (!vscode.workspace.isTrusted) {
      this.noticeVerificationSkipped(
        "Alysis Code will not verify this change against editor diagnostics because this folder is not trusted."
      );
      return undefined;
    }
    const normalizedRoot = workspaceRoot?.trim();
    if (!normalizedRoot) {
      this.noticeVerificationSkipped(
        "Alysis Code will not verify this change against editor diagnostics because no workspace folder is in scope."
      );
      return undefined;
    }
    return this.diagnosticsVerification.captureBaseline(requiredFingerprints, { workspaceRoot: normalizedRoot });
  }

  /**
   * Report a skipped verification once per session intent. Every send in an untrusted folder skips
   * it, and repeating the same line per message would train the user to ignore it.
   */
  private noticeVerificationSkipped(reason: string): void {
    if (this.verificationSkipNoticeGeneration === this.sessionIntentGeneration) {
      return;
    }
    this.verificationSkipNoticeGeneration = this.sessionIntentGeneration;
    this.showNotice(reason);
  }

  private async verifyCompletedJob(jobId: string, status: string): Promise<void> {
    const baseline = this.diagnosticBaselines.get(jobId);
    this.diagnosticBaselines.delete(jobId);
    if (!baseline || !this.diagnosticsVerification || status !== "completed") {
      return;
    }
    // A verification that cannot be cancelled outlives the session it belongs to. Bind it to a real
    // CancellationToken so shutdown, a bridge drop, or New Session stops it at the next checkpoint.
    const cancellation = this.beginVerificationCancellation(jobId);
    let report: DiagnosticsVerificationReport;
    try {
      report = await this.diagnosticsVerification.verifyAfterChanges(baseline, {
        settleMs: VERIFICATION_SETTLE_MS,
        timeoutMs: VERIFICATION_TIMEOUT_MS,
        ...(cancellation ? { signal: cancellation.signal } : {})
      });
    } finally {
      this.endVerificationCancellation(jobId);
    }
    if (this.disposed) {
      return;
    }
    const introducedErrors = report.introducedCounts.error;
    const introducedWarnings = report.introducedCounts.warning;
    const message = report.blocksCompletion
      ? `Verification blocked completion: ${report.reason}`
      : report.requiredAttachedCount > 0
        ? `Verification passed: ${report.reason}`
      : introducedWarnings > 0
        ? `Verification passed with ${introducedWarnings} new warning${introducedWarnings === 1 ? "" : "s"}.`
        : "Verification passed: no new diagnostics were introduced.";
    if (report.blocksCompletion) {
      this.transcript.markVerificationNeedsAttention(jobId, status, message);
    } else if (introducedWarnings > 0) {
      this.transcript.addNotice(message);
    }
    // A clean pass is the expected outcome of every turn; it goes to the Output channel and the
    // runtime event feed, not the conversation, so the transcript stays a dialogue rather than a log.
    this.output.appendLine(
      `Diagnostics verification for ${jobId}: ${report.status}; ` +
        `${introducedErrors} new error(s), ${introducedWarnings} new warning(s); ` +
        `${report.clearedAttachedCount}/${report.requiredAttachedCount} attached diagnostic(s) cleared; ` +
        `${redactForDisplay(report.reason)}`
    );
    this.runtime?.recordEvent({
      severity: report.blocksCompletion ? "error" : introducedWarnings > 0 ? "warning" : "info",
      source: "verification",
      title: report.blocksCompletion ? "Diagnostics verification blocked" : "Diagnostics verification passed",
      message,
      details: `Job ${jobId}; ${introducedErrors} new error(s), ${introducedWarnings} new warning(s); ` +
        `${report.clearedAttachedCount}/${report.requiredAttachedCount} attached diagnostic(s) cleared.`
    });
    // The job is finished: flush the verdict rather than leaving it in a coalesced publish.
    this.publishImmediately();
  }

  /**
   * Back one in-flight verification with a VS Code CancellationToken bridged to an AbortSignal, so
   * the gate's own settle/timeout loop stops the moment the controller decides the work is stale.
   */
  private beginVerificationCancellation(jobId: string): { signal: AbortSignal } | undefined {
    const source = createCancellationTokenSource();
    if (!source) {
      return undefined;
    }
    this.verificationCancellations.get(jobId)?.cancel();
    this.verificationCancellations.set(jobId, source);
    const abort = new AbortController();
    source.token.onCancellationRequested(() => abort.abort());
    if (source.token.isCancellationRequested) {
      abort.abort();
    }
    return { signal: abort.signal };
  }

  private endVerificationCancellation(jobId: string): void {
    const source = this.verificationCancellations.get(jobId);
    if (!source) {
      return;
    }
    this.verificationCancellations.delete(jobId);
    source.dispose();
  }

  private cancelPendingVerifications(): void {
    for (const [jobId, source] of this.verificationCancellations) {
      this.verificationCancellations.delete(jobId);
      source.cancel();
      source.dispose();
    }
  }

  private async canUseWorkspaceWithDirtyDocuments(
    mode: AlysisMode,
    workspace: string,
    action: string
  ): Promise<boolean> {
    if (mode === "readonly") {
      return true;
    }
    try {
      assertNoDirtyWorkspaceDocuments(vscode, workspace, action);
      return true;
    } catch (error) {
      if (!isDirtyWorkspaceDocumentsError(error)) {
        throw error;
      }
      this.output.appendLine(`workspace mutation blocked: ${error.message}`);
      void vscode.window.showWarningMessage(error.message);
      return false;
    }
  }

  private async refreshSessionsView(): Promise<void> {
    this.sessionsView.setActiveSession(this.sessionId);
    try {
      const list = await this.bridge.sessionList();
      this.sessionsView.setSessions(list.sessions);
    } catch {
      const transcriptMode = this.transcript.state().mode;
      this.sessionsView.setSessions(
        this.sessionId
          ? [
              {
                session_id: this.sessionId,
                workspace_root: "",
                mode: isAlysisMode(transcriptMode) ? transcriptMode : this.getConfig().defaultMode,
                closed: false,
                active_job: this.activeJobId
                  ? {
                      job_id: this.activeJobId,
                      session_id: this.sessionId,
                      status: "running"
                    }
                  : null
              }
            ]
          : []
      );
    }
  }

  /**
   * Coalesce state publishes to roughly one animation frame with a trailing flush. A streaming turn
   * emits a delta per token, and each publish re-serializes the whole cockpit (up to 200 transcript
   * items plus runtime state) across the IPC boundary — at frame cadence that cost is paid once
   * per frame instead of once per token. Terminal transitions call publishImmediately(), so the final
   * state of a turn is never left sitting in a pending timer.
   */
  private publish(): void {
    this.cachedCockpitState = undefined;
    if (this.disposed || this.publishTimer !== undefined) {
      return;
    }
    this.publishTimer = setTimeout(() => {
      this.publishTimer = undefined;
      this.flushPublish();
    }, PUBLISH_COALESCE_MS);
    // Never hold the extension host alive for a pending UI refresh.
    this.publishTimer.unref?.();
  }

  /** Publish now, cancelling any pending trailing flush. Used for every terminal transition. */
  private publishImmediately(): void {
    this.cachedCockpitState = undefined;
    this.cancelScheduledPublish();
    if (this.disposed) {
      return;
    }
    this.flushPublish();
  }

  private cancelScheduledPublish(): void {
    if (this.publishTimer !== undefined) {
      clearTimeout(this.publishTimer);
      this.publishTimer = undefined;
    }
  }

  private flushPublish(): void {
    if (this.disposed) {
      return;
    }
    const state = this.cockpitState();
    for (const listener of this.stateListeners) {
      listener(state);
    }
  }

  /**
   * Project the published cockpit state. Memoized for the current synchronous turn only: one publish
   * fans out to the sidebar, the slash-command catalog, and the command-context sync, and rebuilding
   * the runtime and command surfaces once per reader was the other half of the streaming
   * cost. The cache is dropped on the next microtask so no caller can ever observe stale state across
   * an await, and every mutation invalidates it eagerly through publish().
   */
  private cockpitState(): CockpitState {
    if (this.cachedCockpitState) {
      return this.cachedCockpitState;
    }
    const chat = this.transcript.state();
    const cockpit = this.cockpitProvider ? this.cockpitProvider() : emptyCockpitState(chat).cockpit;
    const model = this.sessionModel.model || cockpit.status.model;
    const status = {
      ...cockpit.status,
      mode: this.currentSessionMode(),
      sandbox: this.sessionSandbox?.sessionId === this.sessionId
        ? this.sessionSandbox?.mode ?? cockpit.status.sandbox : cockpit.status.sandbox,
      model,
      sessionModel: { ...this.sessionModel }
    };
    const state: CockpitState = {
      ...chat,
      mode: this.currentSessionMode(),
      lastAcceptedTaskRequestId: this.lastAcceptedTaskRequestId,
      personas: { ...this.personas, available: this.personas.available.map((persona) => ({ ...persona })) },
      readiness: buildCockpitReadiness({
        runtime: status.runtime,
        workspaceTrusted: status.workspaceTrusted,
        cliTrusted: status.cliTrusted,
        // Fail closed before the provider catalog is wired: "not checked yet" is never a green light.
        provider: this.providerReadiness?.() ?? {
          status: "loading",
          reason: "Checking the selected provider and model..."
        }
      }),
      cockpit: {
        ...cockpit,
        status
      }
    };
    this.cachedCockpitState = state;
    queueMicrotask(() => {
      if (this.cachedCockpitState === state) {
        this.cachedCockpitState = undefined;
      }
    });
    return state;
  }

  private syncBrowserOwner(sessionId: string | undefined): void {
    const operation = this.cockpitActions?.browserOwnerChanged?.(sessionId);
    void operation?.catch(() => {
      this.output.appendLine("Managed browser owner state reset safely.");
    });
  }

  private disconnectBrowser(reason: string): void {
    const operation = this.cockpitActions?.browserDisconnected?.(reason);
    void operation?.catch(() => {
      this.output.appendLine("Managed browser disconnected safely.");
    });
  }

  private supportsBridgeMethod(method: string): boolean {
    return this.bridge.supportsMethod ? this.bridge.supportsMethod(method) : true;
  }

  private detachBridgeListeners(): void {
    if (!this.bridge.off) {
      return;
    }
    this.bridge.off("event", this.onBridgeEvent);
    this.bridge.off("stderr", this.onBridgeStderr);
    this.bridge.off("error", this.onBridgeError);
    this.bridge.off("exit", this.onBridgeExit);
    this.bridge.off("reset", this.onBridgeReset);
  }

  private persistSessionReference(sessionId: string | undefined): void {
    if (sessionId) {
      this.retainedSessionMode = this.currentSessionMode();
    }
    if (!this.sessionPersistence) {
      return;
    }
    // Memento writes are asynchronous. Serialize them so a slower earlier write cannot
    // resurrect a session reference after New Session has already cleared it.
    const reference = sessionId && this.sessionWorkspace
      ? { sessionId, workspace: { ...this.sessionWorkspace }, mode: this.retainedSessionMode }
      : undefined;
    this.persistenceWritePromise = this.persistenceWritePromise
      .then(() => Promise.resolve(this.sessionPersistence?.update(reference)))
      .then(() => undefined)
      .catch((error: unknown) => {
        this.output.appendLine(`Could not persist the active Alysis Code session reference: ${protocolErrorMessage(error)}`);
      });
  }

  private clearSessionModel(): void {
    this.sessionModelRefreshGeneration += 1;
    this.sessionModel = emptySessionModelState();
    this.clearPersonas();
  }

  private async attachCollectedContext(
    result: IdeContextCollectionResult,
    label: string,
    draft: string
  ): Promise<void> {
    if (result.blocks.length === 0) {
      const reason = result.skipped[0]?.reason;
      void vscode.window.showWarningMessage(
        reason ? `No ${label} was attached (${reason}).` : `No ${label} was available to attach.`
      );
      return;
    }
    if (!this.queueContextBlocks(result.blocks)) {
      void vscode.window.showWarningMessage(`The ${label} could not be attached because the context limit was reached.`);
      return;
    }
    if (result.skipped.length > 0 || result.truncated) {
      const reasons = [...new Set(result.skipped.map((item) => item.reason))].sort().join(", ");
      this.output.appendLine(
        `Attached ${result.blocks.length} bounded ${label} block(s)` +
          (result.truncated ? "; content was truncated" : "") +
          (reasons ? `; skipped: ${redactForDisplay(reasons)}` : "")
      );
    }
    await this.prefillPrimaryChat(draft);
    void vscode.window.showInformationMessage(
      `${result.blocks.length} ${label} block${result.blocks.length === 1 ? "" : "s"} attached to the next message.`
    );
  }

  private queueContextBlocks(blocks: readonly Record<string, unknown>[]): boolean {
    const candidates: PendingContextBlock[] = [];
    for (const block of blocks) {
      const serialized = JSON.stringify(block);
      if (!serialized) {
        return false;
      }
      const bytes = Buffer.byteLength(serialized);
      const copy = JSON.parse(serialized) as Record<string, unknown>;
      if (typeof copy.type !== "string" || !copy.type || bytes > MAX_CONTEXT_BLOCK_BYTES) {
        return false;
      }
      candidates.push({ id: randomUUID(), block: copy, bytes });
    }
    const totalBytes = this.pendingContextBlocks.reduce((sum, item) => sum + item.bytes, 0) +
      candidates.reduce((sum, item) => sum + item.bytes, 0);
    if (
      this.pendingContextBlocks.length + candidates.length > MAX_PENDING_CONTEXT_BLOCKS ||
      totalBytes > MAX_PENDING_CONTEXT_BYTES
    ) {
      return false;
    }
    this.pendingContextBlocks.push(...candidates);
    return true;
  }

  private consumePendingContext(ids: readonly string[]): void {
    if (ids.length === 0) {
      return;
    }
    const consumed = new Set(ids);
    for (let index = this.pendingContextBlocks.length - 1; index >= 0; index -= 1) {
      if (consumed.has(this.pendingContextBlocks[index].id)) {
        this.pendingContextBlocks.splice(index, 1);
      }
    }
  }

  private async prefillPrimaryChat(text: string): Promise<void> {
    await this.openChat();
    await this.primaryChatView?.prefill(text);
  }

  private showNotice(text: string): void {
    const safeText = redactForDisplay(text);
    this.transcript.addNotice(safeText);
    this.publish();
  }

  /**
   * Surface one failure. The raw (redacted) backend text keeps flowing to the Output channel and the
   * runtime diagnostics feed; the transcript card the user reads is the classified projection, so a
   * protocol code, HTTP status, or stack trace never reaches the cockpit. A cancellation is an
   * expected outcome, not a failure, and is rendered as a notice.
   */
  private showError(error: unknown): void {
    const rawText = typeof error === "string" ? error : protocolErrorMessage(error);
    const safeText = redactForDisplay(rawText);
    const classified = this.classifyError(error, safeText);
    this.output.appendLine(`chat error: ${safeText}`);
    if (classified.kind === "cancelled") {
      this.transcript.addNotice(classified.detail);
      this.publishImmediately();
      return;
    }
    this.statusBar.setError(safeText);
    this.runtime?.setBridgeProcess("error", safeText);
    this.runtime?.recordError("chat", classified.title, safeText, null, classified.kind);
    this.transcript.addError(safeText, classified);
    // An error ends the turn; never leave it waiting behind a coalesced publish.
    this.publishImmediately();
  }

  /** Classify with the live runtime context so managed builds and protocol direction are honored. */
  private classifyError(error: unknown, fallbackText: string): ClassifiedChatError {
    const runtime = this.runtime?.snapshot();
    const context: ChatErrorContext = {
      ...(runtime ? { runtimeOrigin: runtime.runtimeSelection.origin } : {}),
      ...(runtime ? { protocolDirection: runtime.compatibility.protocol.direction } : {})
    };
    return classifyChatError(typeof error === "string" ? fallbackText : error, context);
  }
}

function prepareIdeContextForJob(
  blocks: readonly Record<string, unknown>[]
): Omit<CollectedIdeContext, "pendingAttachmentIds"> {
  const attachedDiagnosticFingerprints: string[] = [];
  const prepared = blocks.map((block) => {
    if (block.type !== "diagnostics" || !Array.isArray(block.items)) {
      return { ...block };
    }
    const items = block.items.map((value) => {
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        return value;
      }
      const item = { ...(value as Record<string, unknown>) };
      const fingerprint = item.verification_id;
      if (typeof fingerprint === "string" && /^[0-9a-f]{64}$/.test(fingerprint)) {
        attachedDiagnosticFingerprints.push(fingerprint);
      }
      delete item.verification_id;
      return item;
    });
    return { ...block, items };
  });
  return { blocks: prepared, attachedDiagnosticFingerprints };
}

const MAX_TRANSIENT_JOB_STATUS_FAILURES = 3;
const MAX_BUFFERED_LIVE_EVENTS_DURING_REPLAY = 1_000;
/** Roughly one animation frame: the coalescing window for streaming state publishes. */
const PUBLISH_COALESCE_MS = 16;
const VERIFICATION_SETTLE_MS = 350;
const VERIFICATION_TIMEOUT_MS = 15_000;

/** Bridge events that end (or gate) the current turn and must not wait for a coalesced publish. */
const TERMINAL_BRIDGE_EVENT_TYPES = new Set([
  "message_end",
  "error_raised",
  "prompt_for_input",
  "status_update",
  "mode_changed"
]);

function isTerminalBridgeEvent(event: ProtocolEventEnvelope): boolean {
  return TERMINAL_BRIDGE_EVENT_TYPES.has(event.type);
}

/**
 * VS Code owns cancellation tokens, but the controller is constructed in unit tests against a
 * minimal `vscode` stub. Missing the API means "no cancellation available", never a crash.
 */
function createCancellationTokenSource(): vscode.CancellationTokenSource | undefined {
  const factory = (vscode as { CancellationTokenSource?: new () => vscode.CancellationTokenSource })
    .CancellationTokenSource;
  return typeof factory === "function" ? new factory() : undefined;
}
const MAX_CONTEXT_BLOCKS = 24;
const MAX_CONTEXT_BLOCK_BYTES = 32 * 1024;
const MAX_CONTEXT_BYTES = 128 * 1024;
const MAX_PENDING_CONTEXT_BLOCKS = 16;
const MAX_PENDING_CONTEXT_BYTES = 96 * 1024;

function mergeContextBlocks(
  pending: readonly PendingContextBlock[],
  automatic: readonly Record<string, unknown>[]
): { blocks: Record<string, unknown>[]; pendingAttachmentIds: string[] } {
  const blocks: Record<string, unknown>[] = [];
  const pendingAttachmentIds: string[] = [];
  let bytes = 0;
  const append = (block: Record<string, unknown>, pendingId?: string): boolean => {
    const blockBytes = Buffer.byteLength(JSON.stringify(block));
    if (blockBytes > MAX_CONTEXT_BLOCK_BYTES || blocks.length >= MAX_CONTEXT_BLOCKS || bytes + blockBytes > MAX_CONTEXT_BYTES) {
      return false;
    }
    blocks.push(block);
    bytes += blockBytes;
    if (pendingId) {
      pendingAttachmentIds.push(pendingId);
    }
    return true;
  };
  for (const item of pending) {
    append(item.block, item.id);
  }
  for (const block of automatic) {
    if (!append({ ...block })) {
      break;
    }
  }
  return { blocks, pendingAttachmentIds };
}

function slashRouteLabel(command: string, id: string): string {
  if (!command) {
    return "unknown";
  }
  return `${command} -> ${id}`;
}

function normalizedSessionId(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const normalized = value.trim();
  return normalized.length > 0 && normalized.length <= 1024 ? normalized : undefined;
}

function normalizedSessionReference(value: unknown): { sessionId: string; workspace?: ChatWorkspaceIdentity; mode?: AlysisMode } | undefined {
  const legacySessionId = normalizedSessionId(value);
  if (legacySessionId) {
    return { sessionId: legacySessionId };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const record = value as { sessionId?: unknown; workspace?: unknown; mode?: unknown };
  const sessionId = normalizedSessionId(record.sessionId);
  const workspace = normalizedWorkspaceIdentity(record.workspace);
  return sessionId && workspace ? { sessionId, workspace, mode: typeof record.mode === "string" && isAlysisMode(record.mode) ? record.mode : undefined } : undefined;
}

function normalizedWorkspaceIdentity(value: unknown): ChatWorkspaceIdentity | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const record = value as { root?: unknown; scheme?: unknown; authority?: unknown };
  const root = typeof record.root === "string" ? record.root.trim() : "";
  const scheme = typeof record.scheme === "string" ? record.scheme.trim() : "";
  const authority = typeof record.authority === "string" ? record.authority.trim() : "";
  if (!root || root.length > 4_096 || !scheme || scheme.length > 128 || authority.length > 512) {
    return undefined;
  }
  return { root, scheme, authority };
}

function workspaceIdentityForLegacyReference(sessionId: string | undefined): ChatWorkspaceIdentity | undefined {
  if (!sessionId || vscode.workspace.workspaceFolders?.length !== 1) {
    return undefined;
  }
  return workspaceIdentity(vscode.workspace.workspaceFolders[0]);
}

function activeWorkspaceIdentity(): ChatWorkspaceIdentity | undefined {
  const folder = activeWorkspaceFolder();
  return folder ? workspaceIdentity(folder) : undefined;
}

function workspaceIdentity(folder: vscode.WorkspaceFolder): ChatWorkspaceIdentity {
  return {
    root: folder.uri.fsPath,
    scheme: folder.uri.scheme,
    authority: folder.uri.authority
  };
}

function workspaceFolderForIdentity(identity: ChatWorkspaceIdentity): vscode.WorkspaceFolder | undefined {
  return vscode.workspace.workspaceFolders?.find((folder) =>
    folder.uri.scheme === identity.scheme
    && folder.uri.authority === identity.authority
    && sameWorkspaceRoot(folder.uri.fsPath, identity.root)
  );
}

function sameWorkspaceRoot(left: string, right: string): boolean {
  return path.relative(path.resolve(left), path.resolve(right)) === "";
}

function normalizedTaskRequestId(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const normalized = value.trim();
  return normalized.length > 0 && normalized.length <= 128 ? normalized : undefined;
}

function taskRequestSignature(context: string, payload: string): string {
  return createHash("sha256").update(context).update("\0").update(payload).digest("hex");
}

function isAlysisMode(value: string | null): value is AlysisMode {
  return value === "readonly" || value === "review" || value === "auto";
}

function untrustedModeMessage(mode: string): string {
  return `Workspace Trust is required before starting Alysis Code in ${mode} mode. Switch alysis.defaultMode to readonly for this untrusted workspace or grant Workspace Trust.`;
}

/** Turn a bridge reset reason like `bridge_profile_upgrade` into words a person can read. */
function humanizeReason(reason: string): string {
  const cleaned = String(reason || "").trim().replace(/[_-]+/g, " ").toLowerCase();
  return cleaned || "unknown reason";
}

function protocolErrorMessage(error: unknown): string {
  let message: string;
  if (error instanceof FeatureCompatibilityError) {
    message = error.message;
  } else if (error instanceof ProtocolClientError) {
    message = `${error.message} (${error.code})`;
  } else if (error instanceof Error) {
    message = error.message;
  } else {
    message = String(error);
  }
  return redactForDisplay(message);
}

function sessionStartSupersededError(): ProtocolClientError {
  return new ProtocolClientError(
    "session_start_superseded",
    "Alysis Code session start was replaced by a newer session action."
  );
}

function isSessionStartSupersededError(error: unknown): boolean {
  return error instanceof ProtocolClientError && error.code === "session_start_superseded";
}

function isCancellationUnsupportedError(error: unknown): boolean {
  if (error instanceof ProtocolClientError) {
    return error.code === "cancel_not_supported";
  }
  return false;
}

function compatibilityErrorMessage(error: unknown): string {
  return protocolErrorMessage(error);
}

function sessionModelFromInfo(info: SessionModelInfoResult, switchSupported: boolean): SessionModelState {
  return {
    supported: true,
    switchSupported,
    sessionId: redactForDisplay(info.session_id),
    model: redactForDisplay(info.model),
    provider: redactForDisplay(info.provider),
    profile: info.profile ? redactForDisplay(info.profile) : null,
    source: redactForDisplay(info.source),
    error: null
  };
}

/**
 * Project the parsed session.personas.list result into the cockpit surface. The raw
 * allow_write_globs stay host-side; only the writeScoped fact ships to the webview.
 */
function personasSurfaceFromList(result: SessionPersonasListResult): PersonasSurfaceState {
  return {
    supported: true,
    reason: null,
    enabled: result.enabled,
    active: result.active,
    activeSource: result.active_source,
    available: result.personas.map((persona) => ({
      name: persona.name,
      description: redactForDisplay(persona.description),
      defaultExecMode: persona.default_exec_mode,
      modelRole: persona.model_role,
      sourceScope: persona.source_scope,
      writeScoped: persona.allow_write_globs.length > 0
    }))
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
