import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { EventEmitter } from "node:events";
import path from "node:path";
import { createInterface } from "node:readline";
import { Transform, TransformCallback } from "node:stream";
import { isStorableProfileName } from "../secrets/secretStorage";

import {
  CliDiscovery,
  AlysisConfig,
  canForwardApiKey,
  cliCommandEnvironment,
  deleteCredentialEnvironmentVariables,
  evaluateCliExecution,
  redactForDisplay,
  resolveCliExecutionPath
} from "./CliDiscovery";
import { applyHostNetworkEnvironment, applyParentProcessEnvironment } from "./hostEnvironment";
import { unsupportedShimReason } from "./ExecutableResolver";
import {
  ArtifactListResult,
  ArtifactReadResult,
  ApprovalRespondResult,
  BridgeMethod,
  BridgeHealth,
  ChatSendParams,
  CheckpointRecord,
  CodeReviewResult,
  CodeReviewScope,
  CodeReviewStartParams,
  CodeReviewStartResult,
  ConfigGetResult,
  ConfigSchemaResult,
  ConfigSetParams,
  ConfigSetResult,
  ConfigValidateParams,
  ConfigValidateResult,
  ConventionsListParams,
  ConventionsListResult,
  ConventionsRenderParams,
  ConventionsRenderResult,
  DiffGetResult,
  DiffListResult,
  DoctorBundleResult,
  DoctorProvidersLiveParams,
  DoctorProvidersLiveResult,
  DoctorProvidersResult,
  DoctorSummaryResult,
  EventReplayResult,
  ExtInfoParams,
  ExtInfoResult,
  ExtInstallParams,
  ExtListResult,
  ExtMutationParams,
  ExtMutationResult,
  ExtSearchParams,
  ExtSearchResult,
  ExtWorkspaceParams,
  ForgeAssetDetail,
  ForgeAssetEntry,
  ForgeAssetRecord,
  ForgeAssetsAddParams,
  ForgeAssetsCancelPendingResult,
  ForgeAssetsCheckPlanResult,
  ForgeAssetsDeleteParams,
  ForgeAssetsEditParams,
  ForgeAssetsListParams,
  ForgeAssetsListResult,
  ForgeAssetsMutationResult,
  ForgeAssetsPruneLegacyParams,
  ForgeAssetsPruneLegacyResult,
  ForgeAssetsRefreshParams,
  ForgeAssetsShowParams,
  ForgeAssetsShowResult,
  ForgeAttachParams,
  ForgeExecuteParams,
  ForgeExecutePreviewParams,
  ForgeExecutePreviewResult,
  ForgeExecuteResult,
  ForgeListParams,
  ForgeListResult,
  ForgeOpenParams,
  ForgeJobProgress,
  ForgePlanParams,
  ForgePlanRegenerateJobResult,
  ForgePlanRegenerateParams,
  ForgePlanRegenerateResult,
  ForgePlanRegenerateStartResult,
  ForgePlanResult,
  ForgePlanSetAssistantParams,
  ForgePlanSetGoalParams,
  ForgePlanStartResult,
  ForgePlanStateResult,
  ForgePlanUpdateTaskParams,
  ForgeReviewParams,
  ForgeReviewResult,
  ForgeReviewStartResult,
  ForgeSwarmApplyResult,
  ForgeSwarmDiscardResult,
  ForgeSwarmDurableStatus,
  ForgeSwarmJobResult,
  ForgeSwarmListParams,
  ForgeSwarmListResult,
  ForgeSwarmReconcileParams,
  ForgeSwarmReconcileResult,
  ForgeSwarmResumeParams,
  ForgeSwarmResumeResult,
  ForgeSwarmReviewResult,
  ForgeSwarmRunResult,
  ForgeSwarmStartParams,
  ForgeSwarmStartResult,
  ForgeSwarmStatusResult,
  ForgeScopedPlanParams,
  ForgeShowResult,
  HooksDoctorResult,
  HooksEffectiveParams,
  HooksEffectiveResult,
  HooksEventParams,
  HooksInitParams,
  HooksInitResult,
  HooksListResult,
  HooksTestResult,
  HooksToggleParams,
  HooksToggleResult,
  HooksTraceParams,
  HooksTraceResult,
  HooksTrustParams,
  HooksTrustResult,
  HooksWorkspaceParams,
  HOST_ACTION_NAMES,
  HostActionName,
  HostActionRespondParams,
  HostActionRespondResult,
  HostActionsNegotiation,
  HostCapabilitiesAdvertisement,
  JobStartResult,
  JobStatusResult,
  ManagementBridgeMethod,
  ManagementResult,
  ManagedBrowserArtifactReadParams,
  ManagedBrowserArtifactReadResult,
  ManagedBrowserBoundedPayload,
  ManagedBrowserClickParams,
  ManagedBrowserClickResult,
  ManagedBrowserCloseParams,
  ManagedBrowserCloseResult,
  ManagedBrowserDiagnosticEvent,
  ManagedBrowserDiagnosticsParams,
  ManagedBrowserDiagnosticsResult,
  ManagedBrowserListResult,
  ManagedBrowserNavigateParams,
  ManagedBrowserNavigateResult,
  ManagedBrowserScreenshotParams,
  ManagedBrowserScreenshotResult,
  ManagedBrowserSessionParams,
  ManagedBrowserSessionStatus,
  ManagedBrowserSnapshotParams,
  ManagedBrowserSnapshotResult,
  ManagedBrowserStartParams,
  ManagedBrowserStatusResult,
  ManagedBrowserTypeParams,
  ManagedBrowserTypeResult,
  McpAuthLoginStartParams,
  McpAuthLoginStartResult,
  McpAuthFlowResult,
  McpAuthLoginCancelParams,
  McpAuthLoginFlowParams,
  McpAuthLogoutParams,
  McpAuthLogoutResult,
  McpAuthStatusParams,
  McpAuthStatusResult,
  McpPromptsGetParams,
  McpPromptsGetResult,
  McpPromptsListParams,
  McpPromptsListResult,
  McpServerMutationAction,
  McpServerMutationParams,
  McpServerMutationResult,
  McpServerStatusParams,
  McpServerStatusResult,
  McpStatusResult,
  McpWorkspaceParams,
  ProfileAddParams,
  ProfileConvertParams,
  ProfileListResult,
  ProfileMutationResult,
  ProfileNameParams,
  ProfilePresetParams,
  ProfilePresetsResult,
  ProfileRemoveParams,
  ProfileRenameParams,
  ProfileShowResult,
  PromptQueueItem,
  PromptQueueListResult,
  ChatSteerResult,
  ChatSteerTakeResult,
  FeedbackPrepareResult,
  FeedbackSendResult,
  PermissionEvaluation,
  PermissionRule,
  TrustedProfileNameParams,
  ProfileUseResult,
  ProtocolEventEnvelope,
  ProtocolResponse,
  ReportCreateParams,
  ReportCreateResult,
  RetainedSessionParams,
  RunStartParams,
  SandboxDoctorParams,
  SandboxDoctorResult,
  SandboxMutationResult,
  SandboxPullParams,
  SandboxSetupParams,
  SessionClearResult,
  SessionCompactResult,
  SessionContextResult,
  SessionCreateParams,
  SessionCreateResult,
  SessionHistoryResult,
  SessionSearchResult,
  StructuredQuestion,
  StructuredQuestionSet,
  StructuredTaskItem,
  StructuredTaskLedger,
  SessionImagesAddParams,
  SessionImagesAddResult,
  SessionImagesClearResult,
  SessionImagesListResult,
  SessionListResult,
  SessionModelInfoResult,
  SessionPersonaSetParams,
  SessionPersonaSetResult,
  SessionPersonasListParams,
  SessionPersonasListResult,
  SessionResumeResult,
  SessionPermissionGrant,
  SessionScoreParams,
  SessionScoreResult,
  SessionShowResult,
  SessionSetActiveWorkdirResult,
  SessionSubagentsStatusResult,
  SessionTerminalShowResult,
  SessionTerminalsListResult,
  SessionTraceArtifactResult,
  SessionTraceClearResult,
  SessionTraceEventsResult,
  SessionTraceLevel,
  SessionTraceStatusResult,
  SessionStatusResult,
  SessionUsageResult,
  SkillInfoParams,
  SkillInfoResult,
  SkillInitParams,
  SkillInstallParams,
  SkillListResult,
  SkillRemoveParams,
  SkillToggleParams,
  SkillToggleResult,
  SkillValidateParams,
  SkillValidateResult,
  ToolInfoParams,
  ToolInfoResult,
  ToolListResult,
  ToolTrustParams,
  ToolTrustResult,
  ToolsCatalogResult,
  UpdateCheckParams,
  UpdateCheckResult,
  WorkspaceScopedParams,
  isRecord,
  hasBridgeMethod,
  parseHealthRecord,
  parseSessionPersonaSetResult,
  parseSessionPersonasListResult,
  protocolRequest
} from "./AlysisProtocol";

const REQUEST_TIMEOUT_MS = 120_000;
const SHORT_REQUEST_TIMEOUT_MS = 30_000;
const LONG_REQUEST_TIMEOUT_MS = 600_000;
const PROCESS_START_TIMEOUT_MS = 5_000;
const INITIALIZE_TIMEOUT_MS = 10_000;
const PROCESS_STOP_TIMEOUT_MS = 2_000;
const GRACEFUL_BRIDGE_EXIT_TIMEOUT_MS = 3_000;
const GRACEFUL_SHUTDOWN_TIMEOUT_MS = 1_500;
const SHUTDOWN_CANCEL_SETTLE_TIMEOUT_MS = 1_000;
const SHUTDOWN_CANCEL_POLL_INTERVAL_MS = 100;
const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "cancellation_requested"]);
const FINAL_JOB_STATUSES = new Set(["completed", "failed", "cancelled"]);
// The CLI rejects any request frame above 1 MiB with an id:null error, so refuse to send one at all
// and report the real problem instead of a bridge-level rejection.
const MAX_OUTBOUND_REQUEST_BYTES = 1_000_000;
// A runaway CLI must not be able to grow the Extension Host heap without bound: inbound frames are
// assembled with a hard cap, oversize frames are dropped, and stderr forwarding is clipped per chunk
// and rate limited per window.
const MAX_INBOUND_FRAME_BYTES = 8 * 1024 * 1024;
const MAX_STDERR_CHUNK_CHARS = 8 * 1024;
const MAX_STDERR_WINDOW_CHARS = 128 * 1024;
const STDERR_WINDOW_MS = 10_000;
// Stray CLI stdout (a pip warning, a stray print) is a diagnostic, not a protocol failure. Only a
// sustained run of unreadable frames — or a frame that claims to be protocol output — is escalated.
const MAX_CONSECUTIVE_FRAME_PARSE_FAILURES = 5;
const MAX_LOGGED_FRAME_PREVIEW_CHARS = 512;

// Requests whose deadline must not be the generic default. `initialize` is a handshake that either
// answers immediately or never will; `bridge.shutdown` is teardown that must not wedge disposal.
const REQUEST_TIMEOUT_OVERRIDES = new Map<string, number>([
  ["initialize", INITIALIZE_TIMEOUT_MS],
  ["bridge.shutdown", GRACEFUL_BRIDGE_EXIT_TIMEOUT_MS]
]);

// Methods that run model-backed work inline. Failing these at the 2-minute default abandons a CLI
// that keeps running (and keeps spending tokens) with no way for the caller to reattach.
const LONG_REQUEST_METHODS = new Set<string>([
  "forge.plan",
  "forge.plan.result",
  "forge.plan.regenerate",
  "forge.plan.regenerate.result",
  "forge.review",
  "forge.review.result",
  "forge.execute",
  "forge.executePreview",
  "forge.swarm.result",
  "code.review.result",
  "session.compact"
]);

// Read-shaped methods answer from local CLI state. Thirty seconds is generous for them and keeps a
// wedged bridge from holding a view for two minutes.
const SHORT_REQUEST_METHOD_SEGMENTS: readonly string[] = [
  "list",
  "status",
  "show",
  "info",
  "get",
  "schema",
  "validate",
  "catalog",
  "effective",
  "presets",
  "usage",
  "modelInfo"
];

// Config-shaped calls named exactly. Persona list answers from local CLI state and persona set is a
// quick mode/model rebind — neither runs model-backed work, so both take the short deadline.
const SHORT_REQUEST_METHODS = new Set<string>([
  "session.personas.list",
  "session.persona.set"
]);

/**
 * Per-method request deadline. Keyed by method name so every call site — including the generic
 * managementRequest() funnel — gets the right policy without repeating it.
 */
export function requestTimeoutForMethod(method: string): number {
  const override = REQUEST_TIMEOUT_OVERRIDES.get(method);
  if (override !== undefined) {
    return override;
  }
  if (LONG_REQUEST_METHODS.has(method)) {
    return LONG_REQUEST_TIMEOUT_MS;
  }
  const segment = method.slice(method.lastIndexOf(".") + 1);
  if (
    SHORT_REQUEST_METHODS.has(method) ||
    method.startsWith("config.") ||
    SHORT_REQUEST_METHOD_SEGMENTS.includes(segment)
  ) {
    return SHORT_REQUEST_TIMEOUT_MS;
  }
  return REQUEST_TIMEOUT_MS;
}

export interface BridgeProcess extends EventEmitter {
  stdin: NodeJS.WritableStream & { writable?: boolean };
  stdout: NodeJS.ReadableStream;
  stderr: NodeJS.ReadableStream;
  kill(signal?: NodeJS.Signals | number): boolean;
  pid?: number;
  exitCode?: number | null;
  signalCode?: NodeJS.Signals | null;
  alysisProcessGroupIsolated?: boolean;
}

export interface BridgeProcessFactoryOptions {
  env: NodeJS.ProcessEnv;
}

export interface BridgeStartOptions {
  apiKey?: string;
  stripApiKey?: boolean;
  /**
   * Provider-scoped credentials to export under an IDE override and the declared api_key_env.
   * The override wins over stale CLI storage and isolates profiles sharing the same env var.
   * Required because the CLI ignores ALYSIS_API_KEY for profiles other than default/openai and will
   * only accept a profile-scoped source. Subject to the same forwarding gate as apiKey.
   */
  providerCredentials?: ReadonlyArray<{ profile?: string; envVar: string; value: string }>;
}

export interface BridgeRequiredProfile {
  credentialsRequired?: boolean;
}

export interface BridgeLaunchProfile {
  resolvedExecutablePath: string;
  apiKeyForwardingAllowed: boolean;
  stripApiKey: boolean;
  alysisApiKeyForwarded: boolean;
  credentialCapable: boolean;
  model: string;
  baseUrl: string;
  workspaceTrusted: boolean | null;
  executableTrusted: boolean | null;
  executableOrigin: string;
}

export interface BridgeProfileStartResult {
  restarted: boolean;
  profile: BridgeLaunchProfile;
}

export type BridgeProcessFactory = (
  command: string,
  args: readonly string[],
  options: BridgeProcessFactoryOptions
) => BridgeProcess;

export interface BridgeClientEvents {
  event: [ProtocolEventEnvelope];
  response: [ProtocolResponse];
  stderr: [string];
  error: [Error];
  exit: [number | null];
  reset: [string];
  // FE-19: emitted whenever the cached live handshake health is set or cleared, so the cockpit runtime
  // recomputes capability compatibility from the freshest capabilities the instant the bridge connects
  // or drops. (Strongly typed via the on/off/emit overloads below so a typo or payload drift is caught.)
  health: [BridgeHealth | undefined];
  hostActionsNegotiated: [string, string, HostActionsNegotiation];
}

export interface HostCapabilityAdvertisementProviderResult {
  workspace_trusted: boolean;
  host_capabilities: HostCapabilitiesAdvertisement;
}

interface PendingRequest {
  method: string;
  resolve: (value: Record<string, unknown>) => void;
  reject: (reason: Error) => void;
  timeout: NodeJS.Timeout;
}

export class ProtocolClientError extends Error {
  public constructor(
    public readonly code: string,
    message: string,
    public readonly details?: Record<string, unknown>
  ) {
    super(redactForDisplay(message));
    this.name = "ProtocolClientError";
  }
}

export class AlysisBridgeClient extends EventEmitter {
  private process: BridgeProcess | undefined;
  private startPromise: Promise<void> | undefined;
  private lifecyclePromise: Promise<unknown> | undefined;
  private terminatingProcess: BridgeProcess | undefined;
  private terminationPromise: Promise<void> | undefined;
  private consecutiveFrameParseFailures = 0;
  private stderrWindowStartedAt = 0;
  private stderrWindowChars = 0;
  private stderrSuppressedChunks = 0;
  private nextRequestId = 1;
  private readonly pending = new Map<string | number, PendingRequest>();
  private readonly activeJobIds = new Set<string>();
  private readonly pendingApprovalIds = new Set<string>();
  private readonly latestSequenceBySession = new Map<string, number>();
  private launchProfile: BridgeLaunchProfile | undefined;
  private launchCredentialFingerprint: string | null = null;
  private bridgeHealth: BridgeHealth | undefined;
  private initialized = false;
  private hostCapabilityProvider: (() => HostCapabilityAdvertisementProviderResult) | undefined;

  public constructor(
    private readonly cliDiscovery: CliDiscovery,
    private readonly processFactory: BridgeProcessFactory = defaultBridgeProcessFactory
  ) {
    super();
    this.bindPrototypeMethods();
  }

  // FE-19: strongly-typed event surface for the known events (a `heath` typo or a payload-shape drift on
  // "health" would otherwise compile and silently never fire). Dynamic session:<id>/job:<id> events fall
  // through the string overload.
  public on<K extends keyof BridgeClientEvents>(event: K, listener: (...args: BridgeClientEvents[K]) => void): this;
  public on(event: string | symbol, listener: (...args: never[]) => void): this;
  public on(event: string | symbol, listener: (...args: never[]) => void): this {
    return super.on(event, listener as (...args: unknown[]) => void);
  }
  public off<K extends keyof BridgeClientEvents>(event: K, listener: (...args: BridgeClientEvents[K]) => void): this;
  public off(event: string | symbol, listener: (...args: never[]) => void): this;
  public off(event: string | symbol, listener: (...args: never[]) => void): this {
    return super.off(event, listener as (...args: unknown[]) => void);
  }
  public emit<K extends keyof BridgeClientEvents>(event: K, ...args: BridgeClientEvents[K]): boolean;
  public emit(event: string | symbol, ...args: unknown[]): boolean;
  public emit(event: string | symbol, ...args: unknown[]): boolean {
    return super.emit(event, ...args);
  }

  public async health(config: AlysisConfig): Promise<BridgeHealth> {
    const guard = evaluateCliExecution(config);
    if (!guard.allowed) {
      throw new ProtocolClientError(
        "cli_untrusted",
        guard.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust."
      );
    }
    return this.cliDiscovery.health(resolveCliExecutionPath(config), cliCommandEnvironment(config));
  }

  public isRunning(): boolean {
    return this.process !== undefined && this.initialized;
  }

  public canRestartSafely(): boolean {
    return this.pending.size === 0 && this.activeJobIds.size === 0 && this.pendingApprovalIds.size === 0;
  }

  public setHostCapabilityProvider(
    provider: (() => HostCapabilityAdvertisementProviderResult) | undefined
  ): void {
    this.hostCapabilityProvider = provider;
  }

  public async ensureStarted(
    config: AlysisConfig,
    options: BridgeStartOptions = {}
  ): Promise<void> {
    await this.startStdio(config, options);
  }

  public async ensureStartedForProfile(
    config: AlysisConfig,
    options: BridgeStartOptions = {},
    requiredProfile: BridgeRequiredProfile = {}
  ): Promise<BridgeProfileStartResult> {
    const guard = evaluateCliExecution(config);
    if (!guard.allowed) {
      throw new ProtocolClientError(
        "cli_untrusted",
        guard.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust."
      );
    }
    const requestedLaunch = createBridgeLaunch(config, options);
    assertLaunchProfileSupportsRequirement(requestedLaunch.profile, requiredProfile);
    return this.withProcessLifecycle(() => this.startForProfile(config, options, requiredProfile, requestedLaunch));
  }

  /** All launch paths share the stop/start boundary, including passive catalog refreshes. */
  private async withProcessLifecycle<T>(action: () => Promise<T>): Promise<T> {
    const previous = this.lifecyclePromise ?? Promise.resolve();
    const started = previous.catch(() => undefined).then(action);
    this.lifecyclePromise = started;
    try {
      return await started;
    } finally {
      if (this.lifecyclePromise === started) {
        this.lifecyclePromise = undefined;
      }
    }
  }

  private async startForProfile(
    config: AlysisConfig,
    options: BridgeStartOptions,
    requiredProfile: BridgeRequiredProfile,
    requestedLaunch: { profile: BridgeLaunchProfile; credentialFingerprint: string | null }
  ): Promise<BridgeProfileStartResult> {
    const requested = requestedLaunch.profile;
    if (this.startPromise) {
      await this.startPromise;
    }
    if (!this.process || !this.initialized) {
      await this.ensureProcessStarted(config, options);
      return {
        restarted: false,
        profile: this.liveProfileSatisfying(requested, requiredProfile, requestedLaunch.credentialFingerprint)
      };
    }
    const current = this.launchProfile;
    if (
      current &&
      launchProfileSatisfies(
        current,
        requested,
        requiredProfile,
        this.launchCredentialFingerprint,
        requestedLaunch.credentialFingerprint
      )
    ) {
      return { restarted: false, profile: current };
    }
    if (!this.canRestartSafely()) {
      throw new ProtocolClientError(
        "bridge_profile_restart_blocked",
        "Alysis Code bridge launch settings changed and cannot be applied while work is active."
      );
    }
    await this.stopProcess("bridge_profile_upgrade", false);
    await this.ensureProcessStarted(config, options);
    return {
      restarted: true,
      profile: this.liveProfileSatisfying(requested, requiredProfile, requestedLaunch.credentialFingerprint)
    };
  }

  /**
   * Re-check the live launch profile after every await before reporting success. A bridge someone
   * else started (or restarted) must never be reported as this caller's requested profile.
   */
  private liveProfileSatisfying(
    requested: BridgeLaunchProfile,
    requiredProfile: BridgeRequiredProfile,
    requestedCredentialFingerprint: string | null
  ): BridgeLaunchProfile {
    const current = this.launchProfile;
    if (
      !current ||
      !this.initialized ||
      !launchProfileSatisfies(
        current,
        requested,
        requiredProfile,
        this.launchCredentialFingerprint,
        requestedCredentialFingerprint
      )
    ) {
      throw new ProtocolClientError(
        "bridge_profile_mismatch",
        "Alysis Code bridge is running with different launch settings than this request required."
      );
    }
    return current;
  }

  public currentLaunchProfile(): BridgeLaunchProfile | undefined {
    return this.launchProfile;
  }

  // The live capabilities from the most recent `initialize` handshake — the SAME source supportsMethod
  // reads, so it is the truth about what the connected CLI can do (undefined when no bridge is up).
  public currentHealth(): BridgeHealth | undefined {
    return this.bridgeHealth;
  }

  // Single writer for the cached live health. Emits "health" so the cockpit runtime can recompute
  // compatibility from the freshest capabilities the instant the bridge connects or drops — not only on
  // an explicit out-of-band detection probe.
  private setBridgeHealth(health: BridgeHealth | undefined): void {
    this.bridgeHealth = health;
    this.emit("health", health);
  }

  public supportsMethod(method: BridgeMethod): boolean {
    return this.bridgeHealth ? hasBridgeMethod(this.bridgeHealth.capabilities, method) : false;
  }

  public supportsEvent(eventType: string): boolean {
    return this.bridgeHealth?.capabilities.events.includes(eventType) === true;
  }

  public supportsFeature(path: readonly string[]): boolean {
    return this.featureValue(path) === true;
  }

  public featureValue(path: readonly string[]): unknown {
    let current: unknown = this.bridgeHealth?.capabilities.features;
    for (const segment of path) {
      if (!isRecord(current)) {
        return undefined;
      }
      current = current[segment];
    }
    return current;
  }

  public async startStdio(
    config: AlysisConfig,
    options: BridgeStartOptions = {}
  ): Promise<void> {
    await this.withProcessLifecycle(() => this.ensureProcessStarted(config, options));
  }

  /** Called only while owning the lifecycle queue, so internal starts must not enqueue again. */
  private async ensureProcessStarted(config: AlysisConfig, options: BridgeStartOptions): Promise<void> {
    await this.waitForProcessTermination();
    if (this.process && this.initialized) {
      return;
    }
    if (this.startPromise) {
      await this.startPromise;
      return;
    }

    const startPromise = this.startStdioOnce(config, options);
    this.startPromise = startPromise;
    try {
      await startPromise;
    } finally {
      if (this.startPromise === startPromise) {
        this.startPromise = undefined;
      }
    }
  }

  private async startStdioOnce(
    config: AlysisConfig,
    options: BridgeStartOptions
  ): Promise<void> {
    if (this.process) {
      throw new ProtocolClientError(
        "bridge_start_incomplete",
        "Alysis Code IDE bridge exists but has not completed its initialize handshake."
      );
    }

    const guard = evaluateCliExecution(config);
    if (!guard.allowed) {
      throw new ProtocolClientError(
        "cli_untrusted",
        guard.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust."
      );
    }
    const launch = createBridgeLaunch(config, options);
    const cliPath = launch.profile.resolvedExecutablePath;
    // A .cmd/.bat shim cannot be spawned without a shell (CVE-2024-27980 hardening), and enabling a
    // shell is not an option here. Report the wrapper by name instead of leaving a bare EINVAL.
    const shimReason = unsupportedShimReason(cliPath);
    if (shimReason) {
      throw new ProtocolClientError("cli_shim_unsupported", shimReason);
    }
    const child = this.processFactory(cliPath, ["ide-bridge", "--stdio"], {
      env: launch.env
    });
    this.process = child;

    const frames = new BoundedFrameStream(MAX_INBOUND_FRAME_BYTES, (bytes) =>
      this.noteOversizeFrame(bytes)
    );
    child.stdout.pipe(frames);
    const lines = createInterface({ input: frames });
    lines.on("line", (line) => this.handleBridgeLine(line));
    // Every stream that can emit "error" needs a listener: an unhandled "error" event throws out of
    // the event loop and takes the whole Extension Host — and every other extension — down with it.
    // readline re-emits its input's errors on the Interface, so it needs one of its own.
    lines.on("error", (error: Error) => this.handleProcessError(child, error));
    child.stdout.on("error", (error: Error) => this.handleProcessError(child, error));
    child.stderr.on("data", (chunk: Buffer | string) => this.forwardStderrChunk(chunk));
    child.stderr.on("error", (error: Error) => {
      // stderr carries diagnostics only: a broken diagnostic pipe must not tear down a live bridge.
      this.emit("stderr", `Alysis Code bridge diagnostics stream failed: ${redactForDisplay(error.message)}`);
    });
    child.stdin.on("error", (error: Error) => this.handleProcessError(child, error));
    child.on("error", (error) => this.handleProcessError(child, error));
    child.on("exit", (code) => this.handleProcessExit(child, code));
    child.on("close", (code) => this.handleProcessExit(child, code));

    try {
      await this.waitForProcessStart(child);
      await this.initializeLiveBridge();
      this.launchProfile = launch.profile;
      this.launchCredentialFingerprint = launch.credentialFingerprint;
    } catch (error) {
      if (this.process === child) {
        this.process = undefined;
      }
      this.initialized = false;
      this.launchProfile = undefined;
      this.launchCredentialFingerprint = null;
      this.setBridgeHealth(undefined);
      await this.beginProcessTermination(child);
      throw error;
    }
  }

  public async restartIfSafe(
    config: AlysisConfig,
    options: BridgeStartOptions = {}
  ): Promise<boolean> {
    return this.withProcessLifecycle(async () => {
      if (!this.canRestartSafely()) {
        return false;
      }
      await this.stopProcess("bridge_restarting", false);
      await this.ensureProcessStarted(config, options);
      return true;
    });
  }

  private async request(
    method: string,
    params: Record<string, unknown> = {},
    timeoutMs = requestTimeoutForMethod(method)
  ): Promise<Record<string, unknown>> {
    const child = this.process;
    if (!child || child.stdin.writable === false) {
      throw new ProtocolClientError("bridge_not_running", "Alysis Code IDE bridge is not running.");
    }
    if (method !== "initialize" && !this.initialized) {
      throw new ProtocolClientError(
        "bridge_not_initialized",
        "Alysis Code IDE bridge has not completed the initialize handshake."
      );
    }
    const id = `vscode-${this.nextRequestId++}`;
    const payload = JSON.stringify(protocolRequest(id, method, params)) + "\n";
    const payloadBytes = Buffer.byteLength(payload, "utf8");
    if (payloadBytes > MAX_OUTBOUND_REQUEST_BYTES) {
      // The bridge answers an oversized frame with an uncorrelated error, so fail fast with the
      // real cause instead of writing a request that can only be rejected.
      throw new ProtocolClientError(
        "request_too_large",
        `Alysis Code bridge request is too large to send: ${method} would write ${payloadBytes} bytes, above the ${MAX_OUTBOUND_REQUEST_BYTES}-byte frame limit. Reduce the attached context or selection and try again.`
      );
    }

    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(
          new ProtocolClientError(
            "request_timeout",
            `Alysis Code bridge request timed out: ${method}.`
          )
        );
      }, timeoutMs);
      this.pending.set(id, { method, resolve, reject, timeout });
      try {
        child.stdin.write(payload, (error?: Error | null) => {
          if (!error) {
            return;
          }
          const current = this.pending.get(id);
          if (!current) {
            return;
          }
          clearTimeout(current.timeout);
          this.pending.delete(id);
          current.reject(
            new ProtocolClientError(
              "bridge_write_failed",
              `Alysis Code bridge request could not be written: ${method}: ${redactForDisplay(error.message)}`
            )
          );
        });
      } catch (error) {
        clearTimeout(timeout);
        this.pending.delete(id);
        reject(
          new ProtocolClientError(
            "bridge_write_failed",
            `Alysis Code bridge request could not be written: ${method}: ${redactForDisplay(
              error instanceof Error ? error.message : String(error)
            )}`
          )
        );
      }
    });
  }

  public async createSession(
    params: SessionCreateParams | Record<string, unknown>
  ): Promise<SessionCreateResult> {
    if (!this.initialized) {
      throw new ProtocolClientError(
        "bridge_not_initialized",
        "Alysis Code IDE bridge has not completed the initialize handshake."
      );
    }
    const result = await this.request("session.create", this.withHostCapabilities(params));
    const session: SessionCreateResult = {
      session_id: requireString(result, "session_id"),
      workspace_root: requireString(result, "workspace_root"),
      mode: normalizeMode(requireString(result, "mode")),
      sandbox_mode: result.sandbox_mode === "strict" || result.sandbox_mode === "warn" || result.sandbox_mode === "off"
        ? result.sandbox_mode : undefined,
      host_actions: isRecord(result.host_actions)
        ? hostActionsNegotiationFromRecord(result.host_actions)
        : undefined
    };
    if (session.host_actions) {
      this.emit("hostActionsNegotiated", session.session_id, session.workspace_root, session.host_actions);
    }
    return session;
  }

  public async sendChat(
    sessionId: string,
    message: string,
    options: Omit<ChatSendParams, "session_id" | "message" | "instruction"> = {}
  ): Promise<JobStartResult> {
    return this.trackStartedJob(
      await this.request("chat.send", { ...options, session_id: sessionId, message })
    );
  }

  public async chatQueueList(
    sessionId: string,
    options: { states?: string[]; limit?: number; after_sequence?: number } = {}
  ): Promise<PromptQueueListResult> {
    const result = await this.request("chat.queue.list", { session_id: sessionId, ...options });
    return {
      session_id: requireString(result, "session_id"),
      items: Array.isArray(result.items)
        ? result.items.filter(isRecord).map(promptQueueItemFromRecord)
        : [],
      next_sequence: typeof result.next_sequence === "number" ? result.next_sequence : 0
    };
  }

  public async chatQueueGet(sessionId: string, promptId: string): Promise<PromptQueueItem> {
    return promptQueueItemFromRecord(
      await this.request("chat.queue.get", { session_id: sessionId, prompt_id: promptId })
    );
  }

  public async chatQueueDelete(sessionId: string, promptId: string): Promise<PromptQueueItem> {
    return promptQueueItemFromRecord(
      await this.request("chat.queue.delete", { session_id: sessionId, prompt_id: promptId })
    );
  }

  public async chatSteer(sessionId: string, message: string): Promise<ChatSteerResult> {
    const result = await this.request("chat.steer", { session_id: sessionId, message });
    return {
      session_id: requireString(result, "session_id"),
      job_id: requireString(result, "job_id"),
      accepted: result.accepted === true,
      pending: requireNumber(result, "pending"),
      dropped: requireNumber(result, "dropped"),
      truncated: result.truncated === true
    };
  }

  public async chatSteerTake(sessionId: string): Promise<ChatSteerTakeResult> {
    const result = await this.request("chat.steer.take", { session_id: sessionId });
    return {
      session_id: requireString(result, "session_id"),
      messages: Array.isArray(result.messages)
        ? result.messages.filter((value): value is string => typeof value === "string")
        : []
    };
  }

  public async feedbackPrepare(params: {
    feedback: string;
    sessionId?: string;
    workspace?: string;
    sessionDir?: string;
    includeDiagnostics?: boolean;
  }): Promise<FeedbackPrepareResult> {
    const result = await this.request("feedback.prepare", {
      feedback: params.feedback,
      ...(params.sessionId ? { session_id: params.sessionId } : {}),
      ...(params.workspace ? { workspace: params.workspace } : {}),
      ...(params.sessionDir ? { session_dir: params.sessionDir } : {}),
      include_diagnostics: params.includeDiagnostics === true
    });
    return {
      report_id: requireString(result, "report_id"),
      report: isRecord(result.report) ? result.report : {},
      include_diagnostics: result.include_diagnostics === true,
      signed_in: result.signed_in === true
    };
  }

  public async feedbackSend(reportId: string): Promise<FeedbackSendResult> {
    const result = await this.request("feedback.send", { report_id: reportId });
    return {
      report_id: requireString(result, "report_id"),
      created_at: requireString(result, "created_at"),
      anonymous: result.anonymous === true
    };
  }

  public async checkpointList(
    sessionId: string,
    limit = 100
  ): Promise<CheckpointRecord[]> {
    const result = await this.request("checkpoint.list", { session_id: sessionId, limit });
    return Array.isArray(result.checkpoints)
      ? result.checkpoints.filter(isRecord).map(checkpointFromRecord)
      : [];
  }

  public async checkpointDiff(
    sessionId: string,
    checkpointId: string,
    maxBytes?: number
  ): Promise<Record<string, unknown>> {
    return await this.request("checkpoint.diff", {
      session_id: sessionId,
      checkpoint_id: checkpointId,
      ...(maxBytes === undefined ? {} : { max_bytes: maxBytes })
    });
  }

  public async checkpointRevert(
    sessionId: string,
    checkpointId: string
  ): Promise<CheckpointRecord> {
    const result = await this.request("checkpoint.revert", {
      session_id: sessionId,
      checkpoint_id: checkpointId,
      workspace_trusted: true,
      confirm: true
    });
    return checkpointFromRecord(requiredRecord(result, "checkpoint"));
  }

  public async checkpointRedo(sessionId: string): Promise<CheckpointRecord> {
    const result = await this.request("checkpoint.redo", {
      session_id: sessionId,
      workspace_trusted: true,
      confirm: true
    });
    return checkpointFromRecord(requiredRecord(result, "checkpoint"));
  }

  public async checkpointBranch(
    sessionId: string,
    checkpointId: string,
    name: string
  ): Promise<{ checkpoint_id: string; ref: string }> {
    const result = await this.request("checkpoint.branch", {
      session_id: sessionId,
      checkpoint_id: checkpointId,
      name
    });
    return {
      checkpoint_id: requireString(result, "checkpoint_id"),
      ref: requireString(result, "ref")
    };
  }

  public async sessionTasksGet(
    sessionId: string,
    targetSessionId?: string
  ): Promise<StructuredTaskLedger> {
    return structuredTaskLedgerFromRecord(
      await this.request("session.tasks.get", {
        session_id: sessionId,
        ...(targetSessionId ? { target_session_id: targetSessionId } : {})
      })
    );
  }

  public async sessionTasksReplace(
    sessionId: string,
    expectedRevision: number,
    tasks: StructuredTaskItem[],
    workspaceTrusted: boolean
  ): Promise<StructuredTaskLedger> {
    return structuredTaskLedgerFromRecord(
      await this.request("session.tasks.replace", {
        session_id: sessionId,
        expected_revision: expectedRevision,
        tasks,
        workspace_trusted: workspaceTrusted
      })
    );
  }

  public async sessionQuestionsCreate(params: {
    session_id: string;
    idempotency_key: string;
    questions: StructuredQuestion[];
    expires_in_seconds?: number;
    workspace_trusted: boolean;
  }): Promise<StructuredQuestionSet> {
    return structuredQuestionSetFromRecord(
      await this.request("session.questions.create", params)
    );
  }

  public async sessionQuestionsGet(
    sessionId: string,
    questionSetId: string
  ): Promise<StructuredQuestionSet> {
    return structuredQuestionSetFromRecord(
      await this.request("session.questions.get", {
        session_id: sessionId,
        question_set_id: questionSetId
      })
    );
  }

  public async sessionQuestionsList(
    sessionId: string,
    options: { statuses?: string[]; limit?: number } = {}
  ): Promise<StructuredQuestionSet[]> {
    const result = await this.request("session.questions.list", {
      session_id: sessionId,
      ...options
    });
    return Array.isArray(result.question_sets)
      ? result.question_sets.filter(isRecord).map((item) =>
          structuredQuestionSetFromRecord({ ...item, session_id: sessionId })
        )
      : [];
  }

  public async sessionQuestionsAnswer(
    sessionId: string,
    questionSetId: string,
    answers: Record<string, string>,
    workspaceTrusted: boolean
  ): Promise<StructuredQuestionSet> {
    return structuredQuestionSetFromRecord(
      await this.request("session.questions.answer", {
        session_id: sessionId,
        question_set_id: questionSetId,
        answers,
        workspace_trusted: workspaceTrusted
      })
    );
  }

  public async sessionQuestionsCancel(
    sessionId: string,
    questionSetId: string,
    workspaceTrusted: boolean
  ): Promise<StructuredQuestionSet> {
    return structuredQuestionSetFromRecord(
      await this.request("session.questions.cancel", {
        session_id: sessionId,
        question_set_id: questionSetId,
        workspace_trusted: workspaceTrusted
      })
    );
  }

  public async permissionRulesList(): Promise<PermissionRule[]> {
    const result = await this.request("permission.rules.list", {});
    return Array.isArray(result.rules)
      ? result.rules.filter(isRecord).map(permissionRuleFromRecord)
      : [];
  }

  public async permissionRuleGrant(params: {
    effect: PermissionRule["effect"];
    tool_pattern?: string;
    path_pattern?: string;
    command_pattern?: string;
  }): Promise<PermissionRule> {
    const result = await this.request("permission.rules.grant", { ...params, confirm: true });
    return permissionRuleFromRecord(requiredRecord(result, "rule"));
  }

  public async permissionRuleRevoke(ruleId: string): Promise<boolean> {
    const result = await this.request("permission.rules.revoke", {
      rule_id: ruleId,
      confirm: true
    });
    return result.status === "revoked";
  }

  public async permissionEvaluate(params: {
    tool_name: string;
    paths?: string[];
    command?: string;
    workspace?: string;
  }): Promise<PermissionEvaluation> {
    return permissionEvaluationFromRecord(await this.request("permission.evaluate", params));
  }

  public async permissionSessionList(sessionId: string): Promise<SessionPermissionGrant[]> {
    const result = await this.request("permission.session.list", { session_id: sessionId });
    return Array.isArray(result.grants)
      ? result.grants.filter(isRecord).map(sessionPermissionGrantFromRecord)
      : [];
  }

  public async permissionSessionRevoke(sessionId: string, grantId: string): Promise<boolean> {
    const result = await this.request("permission.session.revoke", {
      session_id: sessionId,
      grant_id: grantId
    });
    return result.status === "revoked";
  }

  public async codeReviewStart(params: CodeReviewStartParams): Promise<CodeReviewStartResult> {
    const result = await this.request("code.review.start", params as unknown as Record<string, unknown>);
    const started = this.trackStartedJob(result);
    return {
      ...started,
      scope: normalizeCodeReviewScope(result.scope)
    };
  }

  public async codeReviewResult(jobId: string): Promise<CodeReviewResult> {
    return codeReviewResultFromRecord(
      await this.request("code.review.result", { job_id: jobId })
    );
  }

  public async startRun(params: RunStartParams): Promise<JobStartResult> {
    // Host capabilities are negotiated per session. When run.start joins an existing session the
    // negotiation already happened in session.create, and the bridge treats `host_capabilities`
    // and `workspace_trusted` as session options — sending them again is `unsupported_turn_option`.
    const joinsExistingSession = typeof (params as Record<string, unknown>).session_id === "string";
    const request = joinsExistingSession
      ? { ...(params as unknown as Record<string, unknown>) }
      : this.withHostCapabilities(params);
    return this.trackStartedJob(await this.request("run.start", request));
  }

  public async hostActionRespond(params: HostActionRespondParams): Promise<HostActionRespondResult> {
    if (
      !/^ha_[a-f0-9]{32}$/.test(params.host_action_id)
      || !/^wf_[A-Za-z0-9_-]{1,252}$/.test(params.workspace_fence)
      || !/^[a-f0-9]{64}$/i.test(params.capability_fingerprint)
      || (params.ok ? !isRecord(params.result) || params.error !== undefined : !isRecord(params.error) || params.result !== undefined)
    ) {
      throw new ProtocolClientError("invalid_request", "VS Code produced an invalid host action response envelope.");
    }
    const result = await this.request(
      "host.action.respond",
      params as unknown as Record<string, unknown>
    );
    const action = requireString(result, "action");
    if (!isHostActionName(action)) {
      throw new ProtocolClientError("invalid_response", "Alysis Code bridge returned an invalid host action acknowledgement.");
    }
    const outcome = requireString(result, "outcome");
    if (outcome !== "result" && outcome !== "error") {
      throw new ProtocolClientError("invalid_response", "Alysis Code bridge returned an invalid host action outcome.");
    }
    const status = requireString(result, "status");
    if (status !== "applied") {
      throw new ProtocolClientError("invalid_response", "Alysis Code bridge did not apply the host action response.");
    }
    const parsed: HostActionRespondResult = {
      status,
      session_id: requireString(result, "session_id"),
      host_action_id: requireString(result, "host_action_id"),
      action,
      outcome
    };
    if (
      parsed.session_id !== params.session_id
      || parsed.host_action_id !== params.host_action_id
      || parsed.outcome !== (params.ok ? "result" : "error")
    ) {
      throw new ProtocolClientError(
        "invalid_response",
        "Alysis Code bridge returned a mismatched host action acknowledgement."
      );
    }
    return parsed;
  }

  private withHostCapabilities(
    params: SessionCreateParams | RunStartParams | Record<string, unknown>
  ): Record<string, unknown> {
    const base = { ...(params as unknown as Record<string, unknown>) };
    const advertised = this.hostCapabilityProvider?.();
    if (!advertised) {
      return base;
    }
    return {
      ...base,
      workspace_trusted: advertised.workspace_trusted,
      host_capabilities: {
        protocol_version: advertised.host_capabilities.protocol_version,
        actions: [...advertised.host_capabilities.actions]
      }
    };
  }

  public async sessionStatus(sessionId: string): Promise<SessionStatusResult> {
    return sessionStatusFromRecord(await this.request("session.status", { session_id: sessionId }));
  }

  public async sessionUsage(sessionId: string): Promise<SessionUsageResult> {
    const result = await this.request("session.usage", { session_id: sessionId });
    return {
      session_id: requireString(result, "session_id"),
      by_model: Array.isArray(result.by_model) ? result.by_model.filter(isRecord) : [],
      totals: isRecord(result.totals) ? result.totals : {},
      call_count: typeof result.call_count === "number" ? result.call_count : 0
    };
  }

  public async sessionHistory(
    sessionId: string,
    pattern: string,
    options: { max_results?: number; max_file_bytes?: number } = {}
  ): Promise<SessionHistoryResult> {
    const result = await this.request("session.history", {
      session_id: sessionId,
      pattern,
      ...options
    });
    return {
      session_id: requireString(result, "session_id"),
      pattern: requireString(result, "pattern"),
      matches: Array.isArray(result.matches)
        ? result.matches.filter(isRecord).map((match) => ({
            kind: requireString(match, "kind"),
            path: requireString(match, "path"),
            line: typeof match.line === "number" ? match.line : 0,
            text: requireString(match, "text")
          }))
        : [],
      truncated: typeof result.truncated === "boolean" ? result.truncated : false
    };
  }

  public async sessionSearch(
    sessionId: string,
    query: string,
    options: { max_results?: number; max_sessions?: number } = {}
  ): Promise<SessionSearchResult> {
    const result = await this.request("session.search", {
      session_id: sessionId,
      query,
      ...options
    });
    return {
      session_id: requireString(result, "session_id"),
      workspace_root: requireString(result, "workspace_root"),
      query: requireString(result, "query"),
      results: Array.isArray(result.results)
        ? result.results.filter(isRecord).map((item) => ({
            result_id: requireString(item, "result_id"),
            session_id: requireString(item, "session_id"),
            event_type: requireString(item, "event_type"),
            timestamp: nullableString(item.timestamp),
            snippet: requireString(item, "snippet"),
            snippet_truncated: item.snippet_truncated === true,
            context_block: isRecord(item.context_block) ? item.context_block : {}
          }))
        : [],
      scanned_sessions: typeof result.scanned_sessions === "number" ? result.scanned_sessions : 0,
      scanned_events: typeof result.scanned_events === "number" ? result.scanned_events : 0,
      scanned_bytes: typeof result.scanned_bytes === "number" ? result.scanned_bytes : 0,
      truncated: result.truncated === true,
      redacted: true,
      secret_values_included: false
    };
  }

  public async sessionContext(sessionId: string): Promise<SessionContextResult> {
    const result = await this.request("session.context", { session_id: sessionId });
    return {
      ...result,
      session_id: requireString(result, "session_id"),
      model_name: requireString(result, "model_name"),
      max_input_tokens: nullableNumber(result.max_input_tokens),
      used_input_tokens: nullableNumber(result.used_input_tokens),
      remaining_tokens: nullableNumber(result.remaining_tokens),
      percent_left: nullableNumber(result.percent_left),
      source: requireString(result, "source"),
      message_count: typeof result.message_count === "number" ? result.message_count : 0,
      pinned_prefix_len: typeof result.pinned_prefix_len === "number" ? result.pinned_prefix_len : 0
    };
  }

  public async sessionCompact(sessionId: string, focus?: string): Promise<SessionCompactResult> {
    const params: Record<string, unknown> = { session_id: sessionId };
    if (focus) {
      params.focus = focus;
    }
    const result = await this.request("session.compact", params);
    return {
      session_id: requireString(result, "session_id"),
      supported: typeof result.supported === "boolean" ? result.supported : undefined,
      changed: typeof result.changed === "boolean" ? result.changed : false,
      focus: nullableString(result.focus),
      tokens_before: typeof result.tokens_before === "number" ? result.tokens_before : 0,
      tokens_after: typeof result.tokens_after === "number" ? result.tokens_after : 0,
      tokens_delta: typeof result.tokens_delta === "number" ? result.tokens_delta : 0,
      message_count: typeof result.message_count === "number" ? result.message_count : 0,
      source: nullableString(result.source) ?? undefined,
      approximate: typeof result.approximate === "boolean" ? result.approximate : undefined,
      reason: nullableString(result.reason) ?? undefined,
      chunks_before: nullableNumber(result.chunks_before) ?? undefined,
      chunks_after: nullableNumber(result.chunks_after) ?? undefined,
      pins_before: nullableNumber(result.pins_before) ?? undefined,
      pins_after: nullableNumber(result.pins_after) ?? undefined
    };
  }

  public async sessionFork(sessionId: string, sourceSessionId: string): Promise<void> {
    const result = await this.request("session.fork", { session_id: sessionId, source_session_id: sourceSessionId });
    if (requireString(result, "session_id") !== sessionId || requireString(result, "source_session_id") !== sourceSessionId) {
      throw new Error("Worktree conversation response did not match the requested sessions.");
    }
  }

  public async sessionResume(sessionId: string, targetSessionId: string, options?: { emitHistory?: boolean }): Promise<SessionResumeResult> {
    const result = await this.request("session.resume", {
      session_id: sessionId,
      target_session_id: targetSessionId,
      ...(options?.emitHistory ? { emit_history: true } : {})
    });
    return {
      session_id: requireString(result, "session_id"),
      resumed_session_id: requireString(result, "resumed_session_id"),
      resumed: typeof result.resumed === "boolean" ? result.resumed : false,
      message: requireString(result, "message"),
      history_count: typeof result.history_count === "number" ? result.history_count : 0,
      history_count_total: nullableNumber(result.history_count_total) ?? undefined,
      bounded: typeof result.bounded === "boolean" ? result.bounded : undefined,
      max_messages: nullableNumber(result.max_messages) ?? undefined,
      source: nullableString(result.source) ?? undefined,
      model_context_replay_supported:
        typeof result.model_context_replay_supported === "boolean"
          ? result.model_context_replay_supported
          : undefined,
      resume_context_loaded:
        typeof result.resume_context_loaded === "boolean"
          ? result.resume_context_loaded
          : undefined,
      messages_before: nullableNumber(result.messages_before) ?? undefined,
      messages_after: nullableNumber(result.messages_after) ?? undefined,
      queued_prompts_rebound: nullableNumber(result.queued_prompts_rebound) ?? undefined,
      expired_prompts_recovered: nullableNumber(result.expired_prompts_recovered) ?? undefined,
      active_prompts_observed: nullableNumber(result.active_prompts_observed) ?? undefined
    };
  }

  public async sessionImagesList(sessionId: string): Promise<SessionImagesListResult> {
    return sessionImagesListFromRecord(
      await this.request("session.images.list", { session_id: sessionId })
    );
  }

  public async sessionImagesAdd(
    params: SessionImagesAddParams
  ): Promise<SessionImagesAddResult> {
    const result = await this.request("session.images.add", params as unknown as Record<string, unknown>);
    return {
      ...sessionImagesListFromRecord(result),
      added_count: typeof result.added_count === "number" ? result.added_count : 0,
      replaced: typeof result.replaced === "boolean" ? result.replaced : false
    };
  }

  public async sessionImagesClear(sessionId: string): Promise<SessionImagesClearResult> {
    const result = await this.request("session.images.clear", { session_id: sessionId });
    return {
      session_id: requireString(result, "session_id"),
      cleared: typeof result.cleared === "boolean" ? result.cleared : false,
      count_before: typeof result.count_before === "number" ? result.count_before : 0,
      count_after: typeof result.count_after === "number" ? result.count_after : 0,
      images: sessionImagesFromRecord(result)
    };
  }

  public async sessionSetMode(sessionId: string, mode: "readonly" | "review" | "auto"): Promise<SessionStatusResult> {
    return sessionStatusFromRecord(
      await this.request("session.setMode", { session_id: sessionId, mode })
    );
  }

  public async sessionSetModel(
    sessionId: string,
    model: string,
    options: { base_url?: string } = {}
  ): Promise<SessionStatusResult> {
    return sessionStatusFromRecord(
      await this.request("session.setModel", { session_id: sessionId, model, ...options })
    );
  }

  public async sessionSetProfile(sessionId: string, name: string, model?: string): Promise<SessionStatusResult> {
    return sessionStatusFromRecord(await this.request("session.setProfile", {
      session_id: sessionId, name, workspace_trusted: true, ...(model ? { model } : {})
    }));
  }

  public async sessionSetStream(sessionId: string, stream: boolean): Promise<SessionStatusResult> {
    return sessionStatusFromRecord(
      await this.request("session.setStream", { session_id: sessionId, stream })
    );
  }

  public async sessionSetActiveWorkdir(
    sessionId: string,
    path: string
  ): Promise<SessionSetActiveWorkdirResult> {
    const result = await this.request("session.setActiveWorkdir", {
      session_id: sessionId,
      path
    });
    return {
      ...result,
      session_id: requireString(result, "session_id"),
      source: requireString(result, "source"),
      workspace_root: requireString(result, "workspace_root"),
      previous_active_workdir: requireString(result, "previous_active_workdir"),
      previous_active_workdir_relpath: requireString(result, "previous_active_workdir_relpath"),
      active_workdir: requireString(result, "active_workdir"),
      active_workdir_relpath: requireString(result, "active_workdir_relpath"),
      changed: typeof result.changed === "boolean" ? result.changed : false
    };
  }

  public async sessionModelInfo(
    sessionId: string,
    options: { model?: string } = {}
  ): Promise<SessionModelInfoResult> {
    const result = await this.request("session.modelInfo", { session_id: sessionId, ...options });
    return {
      session_id: requireString(result, "session_id"),
      model: requireString(result, "model"),
      provider: typeof result.provider === "string" ? result.provider : "",
      profile: nullableString(result.profile),
      base_url: nullableString(result.base_url),
      base_url_redacted: typeof result.base_url_redacted === "boolean" ? result.base_url_redacted : false,
      context_window: nullableNumber(result.context_window),
      vision_support: nullableBoolean(result.vision_support),
      tool_support: nullableBoolean(result.tool_support),
      streaming_support: nullableBoolean(result.streaming_support),
      source: typeof result.source === "string" ? result.source : "unknown",
      source_metadata: isRecord(result.source_metadata) ? result.source_metadata : {},
      secret_values_included: typeof result.secret_values_included === "boolean" ? result.secret_values_included : false
    };
  }

  public async sessionPersonasList(
    params: SessionPersonasListParams
  ): Promise<SessionPersonasListResult> {
    return parseSessionPersonasListResult(
      await this.request("session.personas.list", { session_id: params.session_id })
    );
  }

  public async sessionPersonaSet(
    params: SessionPersonaSetParams
  ): Promise<SessionPersonaSetResult> {
    return parseSessionPersonaSetResult(
      await this.request("session.persona.set", {
        session_id: params.session_id,
        persona: params.persona
      })
    );
  }

  public async sessionSubagentsStatus(sessionId: string): Promise<SessionSubagentsStatusResult> {
    return sessionSubagentsStatusFromRecord(
      await this.request("session.subagents.status", { session_id: sessionId })
    );
  }

  public async sessionSubagentsSetEnabled(
    sessionId: string,
    enabled: boolean,
    workspaceTrusted: boolean
  ): Promise<SessionSubagentsStatusResult> {
    return sessionSubagentsStatusFromRecord(
      await this.request("session.subagents.setEnabled", {
        session_id: sessionId,
        enabled,
        workspace_trusted: workspaceTrusted
      })
    );
  }

  public async sessionTraceStatus(sessionId: string): Promise<SessionTraceStatusResult> {
    return sessionTraceStatusFromRecord(
      await this.request("session.trace.status", { session_id: sessionId })
    );
  }

  public async sessionTraceSetLevel(
    sessionId: string,
    level: SessionTraceLevel,
    options: { confirm?: boolean; yes?: boolean } = {}
  ): Promise<SessionTraceStatusResult> {
    return sessionTraceStatusFromRecord(
      await this.request("session.trace.setLevel", {
        session_id: sessionId,
        level,
        ...options
      })
    );
  }

  public async sessionTraceListEvents(
    sessionId: string,
    options: { after_sequence?: number; max_events?: number; max_bytes?: number } = {}
  ): Promise<SessionTraceEventsResult> {
    const result = await this.request("session.trace.listEvents", {
      session_id: sessionId,
      ...options
    });
    return {
      session_id: requireString(result, "session_id"),
      level: traceLevelFromValue(result.level),
      events: Array.isArray(result.events) ? result.events.filter(isRecord) as unknown as ProtocolEventEnvelope[] : [],
      count: typeof result.count === "number" ? result.count : 0,
      total_retained: typeof result.total_retained === "number" ? result.total_retained : 0,
      bytes: typeof result.bytes === "number" ? result.bytes : 0,
      redacted: result.redacted === true,
      secret_values_included: result.secret_values_included === true,
      max_events: typeof result.max_events === "number" ? result.max_events : 0,
      max_bytes: typeof result.max_bytes === "number" ? result.max_bytes : 0,
      truncated: result.truncated === true,
      truncated_by_event_count: result.truncated_by_event_count === true,
      truncated_by_bytes: result.truncated_by_bytes === true,
      lowest_retained_sequence: nullableNumber(result.lowest_retained_sequence),
      highest_retained_sequence: nullableNumber(result.highest_retained_sequence)
    };
  }

  public async sessionTraceReadArtifact(
    sessionId: string,
    artifactId: string,
    options: { max_bytes?: number } = {}
  ): Promise<SessionTraceArtifactResult> {
    const result = await this.request("session.trace.readArtifact", {
      session_id: sessionId,
      artifact_id: artifactId,
      ...options
    });
    return {
      artifact_id: requireString(result, "artifact_id"),
      path: requireString(result, "path"),
      size_bytes: typeof result.size_bytes === "number" ? result.size_bytes : 0,
      truncated: result.truncated === true,
      max_bytes: typeof result.max_bytes === "number" ? result.max_bytes : 0,
      encoding: requireString(result, "encoding"),
      content: requireString(result, "content"),
      session_id: requireString(result, "session_id"),
      redacted: result.redacted === true,
      secret_values_included: result.secret_values_included === true
    };
  }

  public async sessionTraceClear(sessionId: string): Promise<SessionTraceClearResult> {
    const result = await this.request("session.trace.clear", { session_id: sessionId });
    return {
      session_id: requireString(result, "session_id"),
      cleared: result.cleared === true,
      events_before: typeof result.events_before === "number" ? result.events_before : 0,
      events_after: typeof result.events_after === "number" ? result.events_after : 0,
      redacted: result.redacted === true,
      secret_values_included: result.secret_values_included === true
    };
  }

  public async sessionTerminalsList(sessionId: string): Promise<SessionTerminalsListResult> {
    return sessionTerminalsListFromRecord(
      await this.request("session.terminals.list", { session_id: sessionId })
    );
  }

  public async sessionTerminalsShow(
    sessionId: string,
    processId: string,
    options: { since?: number; max_lines?: number; max_bytes?: number } = {}
  ): Promise<SessionTerminalShowResult> {
    return sessionTerminalShowFromRecord(
      await this.request("session.terminals.show", {
        session_id: sessionId,
        process_id: processId,
        ...options
      })
    );
  }

  public async sessionTerminalsKill(
    sessionId: string,
    processId: string,
    workspaceTrusted: boolean,
    options: { confirm?: boolean; yes?: boolean } = {}
  ): Promise<SessionTerminalShowResult> {
    return sessionTerminalShowFromRecord(
      await this.request("session.terminals.kill", {
        session_id: sessionId,
        process_id: processId,
        workspace_trusted: workspaceTrusted,
        ...options
      })
    );
  }

  public async sessionTerminalsClear(
    sessionId: string,
    processId: string,
    workspaceTrusted: boolean,
    options: { confirm?: boolean; yes?: boolean } = {}
  ): Promise<SessionTerminalShowResult> {
    return sessionTerminalShowFromRecord(
      await this.request("session.terminals.clear", {
        session_id: sessionId,
        process_id: processId,
        workspace_trusted: workspaceTrusted,
        ...options
      })
    );
  }

  public async sessionClear(sessionId: string): Promise<SessionClearResult> {
    const result = await this.request("session.clear", { session_id: sessionId });
    return {
      session_id: requireString(result, "session_id"),
      cleared: typeof result.cleared === "boolean" ? result.cleared : false,
      messages_before: typeof result.messages_before === "number" ? result.messages_before : 0,
      messages_after: typeof result.messages_after === "number" ? result.messages_after : 0
    };
  }

  public async cancelSession(
    sessionId: string,
    options: { reason?: string; closeWhenIdle?: boolean } = {}
  ): Promise<Record<string, unknown>> {
    return this.request("session.cancel", {
      session_id: sessionId,
      ...(options.reason ? { reason: options.reason } : {}),
      ...(options.closeWhenIdle ? { close_when_idle: true } : {})
    });
  }

  public async respondApproval(params: {
    session_id: string;
    approval_id: string;
    allow: boolean;
    allow_for_session: boolean;
  }): Promise<ApprovalRespondResult> {
    try {
      const result = await this.request("approval.respond", params);
      return {
        session_id: requireString(result, "session_id"),
        approval_id: requireString(result, "approval_id"),
        status: requireString(result, "status"),
        allow: typeof result.allow === "boolean" ? result.allow : false,
        allow_for_session:
          typeof result.allow_for_session === "boolean" ? result.allow_for_session : false,
        allow_for_session_supported:
          typeof result.allow_for_session_supported === "boolean"
            ? result.allow_for_session_supported
            : false,
        allow_for_session_scope: isRecord(result.allow_for_session_scope)
          ? result.allow_for_session_scope
          : null,
        allow_for_session_warning:
          typeof result.allow_for_session_warning === "string"
            ? result.allow_for_session_warning
            : null
      };
    } finally {
      this.pendingApprovalIds.delete(approvalKey(params.session_id, params.approval_id));
    }
  }

  public async jobStatus(jobId: string): Promise<JobStatusResult> {
    const result = await this.request("job.status", { job_id: jobId });
    const status = jobStatusFromRecord(result);
    if (FINAL_JOB_STATUSES.has(status.status)) {
      this.activeJobIds.delete(status.job_id);
    }
    return status;
  }

  public async sessionList(): Promise<SessionListResult> {
    const result = await this.request("session.list");
    const sessions = Array.isArray(result.sessions)
      ? result.sessions.filter(isRecord).map(sessionSummaryFromRecord)
      : [];
    return { sessions };
  }

  public async getEvents(
    sessionId: string,
    afterSequence?: number,
    maxEvents = 500
  ): Promise<EventReplayResult> {
    const params: Record<string, unknown> = {
      session_id: sessionId,
      max_events: maxEvents
    };
    if (afterSequence !== undefined) {
      params.after_sequence = afterSequence;
    }
    const result = await this.request("session.getEvents", params);
    const events = Array.isArray(result.events)
      ? result.events.filter(isProtocolEvent)
      : [];
    for (const event of events) {
      this.noteEventSequence(event);
    }
    return {
      session_id: requireString(result, "session_id"),
      events,
      truncated: typeof result.truncated === "boolean" ? result.truncated : false,
      lowest_retained_sequence: nullableNumber(result.lowest_retained_sequence),
      highest_retained_sequence: nullableNumber(result.highest_retained_sequence),
      max_events: typeof result.max_events === "number" ? result.max_events : maxEvents
    };
  }

  public async artifactList(sessionId: string): Promise<ArtifactListResult> {
    const result = await this.request("artifact.list", { session_id: sessionId });
    return {
      session_id: requireString(result, "session_id"),
      artifacts: Array.isArray(result.artifacts)
        ? result.artifacts.filter(isRecord).map((artifact) => ({
            artifact_id: requireString(artifact, "artifact_id"),
            root: requireString(artifact, "root"),
            path: requireString(artifact, "path"),
            size_bytes: typeof artifact.size_bytes === "number" ? artifact.size_bytes : 0
          }))
        : [],
      truncated: typeof result.truncated === "boolean" ? result.truncated : false,
      max_items: typeof result.max_items === "number" ? result.max_items : 0,
      max_depth: typeof result.max_depth === "number" ? result.max_depth : 0
    };
  }

  public async artifactRead(sessionId: string, artifactId: string): Promise<ArtifactReadResult> {
    const result = await this.request("artifact.read", {
      session_id: sessionId,
      artifact_id: artifactId
    });
    return {
      session_id: requireString(result, "session_id"),
      artifact_id: requireString(result, "artifact_id"),
      path: requireString(result, "path"),
      size_bytes: typeof result.size_bytes === "number" ? result.size_bytes : 0,
      truncated: typeof result.truncated === "boolean" ? result.truncated : false,
      max_bytes: typeof result.max_bytes === "number" ? result.max_bytes : 0,
      encoding: requireString(result, "encoding"),
      content: requireString(result, "content")
    };
  }

  public async browserStart(params: ManagedBrowserStartParams): Promise<ManagedBrowserStatusResult> {
    return managedBrowserStatusFromRecord(
      await this.request("browser.start", params as unknown as Record<string, unknown>)
    );
  }

  public async browserNavigate(
    params: ManagedBrowserNavigateParams
  ): Promise<ManagedBrowserNavigateResult> {
    const result = await this.request(
      "browser.navigate",
      params as unknown as Record<string, unknown>
    );
    return {
      ...managedBrowserScopeFromRecord(result),
      url: requireBrowserString(result, "url"),
      result: managedBrowserBoundedPayloadFromRecord(requiredRecord(result, "result"))
    };
  }

  public async browserSnapshot(
    params: ManagedBrowserSnapshotParams
  ): Promise<ManagedBrowserSnapshotResult> {
    return managedBrowserSnapshotFromRecord(
      await this.request("browser.snapshot", params as unknown as Record<string, unknown>)
    );
  }

  public async browserScreenshot(
    params: ManagedBrowserScreenshotParams
  ): Promise<ManagedBrowserScreenshotResult> {
    const result = await this.request(
      "browser.screenshot",
      params as unknown as Record<string, unknown>
    );
    const mediaType = requireBrowserString(result, "media_type");
    if (mediaType !== "image/png") {
      throw invalidBrowserResponse("media_type");
    }
    return {
      ...managedBrowserScopeFromRecord(result),
      artifact_id: requireBrowserString(result, "artifact_id"),
      media_type: mediaType,
      size_bytes: requireBrowserNonNegativeNumber(result, "size_bytes"),
      sha256: requireBrowserString(result, "sha256")
    };
  }

  public async browserArtifactRead(
    params: ManagedBrowserArtifactReadParams
  ): Promise<ManagedBrowserArtifactReadResult> {
    const result = await this.request(
      "browser.artifact.read",
      params as unknown as Record<string, unknown>
    );
    const mediaType = requireBrowserString(result, "media_type");
    const encoding = requireBrowserString(result, "encoding");
    if (mediaType !== "image/png") {
      throw invalidBrowserResponse("media_type");
    }
    if (encoding !== "base64") {
      throw invalidBrowserResponse("encoding");
    }
    return {
      ...managedBrowserScopeFromRecord(result),
      artifact_id: requireBrowserString(result, "artifact_id"),
      media_type: mediaType,
      encoding,
      content: requireBrowserString(result, "content", true),
      offset: requireBrowserNonNegativeNumber(result, "offset"),
      next_offset: requireBrowserNonNegativeNumber(result, "next_offset"),
      size_bytes: requireBrowserNonNegativeNumber(result, "size_bytes"),
      truncated: requireBrowserBoolean(result, "truncated")
    };
  }

  public async browserDiagnostics(
    params: ManagedBrowserDiagnosticsParams
  ): Promise<ManagedBrowserDiagnosticsResult> {
    const result = await this.request(
      "browser.diagnostics",
      params as unknown as Record<string, unknown>
    );
    const events = Array.isArray(result.events)
      ? result.events.map((event) =>
          managedBrowserDiagnosticEventFromRecord(requireBrowserRecord(event, "events"))
        )
      : (() => {
          throw invalidBrowserResponse("events");
        })();
    return {
      ...managedBrowserScopeFromRecord(result),
      events,
      truncated: requireBrowserBoolean(result, "truncated"),
      max_events: requireBrowserNonNegativeNumber(result, "max_events")
    };
  }

  public async browserClick(params: ManagedBrowserClickParams): Promise<ManagedBrowserClickResult> {
    const result = await this.request(
      "browser.click",
      params as unknown as Record<string, unknown>
    );
    return {
      ...managedBrowserScopeFromRecord(result),
      clicked: requireBrowserBoolean(result, "clicked")
    };
  }

  public async browserType(params: ManagedBrowserTypeParams): Promise<ManagedBrowserTypeResult> {
    const result = await this.request(
      "browser.type",
      params as unknown as Record<string, unknown>
    );
    return {
      ...managedBrowserScopeFromRecord(result),
      typed: requireBrowserBoolean(result, "typed"),
      character_count: requireBrowserNonNegativeNumber(result, "character_count")
    };
  }

  public async browserStatus(
    params: ManagedBrowserSessionParams
  ): Promise<ManagedBrowserStatusResult> {
    return managedBrowserStatusFromRecord(
      await this.request("browser.status", params as unknown as Record<string, unknown>)
    );
  }

  public async browserList(sessionId: string): Promise<ManagedBrowserListResult> {
    const result = await this.request("browser.list", { session_id: sessionId });
    if (!Array.isArray(result.browsers)) {
      throw invalidBrowserResponse("browsers");
    }
    const browsers = result.browsers.map((browser) =>
      managedBrowserSessionStatusFromRecord(requireBrowserRecord(browser, "browsers"))
    );
    const count = requireBrowserNonNegativeNumber(result, "count");
    if (count !== browsers.length) {
      throw invalidBrowserResponse("count");
    }
    return {
      session_id: requireBrowserString(result, "session_id"),
      browsers,
      count
    };
  }

  public async browserClose(params: ManagedBrowserCloseParams): Promise<ManagedBrowserCloseResult> {
    const result = await this.request(
      "browser.close",
      params as unknown as Record<string, unknown>
    );
    const status = requireBrowserString(result, "status");
    if (status !== "closed" && status !== "not_found") {
      throw invalidBrowserResponse("status");
    }
    return {
      ...managedBrowserScopeFromRecord(result),
      status
    };
  }

  public async forgePlan(params: ForgePlanParams): Promise<ForgePlanResult> {
    const result = await this.request("forge.plan", params as unknown as Record<string, unknown>);
    return forgePlanFromRecord(result);
  }

  public async forgePlanStart(params: ForgePlanParams): Promise<ForgePlanStartResult> {
    const result = await this.request(
      "forge.plan.start",
      params as unknown as Record<string, unknown>
    );
    const started = this.trackStartedJob(result);
    return {
      ...started,
      ...(typeof result.durably_accepted === "boolean"
        ? { durably_accepted: result.durably_accepted }
        : {}),
      ...(typeof result.duplicate === "boolean" ? { duplicate: result.duplicate } : {})
    };
  }

  public async forgePlanResult(jobId: string): Promise<ForgePlanResult> {
    const result = await this.request("forge.plan.result", { job_id: jobId });
    return forgePlanFromRecord(result);
  }

  public async forgeList(params: ForgeListParams): Promise<ForgeListResult> {
    const result = await this.request("forge.list", params as unknown as Record<string, unknown>);
    return {
      workspace_root: requireString(result, "workspace_root"),
      plans: Array.isArray(result.plans) ? result.plans.filter(isRecord).map(forgePlanSummaryFromRecord) : [],
      truncated: typeof result.truncated === "boolean" ? result.truncated : false,
      max_items: typeof result.max_items === "number" ? result.max_items : 0
    };
  }

  public async forgeOpen(params: ForgeOpenParams): Promise<ForgePlanResult> {
    const result = await this.request("forge.open", params as unknown as Record<string, unknown>);
    return forgePlanFromRecord(result);
  }

  public async forgeResume(params: ForgeOpenParams): Promise<ForgePlanResult> {
    const result = await this.request("forge.resume", params as unknown as Record<string, unknown>);
    return forgePlanFromRecord(result);
  }

  public async forgeStatus(sessionId: string, planId: string): Promise<ForgePlanResult> {
    const result = await this.request("forge.status", {
      session_id: sessionId,
      plan_id: planId
    });
    return forgePlanFromRecord(result);
  }

  public async forgeShow(params: ForgeScopedPlanParams): Promise<ForgeShowResult> {
    const result = await this.request("forge.show", params as unknown as Record<string, unknown>);
    return forgeShowFromRecord(result);
  }

  public async forgePlanGetState(params: ForgeScopedPlanParams): Promise<ForgePlanStateResult> {
    const result = await this.request("forge.plan.getState", params as unknown as Record<string, unknown>);
    return forgePlanStateFromRecord(result);
  }

  public async forgePlanSetAssistant(params: ForgePlanSetAssistantParams): Promise<ForgePlanStateResult> {
    const result = await this.request("forge.plan.setAssistant", params as unknown as Record<string, unknown>);
    return forgePlanStateFromRecord(result);
  }

  public async forgePlanSetGoal(params: ForgePlanSetGoalParams): Promise<ForgePlanStateResult> {
    const result = await this.request("forge.plan.setGoal", params as unknown as Record<string, unknown>);
    return forgePlanStateFromRecord(result);
  }

  public async forgePlanUpdateTask(params: ForgePlanUpdateTaskParams): Promise<ForgePlanStateResult> {
    const result = await this.request("forge.plan.updateTask", params as unknown as Record<string, unknown>);
    return forgePlanStateFromRecord(result);
  }

  public async forgePlanValidate(params: ForgeScopedPlanParams): Promise<Record<string, unknown>> {
    return this.request("forge.plan.validate", params as unknown as Record<string, unknown>);
  }

  public async forgePlanRegenerate(params: ForgePlanRegenerateParams): Promise<ForgePlanRegenerateResult> {
    const result = await this.request("forge.plan.regenerate", params as unknown as Record<string, unknown>);
    return forgePlanRegenerateFromRecord(result);
  }

  public async forgePlanRegenerateStart(
    params: ForgePlanRegenerateParams
  ): Promise<ForgePlanRegenerateStartResult> {
    const result = await this.request(
      "forge.plan.regenerate.start",
      params as unknown as Record<string, unknown>
    );
    const started = this.trackStartedJob(result);
    return { ...started, plan_id: requireString(result, "plan_id") };
  }

  public async forgePlanRegenerateResult(jobId: string): Promise<ForgePlanRegenerateJobResult> {
    const result = await this.request("forge.plan.regenerate.result", { job_id: jobId });
    if (typeof result.complete === "boolean") {
      return forgeJobProgressFromRecord(result);
    }
    return forgePlanRegenerateFromRecord(result);
  }

  public async forgeReview(params: ForgeReviewParams): Promise<ForgeReviewResult> {
    const result = await this.request("forge.review", params as unknown as Record<string, unknown>);
    return forgeReviewFromRecord(result);
  }

  public async forgeReviewStart(params: ForgeReviewParams): Promise<ForgeReviewStartResult> {
    const result = await this.request(
      "forge.review.start",
      params as unknown as Record<string, unknown>
    );
    const started = this.trackStartedJob(result);
    return {
      ...started,
      plan_id: requireString(result, "plan_id"),
      task_id: requireString(result, "task_id")
    };
  }

  public async forgeReviewResult(jobId: string): Promise<ForgeReviewResult | ForgeJobProgress> {
    const result = await this.request("forge.review.result", { job_id: jobId });
    if (typeof result.complete === "boolean") {
      return forgeJobProgressFromRecord(result);
    }
    return forgeReviewFromRecord(result);
  }

  public async forgeSwarmStart(params: ForgeSwarmStartParams): Promise<ForgeSwarmStartResult> {
    const result = await this.request(
      "forge.swarm.start",
      params as unknown as Record<string, unknown>
    );
    const started = this.trackStartedJob(result);
    return {
      ...started,
      plan_id: requireString(result, "plan_id"),
      parallel: typeof result.parallel === "number" ? result.parallel : 1
    };
  }

  public async forgeSwarmResume(params: ForgeSwarmResumeParams): Promise<ForgeSwarmResumeResult> {
    const result = await this.request(
      "forge.swarm.resume",
      params as unknown as Record<string, unknown>
    );
    const durable = forgeSwarmDurableStatusFromRecord(result);
    const status = requireString(result, "status");
    if (status !== "resumed") {
      throw new ProtocolClientError(
        "invalid_response",
        "Bridge returned an invalid Forge swarm resume status."
      );
    }
    if (durable.state === "queued" || durable.state === "running") {
      this.activeJobIds.add(durable.job_id);
    }
    return {
      ...durable,
      session_id: requireString(result, "session_id"),
      plan_id: requireString(result, "plan_id"),
      status
    };
  }

  public async forgeSwarmList(params: ForgeSwarmListParams): Promise<ForgeSwarmListResult> {
    const result = await this.request(
      "forge.swarm.list",
      params as unknown as Record<string, unknown>
    );
    return {
      session_id: requireString(result, "session_id"),
      jobs: Array.isArray(result.jobs)
        ? result.jobs.filter(isRecord).map(forgeSwarmDurableStatusFromRecord)
        : [],
      count: requireNumber(result, "count")
    };
  }

  public async forgeSwarmStatus(sessionId: string, jobId: string): Promise<ForgeSwarmStatusResult> {
    const result = await this.request("forge.swarm.status", {
      session_id: sessionId,
      job_id: jobId
    });
    return {
      job_id: requireString(result, "job_id"),
      session_id: requireString(result, "session_id"),
      status: requireString(result, "status"),
      state: requireString(result, "state"),
      plan_id: typeof result.plan_id === "string" ? result.plan_id : null,
      cancellation_requested: result.cancellation_requested === true,
      task_status_counts: isRecord(result.task_status_counts)
        ? (result.task_status_counts as Record<string, number>)
        : undefined
    };
  }

  public async forgeSwarmResult(jobId: string, sessionId?: string): Promise<ForgeSwarmJobResult> {
    const result = await this.request("forge.swarm.result", {
      job_id: jobId,
      ...(sessionId ? { session_id: sessionId } : {})
    });
    if (result.complete === false) {
      return forgeJobProgressFromRecord(result);
    }
    this.activeJobIds.delete(jobId);
    return result as unknown as ForgeSwarmRunResult;
  }

  public async forgeSwarmCancel(
    sessionId: string,
    jobId: string,
    reason?: string
  ): Promise<Record<string, unknown>> {
    return this.request("forge.swarm.cancel", {
      session_id: sessionId,
      job_id: jobId,
      ...(reason ? { reason } : {})
    });
  }

  public async forgeSwarmReconcile(
    params: ForgeSwarmReconcileParams
  ): Promise<ForgeSwarmReconcileResult> {
    const result = await this.request(
      "forge.swarm.reconcile",
      params as unknown as Record<string, unknown>
    );
    return result as unknown as ForgeSwarmReconcileResult;
  }

  public async forgeSwarmReview(sessionId: string, planId: string): Promise<ForgeSwarmReviewResult> {
    const result = await this.request("forge.swarm.review", {
      session_id: sessionId,
      plan_id: planId
    });
    return result as unknown as ForgeSwarmReviewResult;
  }

  public async forgeSwarmApply(
    sessionId: string,
    planId: string,
    taskIds: string[],
    options?: { base_branch?: string }
  ): Promise<ForgeSwarmApplyResult> {
    const result = await this.request("forge.swarm.apply", {
      session_id: sessionId,
      plan_id: planId,
      task_ids: taskIds,
      workspace_trusted: true,
      ...(options?.base_branch ? { base_branch: options.base_branch } : {})
    });
    return result as unknown as ForgeSwarmApplyResult;
  }

  public async forgeSwarmDiscard(
    sessionId: string,
    planId: string,
    taskIds: string[]
  ): Promise<ForgeSwarmDiscardResult> {
    const result = await this.request("forge.swarm.discard", {
      session_id: sessionId,
      plan_id: planId,
      task_ids: taskIds,
      workspace_trusted: true,
      yes: true
    });
    return result as unknown as ForgeSwarmDiscardResult;
  }

  public async forgeAttach(params: ForgeAttachParams): Promise<ForgeAssetsMutationResult> {
    const result = await this.request("forge.attach", params as unknown as Record<string, unknown>);
    return forgeAssetMutationFromRecord(result);
  }

  public async forgeAssetsList(params: ForgeAssetsListParams): Promise<ForgeAssetsListResult> {
    const result = await this.request("forge.assets.list", params as unknown as Record<string, unknown>);
    return {
      session_id: requireString(result, "session_id"),
      plan_id: requireString(result, "plan_id"),
      run_id: requireString(result, "run_id"),
      assets: Array.isArray(result.assets) ? result.assets.filter(isRecord).map(forgeAssetEntryFromRecord) : [],
      count: typeof result.count === "number" ? result.count : 0,
      include_deleted: typeof result.include_deleted === "boolean" ? result.include_deleted : false
    };
  }

  public async forgeAssetsShow(params: ForgeAssetsShowParams): Promise<ForgeAssetsShowResult> {
    const result = await this.request("forge.assets.show", params as unknown as Record<string, unknown>);
    return {
      session_id: requireString(result, "session_id"),
      plan_id: requireString(result, "plan_id"),
      asset: isRecord(result.asset) ? forgeAssetDetailFromRecord(result.asset) : forgeEmptyAssetDetail()
    };
  }

  public async forgeAssetsAdd(params: ForgeAssetsAddParams): Promise<ForgeAssetsMutationResult> {
    const result = await this.request("forge.assets.add", params as unknown as Record<string, unknown>);
    return forgeAssetMutationFromRecord(result);
  }

  public async forgeAssetsDelete(params: ForgeAssetsDeleteParams): Promise<ForgeAssetsMutationResult> {
    const result = await this.request("forge.assets.delete", params as unknown as Record<string, unknown>);
    return forgeAssetMutationFromRecord(result);
  }

  public async forgeAssetsEdit(params: ForgeAssetsEditParams): Promise<ForgeAssetsMutationResult> {
    const result = await this.request("forge.assets.edit", params as unknown as Record<string, unknown>);
    return forgeAssetMutationFromRecord(result);
  }

  public async forgeAssetsRefresh(params: ForgeAssetsRefreshParams): Promise<ForgeAssetsMutationResult> {
    const result = await this.request("forge.assets.refresh", params as unknown as Record<string, unknown>);
    return forgeAssetMutationFromRecord(result);
  }

  public async forgeAssetsCancelPending(params: ForgeScopedPlanParams): Promise<ForgeAssetsCancelPendingResult> {
    const result = await this.request("forge.assets.cancelPending", params as unknown as Record<string, unknown>);
    return {
      session_id: requireString(result, "session_id"),
      plan_id: requireString(result, "plan_id"),
      cancelled_count: typeof result.cancelled_count === "number" ? result.cancelled_count : 0,
      status: requireString(result, "status"),
      message: requireString(result, "message")
    };
  }

  public async forgeAssetsCheckPlan(params: ForgeScopedPlanParams): Promise<ForgeAssetsCheckPlanResult> {
    const result = await this.request("forge.assets.checkPlan", params as unknown as Record<string, unknown>);
    return {
      session_id: requireString(result, "session_id"),
      plan_id: requireString(result, "plan_id"),
      deleted_referenced: Array.isArray(result.deleted_referenced)
        ? result.deleted_referenced.filter(isRecord).map(stringRecordFromRecord)
        : [],
      missing_referenced: Array.isArray(result.missing_referenced)
        ? result.missing_referenced.filter(isRecord).map(stringRecordFromRecord)
        : [],
      pinned_added: stringArray(result.pinned_added),
      ok: typeof result.ok === "boolean" ? result.ok : false
    };
  }

  public async forgeAssetsPruneLegacy(params: ForgeAssetsPruneLegacyParams): Promise<ForgeAssetsPruneLegacyResult> {
    const result = await this.request("forge.assets.pruneLegacy", params as unknown as Record<string, unknown>);
    return {
      session_id: requireString(result, "session_id"),
      plan_id: requireString(result, "plan_id"),
      verified: stringArray(result.verified),
      unverified: stringArray(result.unverified),
      deleted: stringArray(result.deleted),
      requires_confirmation: typeof result.requires_confirmation === "boolean" ? result.requires_confirmation : false,
      blocked: typeof result.blocked === "boolean" ? result.blocked : false
    };
  }

  public async forgeExecute(params: ForgeExecuteParams): Promise<ForgeExecuteResult> {
    const result = await this.request("forge.execute", params as unknown as Record<string, unknown>);
    const parsed = {
      session_id: requireString(result, "session_id"),
      plan_id: requireString(result, "plan_id"),
      job_id: requireString(result, "job_id") || null,
      status: requireString(result, "status")
    };
    if (parsed.job_id && (parsed.status === "started" || parsed.status === "running")) {
      this.activeJobIds.add(parsed.job_id);
    }
    return parsed;
  }

  public async forgeExecutePreview(params: ForgeExecutePreviewParams): Promise<ForgeExecutePreviewResult> {
    const result = await this.request("forge.executePreview", params as unknown as Record<string, unknown>);
    return forgeExecutePreviewFromRecord(result);
  }

  public async forgeCancel(params: { session_id: string; plan_id: string }): Promise<Record<string, unknown>> {
    return this.request("forge.cancel", params);
  }

  public async diffList(sessionId: string, planId?: string): Promise<DiffListResult> {
    const params: Record<string, unknown> = { session_id: sessionId };
    if (planId) {
      params.plan_id = planId;
    }
    const result = await this.request("diff.list", params);
    return {
      diffs: Array.isArray(result.diffs)
        ? result.diffs.filter(isRecord).map(diffSummaryFromRecord)
        : [],
      empty_reason: typeof result.empty_reason === "string" ? result.empty_reason : null
    };
  }

  public async diffGet(
    diffId: string,
    options: { sessionId: string; planId: string; maxBytes?: number }
  ): Promise<DiffGetResult> {
    const params: Record<string, unknown> = {
      diff_id: diffId,
      session_id: options.sessionId,
      plan_id: options.planId
    };
    if (options.maxBytes !== undefined) {
      params.max_bytes = options.maxBytes;
    }
    const result = await this.request("diff.get", params);
    return {
      diff_id: requireString(result, "diff_id"),
      session_id: requireString(result, "session_id"),
      plan_id: requireString(result, "plan_id"),
      job_id: nullableString(result.job_id),
      file_path: requireString(result, "file_path"),
      old_text: nullableString(result.old_text),
      new_text: nullableString(result.new_text),
      old_artifact_id: nullableString(result.old_artifact_id),
      new_artifact_id: nullableString(result.new_artifact_id),
      unified_diff: requireString(result, "unified_diff"),
      truncated: typeof result.truncated === "boolean" ? result.truncated : false,
      size_bytes: typeof result.size_bytes === "number" ? result.size_bytes : 0,
      max_bytes: typeof result.max_bytes === "number" ? result.max_bytes : 0,
      redaction: requireString(result, "redaction") || undefined
    };
  }

  public async configGet(): Promise<ConfigGetResult> {
    return this.managementRequest("config.get");
  }

  public async configSet(params: ConfigSetParams): Promise<ConfigSetResult> {
    return this.managementRequest("config.set", params);
  }

  public async configSchema(): Promise<ConfigSchemaResult> {
    return this.managementRequest("config.schema");
  }

  public async configValidate(params: ConfigValidateParams = {}): Promise<ConfigValidateResult> {
    return this.managementRequest("config.validate", params);
  }

  public async profileList(): Promise<ProfileListResult> {
    return this.managementRequest("profile.list");
  }

  public async profileShow(params: ProfileNameParams): Promise<ProfileShowResult> {
    return this.managementRequest("profile.show", params);
  }

  public async profileAdd(params: ProfileAddParams): Promise<ProfileMutationResult> {
    return this.managementRequest("profile.add", params);
  }

  public async profileRemove(params: ProfileRemoveParams): Promise<ProfileMutationResult> {
    return this.managementRequest("profile.remove", params);
  }

  public async profileUse(params: TrustedProfileNameParams): Promise<ProfileUseResult> {
    return this.managementRequest("profile.use", params);
  }

  public async profileRename(params: ProfileRenameParams): Promise<ProfileMutationResult> {
    return this.managementRequest("profile.rename", params);
  }

  public async profilePresets(): Promise<ProfilePresetsResult> {
    return this.managementRequest("profile.presets");
  }

  public async profilePreset(params: ProfilePresetParams): Promise<ProfileMutationResult> {
    return this.managementRequest("profile.preset", params);
  }

  public async profileConvert(params: ProfileConvertParams): Promise<ProfileMutationResult> {
    return this.managementRequest("profile.convert", params);
  }

  public async sessionShow(params: RetainedSessionParams): Promise<SessionShowResult> {
    return this.managementRequest("session.show", params);
  }

  public async sessionScore(params: SessionScoreParams = {}): Promise<SessionScoreResult> {
    return this.managementRequest("session.score", params);
  }

  public async toolsCatalog(): Promise<ToolsCatalogResult> {
    return this.managementRequest("tools.catalog");
  }

  public async toolList(params: WorkspaceScopedParams): Promise<ToolListResult> {
    return this.managementRequest("tool.list", params);
  }

  public async toolInfo(params: ToolInfoParams): Promise<ToolInfoResult> {
    return this.managementRequest("tool.info", params);
  }

  public async toolTrust(params: ToolTrustParams): Promise<ToolTrustResult> {
    return this.managementRequest("tool.trust", params);
  }

  public async toolUntrust(params: ToolTrustParams): Promise<ToolTrustResult> {
    return this.managementRequest("tool.untrust", params);
  }

  public async commandsList(mode: "chat" | "forge" = "chat"): Promise<ManagementResult & {
    commands: Array<{ name: string; usage: string; description: string }>;
  }> {
    return this.managementRequest("commands.list", { mode });
  }

  /** Subagents and role models with the model each would run on; read-only. */
  public async agentsList(params: { workspace?: string; path?: string } = {}): Promise<ManagementResult & {
    subagents_enabled: boolean;
    user_agents_dir: string;
    agents: Array<Record<string, unknown>>;
    roles: Array<Record<string, unknown>>;
  }> {
    return this.managementRequest("agents.list", params);
  }

  public async skillList(params: WorkspaceScopedParams): Promise<SkillListResult> {
    return this.managementRequest("skill.list", params);
  }

  public async skillInfo(params: SkillInfoParams): Promise<SkillInfoResult> {
    return this.managementRequest("skill.info", params);
  }

  public async skillInit(params: SkillInitParams): Promise<ManagementResult> {
    return this.managementRequest("skill.init", params);
  }

  public async skillValidate(params: SkillValidateParams): Promise<SkillValidateResult> {
    return this.managementRequest("skill.validate", params);
  }

  public async skillInstall(params: SkillInstallParams): Promise<ManagementResult> {
    return this.managementRequest("skill.install", params);
  }

  public async skillEnable(params: SkillToggleParams): Promise<SkillToggleResult> {
    return this.managementRequest("skill.enable", params);
  }

  public async skillDisable(params: SkillToggleParams): Promise<SkillToggleResult> {
    return this.managementRequest("skill.disable", params);
  }

  public async skillRemove(params: SkillRemoveParams): Promise<ManagementResult> {
    return this.managementRequest("skill.remove", params);
  }

  public async doctorSummary(): Promise<DoctorSummaryResult> {
    return this.managementRequest("doctor.summary");
  }

  public async doctorProviders(): Promise<DoctorProvidersResult> {
    return this.managementRequest("doctor.providers");
  }

  /**
   * One explicit-intent live provider check. Only called from direct user actions — never from
   * activation, status refresh, or any passive flow (`allow_live` is a required param for exactly
   * that reason).
   */
  public async doctorProvidersLive(
    params: DoctorProvidersLiveParams
  ): Promise<DoctorProvidersLiveResult> {
    return this.managementRequest("doctor.providers.live", params);
  }

  public async doctorBundle(): Promise<DoctorBundleResult> {
    return this.managementRequest("doctor.bundle");
  }

  public async sandboxDoctor(params: SandboxDoctorParams = {}): Promise<SandboxDoctorResult> {
    return this.managementRequest("sandbox.doctor", params);
  }

  public async sandboxSetup(params: SandboxSetupParams): Promise<SandboxMutationResult> {
    return this.managementRequest("sandbox.setup", params);
  }

  public async sandboxPull(params: SandboxPullParams): Promise<SandboxMutationResult> {
    return this.managementRequest("sandbox.pull", params);
  }

  public async updateCheck(params: UpdateCheckParams = {}): Promise<UpdateCheckResult> {
    return this.managementRequest("update.check", params);
  }

  public async reportCreate(params: ReportCreateParams): Promise<ReportCreateResult> {
    return this.managementRequest("report.create", params);
  }

  public async mcpStatus(params: McpWorkspaceParams): Promise<McpStatusResult> {
    return this.managementRequest("mcp.status", params);
  }

  public async mcpServerStatus(params: McpServerStatusParams): Promise<McpServerStatusResult> {
    const result = await this.request("mcp.server.status", params as unknown as Record<string, unknown>);
    return mcpServerStatusFromRecord(result);
  }

  public async mcpServerEnable(
    params: McpServerMutationParams
  ): Promise<McpServerMutationResult> {
    const result = await this.request("mcp.server.enable", params as unknown as Record<string, unknown>);
    return mcpServerMutationFromRecord(result, "enable");
  }

  public async mcpServerDisable(
    params: McpServerMutationParams
  ): Promise<McpServerMutationResult> {
    const result = await this.request("mcp.server.disable", params as unknown as Record<string, unknown>);
    return mcpServerMutationFromRecord(result, "disable");
  }

  public async mcpServerRestart(
    params: McpServerMutationParams
  ): Promise<McpServerMutationResult> {
    const result = await this.request("mcp.server.restart", params as unknown as Record<string, unknown>);
    return mcpServerMutationFromRecord(result, "restart");
  }

  public async mcpPromptsList(params: McpPromptsListParams): Promise<McpPromptsListResult> {
    return this.managementRequest("mcp.prompts.list", params);
  }

  public async mcpPromptsGet(params: McpPromptsGetParams): Promise<McpPromptsGetResult> {
    return this.managementRequest("mcp.prompts.get", params);
  }

  public async mcpAuthStatus(params: McpAuthStatusParams): Promise<McpAuthStatusResult> {
    return this.managementRequest("mcp.auth.status", params);
  }

  public async mcpAuthLoginStart(
    params: McpAuthLoginStartParams
  ): Promise<McpAuthLoginStartResult> {
    return this.managementRequest("mcp.auth.login.start", params);
  }

  public async mcpAuthLoginStatus(params: McpAuthLoginFlowParams): Promise<McpAuthFlowResult> {
    return this.managementRequest("mcp.auth.login.status", params);
  }

  public async mcpAuthLoginCancel(params: McpAuthLoginCancelParams): Promise<McpAuthFlowResult> {
    return this.managementRequest("mcp.auth.login.cancel", params);
  }

  public async mcpAuthLogout(params: McpAuthLogoutParams): Promise<McpAuthLogoutResult> {
    return this.managementRequest("mcp.auth.logout", params);
  }

  public async hooksList(params: HooksWorkspaceParams): Promise<HooksListResult> {
    return this.managementRequest("hooks.list", params);
  }

  public async hooksDoctor(params: HooksWorkspaceParams): Promise<HooksDoctorResult> {
    return this.managementRequest("hooks.doctor", params);
  }

  public async hooksTrace(params: HooksTraceParams = {}): Promise<HooksTraceResult> {
    return this.managementRequest("hooks.trace", params);
  }

  public async hooksTest(params: HooksEventParams): Promise<HooksTestResult> {
    return this.managementRequest("hooks.test", params);
  }

  public async hooksTrust(params: HooksTrustParams): Promise<HooksTrustResult> {
    return this.managementRequest("hooks.trust", params);
  }

  public async hooksUntrust(params: HooksTrustParams): Promise<HooksTrustResult> {
    return this.managementRequest("hooks.untrust", params);
  }

  public async hooksInit(params: HooksInitParams): Promise<HooksInitResult> {
    return this.managementRequest("hooks.init", params);
  }

  public async hooksEffective(params: HooksEffectiveParams): Promise<HooksEffectiveResult> {
    return this.managementRequest("hooks.effective", params);
  }

  public async hooksEnable(params: HooksToggleParams): Promise<HooksToggleResult> {
    return this.managementRequest("hooks.enable", params);
  }

  public async hooksDisable(params: HooksToggleParams): Promise<HooksToggleResult> {
    return this.managementRequest("hooks.disable", params);
  }

  public async conventionsList(params: ConventionsListParams): Promise<ConventionsListResult> {
    return this.managementRequest("conventions.list", params);
  }

  public async conventionsRender(
    params: ConventionsRenderParams
  ): Promise<ConventionsRenderResult> {
    return this.managementRequest("conventions.render", params);
  }

  public async extSearch(params: ExtSearchParams): Promise<ExtSearchResult> {
    return this.managementRequest("ext.search", params);
  }

  public async extList(params: ExtWorkspaceParams = {}): Promise<ExtListResult> {
    return this.managementRequest("ext.list", params);
  }

  public async extInfo(params: ExtInfoParams): Promise<ExtInfoResult> {
    return this.managementRequest("ext.info", params);
  }

  public async extInstall(params: ExtInstallParams): Promise<ExtMutationResult> {
    return this.managementRequest("ext.install", params);
  }

  public async extUninstall(params: ExtMutationParams): Promise<ExtMutationResult> {
    return this.managementRequest("ext.uninstall", params);
  }

  public async extEnable(params: ExtMutationParams): Promise<ExtMutationResult> {
    return this.managementRequest("ext.enable", params);
  }

  public async extDisable(params: ExtMutationParams): Promise<ExtMutationResult> {
    return this.managementRequest("ext.disable", params);
  }

  public lastSequence(sessionId: string): number {
    return this.latestSequenceBySession.get(sessionId) ?? 0;
  }

  public dispose(): void {
    // Fire-and-forget form of disposeAsync() for VS Code's synchronous Disposable contract. Callers
    // that can wait for teardown diagnostics (deactivate) should await disposeAsync() instead.
    void this.disposeAsync();
  }

  /** Awaitable teardown: the same shutdown path as shutdown(), with listeners dropped afterwards. */
  public async disposeAsync(code = "bridge_stopped"): Promise<void> {
    try {
      await this.shutdown(code);
    } finally {
      this.removeAllListeners();
    }
  }

  public async shutdown(code = "bridge_stopped"): Promise<void> {
    await this.stopProcess(code, true);
  }

  private bindPrototypeMethods(): void {
    const self = this as unknown as Record<string, unknown>;
    for (const name of Object.getOwnPropertyNames(AlysisBridgeClient.prototype)) {
      if (name === "constructor") {
        continue;
      }
      const value = self[name];
      if (typeof value === "function") {
        self[name] = value.bind(this);
      }
    }
  }

  private async stopProcess(code: string, attemptGraceful: boolean): Promise<void> {
    const child = this.process;
    if (!child) {
      await this.waitForProcessTermination();
      return;
    }
    const closed = onceProcessClosed(child);
    if (attemptGraceful) {
      const gracefulCompleted = await withTimeout(
        this.closeIdleSessionsForShutdown(),
        GRACEFUL_SHUTDOWN_TIMEOUT_MS
      );
      if (!gracefulCompleted) {
        this.warnTrackedActiveJobs(
          "Alysis Code bridge shutdown timed out while requesting cooperative cancellation; the bridge process will be terminated and unfinished jobs may be marked interrupted after restart."
        );
      }
    }
    const gracefulShutdownRequested =
      attemptGraceful && this.initialized && this.supportsMethod("bridge.shutdown")
        ? await this.requestGracefulBridgeShutdown()
        : false;
    if (code !== "bridge_stopped") {
      this.emit("reset", code);
    }
    this.rejectPending(code, "Alysis Code IDE bridge stopped.");
    if (this.process === child) {
      this.process = undefined;
    }
    this.initialized = false;
    this.launchProfile = undefined;
    this.launchCredentialFingerprint = null;
    this.setBridgeHealth(undefined);
    let closedGracefully = false;
    if (gracefulShutdownRequested) {
      closedGracefully = await withTimeout(closed, GRACEFUL_BRIDGE_EXIT_TIMEOUT_MS);
    }
    if (!closedGracefully) {
      // Normal profile changes and explicit restarts use the same exact-child
      // termination barrier as process-error recovery. A replacement bridge
      // must never start while this child (or one of its descendants) may
      // still be alive.
      await this.beginProcessTermination(child);
    }
    this.activeJobIds.clear();
    this.pendingApprovalIds.clear();
  }

  private async requestGracefulBridgeShutdown(): Promise<boolean> {
    try {
      await this.request("bridge.shutdown", {});
      return true;
    } catch (error) {
      this.emit(
        "stderr",
        `Alysis Code bridge rejected graceful shutdown; falling back to process-tree termination: ${redactForDisplay(protocolErrorMessage(error))}`
      );
      return false;
    }
  }

  private async closeIdleSessionsForShutdown(): Promise<void> {
    if (!this.process || !this.initialized) {
      this.warnTrackedActiveJobs(
        "Alysis Code bridge was not initialized during shutdown; cooperative cancellation could not be requested before terminating the bridge process."
      );
      return;
    }
    let sessions: SessionListResult;
    try {
      sessions = await this.sessionList();
    } catch {
      this.warnTrackedActiveJobs(
        "Alysis Code bridge sessions could not be listed during shutdown; cooperative cancellation could not be requested before terminating the bridge process."
      );
      return;
    }
    for (const session of sessions.sessions) {
      const job = session.active_job;
      if (job && ACTIVE_JOB_STATUSES.has(job.status)) {
        await this.requestActiveJobCancellationForShutdown(session.session_id, job);
        continue;
      }
      try {
        await this.cancelSession(session.session_id);
      } catch {
        // Continue shutdown; pending approvals time out or are denied by the bridge close path.
      }
    }
  }

  private async requestActiveJobCancellationForShutdown(sessionId: string, job: JobStatusResult): Promise<void> {
    const jobId = job.job_id;
    const activeCancellationSupported =
      this.supportsFeature(["cancellation", "active_jobs"]) === true && this.supportsMethod("session.cancel");
    if (!activeCancellationSupported) {
      this.emit(
        "stderr",
        `Active Alysis Code job ${jobId} is still ${job.status}; the bridge does not advertise cooperative active-job cancellation, so shutdown will terminate the process and the job may be marked interrupted.`
      );
      return;
    }

    if (job.status !== "cancellation_requested") {
      let result: Record<string, unknown>;
      try {
        result = await this.cancelSession(sessionId);
      } catch (error) {
        this.emit(
          "stderr",
          `Active Alysis Code job ${jobId}: cooperative cancellation request failed during shutdown: ${redactForDisplay(protocolErrorMessage(error))}`
        );
        return;
      }
      const status = protocolStatus(result) || "cancellation_requested";
      if (status === "non_cancellable") {
        this.emit(
          "stderr",
          `Active Alysis Code job ${jobId} is non-cancellable according to the backend; shutdown will terminate the bridge process without reporting cancellation success.`
        );
        return;
      }
      if (FINAL_JOB_STATUSES.has(status) || status === "no_active_job" || status === "closed") {
        this.activeJobIds.delete(jobId);
        return;
      }
      if (status !== "cancellation_requested") {
        this.emit(
          "stderr",
          `Active Alysis Code job ${jobId}: cancellation request returned ${redactForDisplay(status)}; waiting for terminal status before shutdown.`
        );
      }
    }

    const reachedTerminal = await this.waitForJobTerminalDuringShutdown(jobId);
    if (!reachedTerminal) {
      this.emit(
        "stderr",
        `Active Alysis Code job ${jobId}: cooperative cancellation did not reach a terminal state before shutdown timeout; the bridge process will be terminated and the job may be marked interrupted.`
      );
    }
  }

  private async waitForJobTerminalDuringShutdown(jobId: string): Promise<boolean> {
    const deadline = Date.now() + SHUTDOWN_CANCEL_SETTLE_TIMEOUT_MS;
    while (Date.now() <= deadline) {
      try {
        const status = await this.jobStatus(jobId);
        if (FINAL_JOB_STATUSES.has(status.status)) {
          this.activeJobIds.delete(jobId);
          return true;
        }
        if (status.status === "non_cancellable") {
          return false;
        }
      } catch (error) {
        this.emit(
          "stderr",
          `Active Alysis Code job ${jobId}: job status polling failed during shutdown: ${redactForDisplay(protocolErrorMessage(error))}`
        );
        return false;
      }
      await sleep(SHUTDOWN_CANCEL_POLL_INTERVAL_MS);
    }
    return false;
  }

  private warnTrackedActiveJobs(message: string): void {
    if (this.activeJobIds.size === 0) {
      return;
    }
    for (const jobId of this.activeJobIds) {
      this.emit("stderr", `Active Alysis Code job ${jobId}: ${message}`);
    }
  }

  private handleBridgeLine(line: string): void {
    let payload: unknown;
    try {
      payload = JSON.parse(line);
    } catch (error) {
      this.noteFrameParseFailure(line, error instanceof Error ? error : new Error(String(error)));
      return;
    }
    this.consecutiveFrameParseFailures = 0;

    if (isProtocolResponse(payload)) {
      this.handleResponse(payload);
      return;
    }
    if (isProtocolEvent(payload)) {
      this.noteEventSequence(payload);
      this.noteApprovalEvent(payload);
      this.emit("event", payload);
      this.emit(`session:${payload.session_id}`, payload);
      if (payload.job_id) {
        this.emit(`job:${payload.job_id}`, payload);
      }
    }
  }

  /**
   * Log unreadable stdout instead of turning every stray line into a user-visible error card. A
   * single pip/Python warning on stdout is a diagnostic; a sustained run of unreadable frames, or a
   * frame that claims to be protocol output, is a real desync worth escalating.
   */
  private noteFrameParseFailure(line: string, error: Error): void {
    this.consecutiveFrameParseFailures += 1;
    const preview = redactForDisplay(line.slice(0, MAX_LOGGED_FRAME_PREVIEW_CHARS).trim());
    this.emit(
      "stderr",
      `Ignored unreadable Alysis Code bridge output (${redactForDisplay(error.message)}): ${preview}`
    );
    if (
      this.consecutiveFrameParseFailures < MAX_CONSECUTIVE_FRAME_PARSE_FAILURES &&
      !looksLikeProtocolFrame(line)
    ) {
      return;
    }
    const failures = this.consecutiveFrameParseFailures;
    this.consecutiveFrameParseFailures = 0;
    this.emitClientError(
      new ProtocolClientError(
        "bridge_protocol_desync",
        `The Alysis Code CLI wrote ${failures} unreadable frame(s) to stdout. Check the Alysis Code output channel: the CLI may be printing non-protocol output over the IDE bridge.`
      )
    );
  }

  private noteOversizeFrame(bytes: number): void {
    this.emit(
      "stderr",
      `Dropped an oversized Alysis Code bridge frame (${bytes} bytes, limit ${MAX_INBOUND_FRAME_BYTES}). The CLI is writing more output than the IDE bridge protocol allows.`
    );
  }

  /** Clip and rate limit stderr so a runaway CLI cannot grow the Extension Host heap through logs. */
  private forwardStderrChunk(chunk: Buffer | string): void {
    const text = redactForDisplay(chunk.toString().trim());
    if (text.length === 0) {
      return;
    }
    const now = Date.now();
    if (now - this.stderrWindowStartedAt >= STDERR_WINDOW_MS) {
      const suppressed = this.stderrSuppressedChunks;
      this.stderrWindowStartedAt = now;
      this.stderrWindowChars = 0;
      this.stderrSuppressedChunks = 0;
      if (suppressed > 0) {
        this.emit("stderr", `Alysis Code CLI diagnostics: ${suppressed} further message(s) were suppressed.`);
      }
    }
    if (this.stderrWindowChars >= MAX_STDERR_WINDOW_CHARS) {
      this.stderrSuppressedChunks += 1;
      return;
    }
    const bounded =
      text.length > MAX_STDERR_CHUNK_CHARS
        ? `${text.slice(0, MAX_STDERR_CHUNK_CHARS)}… (truncated ${text.length - MAX_STDERR_CHUNK_CHARS} characters)`
        : text;
    this.stderrWindowChars += bounded.length;
    this.emit("stderr", bounded);
  }

  private handleResponse(response: ProtocolResponse): void {
    if (response.id === null || response.id === undefined) {
      this.handleUncorrelatedResponse(response);
      return;
    }
    const pending = this.pending.get(response.id);
    if (!pending) {
      this.emit("response", response);
      return;
    }
    clearTimeout(pending.timeout);
    this.pending.delete(response.id);
    this.emit("response", response);

    if (response.ok) {
      pending.resolve(isRecord(response.result) ? response.result : {});
      return;
    }
    const code = response.error?.code ?? "protocol_error";
    pending.reject(new ProtocolClientError(code, response.error?.message ?? `Bridge request failed: ${pending.method}.`, response.error?.details));
  }

  /**
   * The bridge answers anything it could not parse — including a frame above its 1 MiB limit — with
   * `id: null`, which can never match a pending `vscode-N` key. Without this the caller waits out
   * its whole timeout and then reports a misleading "timed out", so fail the in-flight request(s)
   * with the code and message the bridge actually returned.
   */
  private handleUncorrelatedResponse(response: ProtocolResponse): void {
    this.emit("response", response);
    if (response.ok !== false || this.pending.size === 0) {
      return;
    }
    this.rejectPending(
      response.error?.code ?? "protocol_error",
      response.error?.message ?? "Alysis Code bridge rejected the request without correlating it to an id."
    );
  }

  private handleProcessError(child: BridgeProcess, error: Error): void {
    if (this.process !== child) {
      return;
    }
    const message = redactForDisplay(error.message);
    this.process = undefined;
    this.initialized = false;
    this.launchProfile = undefined;
    this.launchCredentialFingerprint = null;
    this.setBridgeHealth(undefined);
    // Drop the broken process from the request path immediately, but retain an exact reference in
    // the termination barrier. startStdio() waits on that barrier, so a replacement can never be
    // spawned while an errored bridge (or one of its descendants) may still be alive.
    void this.beginProcessTermination(child);
    this.rejectPending("bridge_process_error", message);
    this.activeJobIds.clear();
    this.pendingApprovalIds.clear();
    this.emitClientError(new Error(message));
    // A stream/process error does not always lead to an `exit` event (for example, a
    // broken stdin pipe can leave the child alive briefly). Notify every controller
    // immediately so none of them retain session or Forge ids owned by a bridge that
    // is no longer usable.
    this.emit("reset", "bridge_process_error");
  }

  private waitForProcessTermination(): Promise<void> {
    return this.terminationPromise ?? Promise.resolve();
  }

  /**
   * Terminate one exact bridge process at most once. Stream and process `error` can both fire for
   * the same failure, and startup cleanup can race those listeners; all paths share this promise.
   */
  private beginProcessTermination(child: BridgeProcess): Promise<void> {
    if (this.terminatingProcess === child && this.terminationPromise) {
      return this.terminationPromise;
    }

    const closed = onceProcessClosed(child);
    const terminate = async (): Promise<void> => {
      // Startup can fail because the child exited before this teardown path installed its waiter.
      // ChildProcess retains that terminal state; do not signal or wait for an event that already
      // happened.
      if (hasProcessExited(child)) {
        return;
      }
      try {
        await terminateBridgeProcessTree(child, false);
      } catch (error) {
        this.emit("stderr", `Could not terminate the failed Alysis Code bridge cleanly: ${redactForDisplay(protocolErrorMessage(error))}`);
      }
      const closedAfterTerminate = await withTimeout(closed, PROCESS_STOP_TIMEOUT_MS);
      if (!closedAfterTerminate) {
        try {
          await terminateBridgeProcessTree(child, true);
        } catch (error) {
          this.emit("stderr", `Could not force-stop the failed Alysis Code bridge: ${redactForDisplay(protocolErrorMessage(error))}`);
        }
        const closedAfterForce = await withTimeout(closed, PROCESS_STOP_TIMEOUT_MS);
        if (!closedAfterForce) {
          this.emit(
            "stderr",
            "The failed Alysis Code bridge has not reported exit after forced termination; replacement startup remains blocked to avoid overlapping bridge processes."
          );
          await closed;
        }
      }
    };

    this.terminatingProcess = child;
    const termination = terminate();
    const tracked = termination.finally(() => {
      if (this.terminationPromise === tracked) {
        this.terminationPromise = undefined;
        this.terminatingProcess = undefined;
      }
    });
    this.terminationPromise = tracked;
    return tracked;
  }

  private handleProcessExit(child: BridgeProcess, code: number | null): void {
    if (this.process !== child) {
      return;
    }
    this.process = undefined;
    this.initialized = false;
    this.launchProfile = undefined;
    this.launchCredentialFingerprint = null;
    this.setBridgeHealth(undefined);
    this.rejectPending("bridge_exited", `Alysis Code IDE bridge exited with code ${code ?? "unknown"}.`);
    this.activeJobIds.clear();
    this.pendingApprovalIds.clear();
    this.emit("exit", code);
  }

  private rejectPending(code: string, message: string): void {
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timeout);
      pending.reject(new ProtocolClientError(code, message));
      this.pending.delete(id);
    }
  }

  private waitForProcessStart(child: BridgeProcess): Promise<void> {
    return new Promise((resolve, reject) => {
      let settled = false;
      let timeout: NodeJS.Timeout | undefined;
      const cleanup = () => {
        if (timeout) {
          clearTimeout(timeout);
        }
        child.off("spawn", onSpawn);
        child.off("error", onError);
        child.off("exit", onExit);
        child.off("close", onExit);
      };
      const settle = (fn: () => void) => {
        if (settled) {
          return;
        }
        settled = true;
        cleanup();
        fn();
      };
      const onSpawn = () => settle(resolve);
      const onError = (error: Error) =>
        settle(() =>
          reject(
            new ProtocolClientError(
              "bridge_start_failed",
              `Alysis Code IDE bridge failed to start: ${redactForDisplay(error.message)}`
            )
          )
        );
      const onExit = (code: number | null) =>
        settle(() =>
          reject(
            new ProtocolClientError(
              "bridge_start_failed",
              `Alysis Code IDE bridge exited before startup completed with code ${code ?? "unknown"}.`
            )
          )
        );
      timeout = setTimeout(
        () =>
          settle(() =>
            reject(
              new ProtocolClientError(
                "bridge_start_timeout",
                "Alysis Code IDE bridge did not report startup in time."
              )
            )
          ),
        PROCESS_START_TIMEOUT_MS
      );
      child.once("spawn", onSpawn);
      child.once("error", onError);
      child.once("exit", onExit);
      child.once("close", onExit);
    });
  }

  private async initializeLiveBridge(): Promise<void> {
    const result = await this.request("initialize", {}, INITIALIZE_TIMEOUT_MS);
    this.setBridgeHealth(parseHealthRecord(result));
    this.initialized = true;
  }

  private async managementRequest<T extends ManagementResult>(
    method: ManagementBridgeMethod,
    params: object = {}
  ): Promise<T> {
    return (await this.request(method, params as Record<string, unknown>)) as T;
  }

  private trackStartedJob(result: Record<string, unknown>): JobStartResult {
    const started = {
      session_id: requireString(result, "session_id"),
      job_id: requireString(result, "job_id"),
      status: requireString(result, "status")
    };
    if (
      started.status === "started" ||
      started.status === "queued" ||
      started.status === "running" ||
      started.status === "cancellation_requested"
    ) {
      this.activeJobIds.add(started.job_id);
    }
    return started;
  }

  private noteEventSequence(event: ProtocolEventEnvelope): void {
    const previous = this.latestSequenceBySession.get(event.session_id) ?? 0;
    if (event.sequence > previous) {
      this.latestSequenceBySession.set(event.session_id, event.sequence);
    }
  }

  private noteApprovalEvent(event: ProtocolEventEnvelope): void {
    if (event.type !== "prompt_for_input") {
      return;
    }
    const kind = typeof event.payload.kind === "string" ? event.payload.kind : "";
    const approvalId =
      typeof event.payload.approval_id === "string"
        ? event.payload.approval_id
        : typeof event.payload.prompt_id === "string"
          ? event.payload.prompt_id
          : "";
    if (!approvalId) {
      return;
    }
    const key = approvalKey(event.session_id, approvalId);
    if (kind === "approval") {
      this.pendingApprovalIds.add(key);
      return;
    }
    if (kind === "approval_result") {
      this.pendingApprovalIds.delete(key);
    }
  }

  private emitClientError(error: Error): void {
    if (this.listenerCount("error") > 0) {
      this.emit("error", error);
    }
  }
}

function defaultBridgeProcessFactory(
  command: string,
  args: readonly string[],
  options: BridgeProcessFactoryOptions
): BridgeProcess {
  const child = spawn(command, [...args], {
    env: options.env,
    stdio: "pipe",
    windowsHide: true,
    detached: process.platform !== "win32"
  }) as ChildProcessWithoutNullStreams;
  const bridgeProcess = child as BridgeProcess;
  bridgeProcess.alysisProcessGroupIsolated = process.platform !== "win32";
  return bridgeProcess;
}

async function terminateBridgeProcessTree(child: BridgeProcess, force: boolean): Promise<void> {
  const pid = child.pid;
  if (process.platform === "win32" && typeof pid === "number" && Number.isSafeInteger(pid) && pid > 0) {
    await runTaskkill(pid, force);
    return;
  }
  if (
    process.platform !== "win32" &&
    child.alysisProcessGroupIsolated === true &&
    typeof pid === "number" &&
    Number.isSafeInteger(pid) &&
    pid > 0
  ) {
    try {
      process.kill(-pid, force ? "SIGKILL" : "SIGTERM");
      return;
    } catch {
      // The process may already have exited or the host may not support group signals.
    }
  }
  child.kill(force ? "SIGKILL" : "SIGTERM");
}

/**
 * Absolute System32 path for taskkill. Windows CreateProcess searches the parent's working directory
 * before the system directory, so a bare "taskkill.exe" can be satisfied by a workspace-planted
 * binary during process-tree teardown.
 */
export function taskkillExecutablePath(env: NodeJS.ProcessEnv = process.env): string {
  return path.join(env.SystemRoot ?? "C:\\Windows", "System32", "taskkill.exe");
}

async function runTaskkill(pid: number, force: boolean): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const args = ["/PID", String(pid), "/T"];
    if (force) {
      args.push("/F");
    }
    const killer = spawn(taskkillExecutablePath(), args, {
      windowsHide: true,
      stdio: "ignore",
      shell: false
    });
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        killer.kill();
        finish(new Error(`taskkill.exe timed out while stopping bridge process ${pid}.`));
      }
    }, PROCESS_STOP_TIMEOUT_MS);
    timer.unref();
    const finish = (error?: Error): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      if (error) {
        reject(error);
      } else {
        resolve();
      }
    };
    killer.once("error", (error) => {
      finish(new Error(`Could not launch taskkill.exe for bridge process ${pid}: ${error.message}`));
    });
    killer.once("close", (code) => {
      if (code === 0) {
        finish();
        return;
      }
      finish(new Error(`taskkill.exe exited with code ${String(code)} for bridge process ${pid}.`));
    });
  });
}

/**
 * A line that announces itself as protocol output but does not parse is a real desync, not stray
 * CLI chatter, so it is escalated immediately instead of waiting for a run of failures.
 */
function looksLikeProtocolFrame(line: string): boolean {
  const trimmed = line.trimStart();
  return trimmed.startsWith("{") && trimmed.includes("\"protocol_version\"");
}

/**
 * Bounded JSONL framing for the bridge's stdout. readline buffers an unterminated line with no
 * limit, so a runaway CLI could grow the Extension Host heap until the window dies. This transform
 * assembles frames itself, forwards only complete frames within the cap, and drops anything larger
 * (reported once per frame) so the rest of the stream keeps working.
 */
class BoundedFrameStream extends Transform {
  private pending: Buffer[] = [];
  private pendingBytes = 0;
  private dropping = false;

  public constructor(
    private readonly maxBytes: number,
    private readonly onOversizeFrame: (bytes: number) => void
  ) {
    super();
  }

  public override _transform(chunk: Buffer | string, _encoding: BufferEncoding, done: TransformCallback): void {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), "utf8");
    let offset = 0;
    while (offset < buffer.length) {
      const newline = buffer.indexOf(0x0a, offset);
      const end = newline === -1 ? buffer.length : newline + 1;
      this.appendSegment(buffer.subarray(offset, end), newline !== -1);
      offset = end;
    }
    done();
  }

  public override _flush(done: TransformCallback): void {
    // A final frame without a trailing newline is still a frame; an oversize one stays dropped.
    if (!this.dropping && this.pendingBytes > 0) {
      this.push(Buffer.concat(this.pending, this.pendingBytes));
    }
    this.resetFrame();
    done();
  }

  private appendSegment(segment: Buffer, complete: boolean): void {
    if (!this.dropping && this.pendingBytes + segment.length > this.maxBytes) {
      this.dropping = true;
      this.pending = [];
    }
    this.pendingBytes += segment.length;
    if (!this.dropping) {
      this.pending.push(segment);
    }
    if (!complete) {
      return;
    }
    if (this.dropping) {
      const bytes = this.pendingBytes;
      this.resetFrame();
      this.onOversizeFrame(bytes);
      return;
    }
    this.push(Buffer.concat(this.pending, this.pendingBytes));
    this.resetFrame();
  }

  private resetFrame(): void {
    this.pending = [];
    this.pendingBytes = 0;
    this.dropping = false;
  }
}

function isProtocolResponse(value: unknown): value is ProtocolResponse {
  return (
    isRecord(value) &&
    "ok" in value &&
    "id" in value &&
    typeof value.protocol_version === "string"
  );
}

function isProtocolEvent(value: unknown): value is ProtocolEventEnvelope {
  return (
    isRecord(value) &&
    typeof value.protocol_version === "string" &&
    typeof value.session_id === "string" &&
    typeof value.sequence === "number" &&
    typeof value.type === "string" &&
    isRecord(value.payload)
  );
}

function requireString(record: Record<string, unknown>, field: string): string {
  const value = record[field];
  if (typeof value !== "string") {
    return "";
  }
  return value;
}

function requireNumber(record: Record<string, unknown>, field: string): number {
  const value = nullableNumber(record[field]);
  if (value === null) {
    throw new ProtocolClientError("invalid_response", `Bridge response field ${field} is invalid.`);
  }
  return value;
}

function isHostActionName(value: string): value is HostActionName {
  return (HOST_ACTION_NAMES as readonly string[]).includes(value);
}

function hostActionsNegotiationFromRecord(record: Record<string, unknown>): HostActionsNegotiation {
  const protocolVersion = requireString(record, "protocol_version");
  const workspaceFence = requireString(record, "workspace_fence");
  const capabilityFingerprint = requireString(record, "capability_fingerprint");
  const requestEvent = requireString(record, "request_event");
  const cancellationEvent = requireString(record, "cancellation_event");
  const sessionClosedEvent = requireString(record, "session_closed_event");
  const responseMethod = requireString(record, "response_method");
  const requestTimeoutSeconds = requireNumber(record, "request_timeout_seconds");
  const maxArgumentBytes = requireNumber(record, "max_argument_bytes");
  const maxResultBytes = requireNumber(record, "max_result_bytes");
  const rawActions = record.actions;
  const actions = Array.isArray(rawActions)
    ? rawActions.filter((value): value is string => typeof value === "string")
    : [];
  if (
    protocolVersion !== "1"
    || !/^wf_[A-Za-z0-9_-]{1,252}$/.test(workspaceFence)
    || !/^[a-f0-9]{64}$/i.test(capabilityFingerprint)
    || requestEvent !== "host_action_requested"
    || cancellationEvent !== "host_action_cancelled"
    || sessionClosedEvent !== "session_closed"
    || responseMethod !== "host.action.respond"
    || !Number.isInteger(requestTimeoutSeconds)
    || requestTimeoutSeconds < 1
    || requestTimeoutSeconds > 300
    || !Number.isInteger(maxArgumentBytes)
    || maxArgumentBytes < 1
    || maxArgumentBytes > 8_192
    || !Number.isInteger(maxResultBytes)
    || maxResultBytes < 1
    || maxResultBytes > 65_536
    || !Array.isArray(rawActions)
    || actions.length !== rawActions.length
    || actions.some((action) => !isHostActionName(action))
    || new Set(actions).size !== actions.length
  ) {
    throw new ProtocolClientError(
      "invalid_response",
      "Alysis Code bridge returned an invalid host action negotiation."
    );
  }
  return {
    protocol_version: "1",
    actions: actions as HostActionName[],
    workspace_fence: workspaceFence,
    capability_fingerprint: capabilityFingerprint,
    request_event: "host_action_requested",
    cancellation_event: "host_action_cancelled",
    session_closed_event: "session_closed",
    response_method: "host.action.respond",
    request_timeout_seconds: requestTimeoutSeconds,
    max_argument_bytes: maxArgumentBytes,
    max_result_bytes: maxResultBytes
  };
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function nullableBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

const MCP_SERVER_CONNECTION_STATES = new Set([
  "disabled",
  "connected",
  "disconnected",
  "not_materialized"
]);

function invalidMcpServerResponse(field: string): ProtocolClientError {
  return new ProtocolClientError(
    "invalid_response",
    `MCP server lifecycle response field ${field} is invalid.`
  );
}

function requireMcpServerString(record: Record<string, unknown>, field: string): string {
  const value = record[field];
  if (typeof value !== "string" || value.trim().length === 0) {
    throw invalidMcpServerResponse(field);
  }
  return value;
}

function requireMcpServerBoolean(record: Record<string, unknown>, field: string): boolean {
  const value = record[field];
  if (typeof value !== "boolean") {
    throw invalidMcpServerResponse(field);
  }
  return value;
}

function requireMcpServerCount(record: Record<string, unknown>, field: string): number {
  const value = record[field];
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw invalidMcpServerResponse(field);
  }
  return value;
}

function mcpServerStatusFromRecord(record: Record<string, unknown>): McpServerStatusResult {
  const connectionState = requireMcpServerString(record, "connection_state");
  if (!MCP_SERVER_CONNECTION_STATES.has(connectionState)) {
    throw invalidMcpServerResponse("connection_state");
  }
  if (record.secret_values_included !== false) {
    throw invalidMcpServerResponse("secret_values_included");
  }
  return {
    session_id: requireMcpServerString(record, "session_id"),
    server_id: requireMcpServerString(record, "server_id"),
    transport: requireMcpServerString(record, "transport"),
    enabled: requireMcpServerBoolean(record, "enabled"),
    connection_state: connectionState as McpServerStatusResult["connection_state"],
    connected: requireMcpServerBoolean(record, "connected"),
    generation: requireMcpServerCount(record, "generation"),
    catalog_initialized: requireMcpServerBoolean(record, "catalog_initialized"),
    exposed_tool_count: requireMcpServerCount(record, "exposed_tool_count"),
    snapshotted_resource_count: requireMcpServerCount(record, "snapshotted_resource_count"),
    prompt_snapshot_loaded: requireMcpServerBoolean(record, "prompt_snapshot_loaded"),
    snapshotted_prompt_count: requireMcpServerCount(record, "snapshotted_prompt_count"),
    secret_values_included: false
  };
}

function mcpServerMutationFromRecord(
  record: Record<string, unknown>,
  expectedAction: McpServerMutationAction
): McpServerMutationResult {
  const action = requireMcpServerString(record, "action");
  if (action !== expectedAction) {
    throw invalidMcpServerResponse("action");
  }
  return {
    ...mcpServerStatusFromRecord(record),
    changed: requireMcpServerBoolean(record, "changed"),
    action: expectedAction
  };
}

function invalidBrowserResponse(field: string): ProtocolClientError {
  return new ProtocolClientError(
    "invalid_response",
    `Managed browser response field ${field} is invalid.`
  );
}

function requireBrowserRecord(value: unknown, field: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw invalidBrowserResponse(field);
  }
  return value;
}

function requireBrowserString(
  record: Record<string, unknown>,
  field: string,
  allowEmpty = false
): string {
  const value = record[field];
  if (typeof value !== "string" || (!allowEmpty && value.trim().length === 0)) {
    throw invalidBrowserResponse(field);
  }
  return value;
}

function requireBrowserBoolean(record: Record<string, unknown>, field: string): boolean {
  const value = record[field];
  if (typeof value !== "boolean") {
    throw invalidBrowserResponse(field);
  }
  return value;
}

function requireBrowserNonNegativeNumber(record: Record<string, unknown>, field: string): number {
  const value = record[field];
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw invalidBrowserResponse(field);
  }
  return value;
}

function managedBrowserScopeFromRecord(record: Record<string, unknown>): ManagedBrowserSessionParams {
  return {
    session_id: requireBrowserString(record, "session_id"),
    browser_session_id: requireBrowserString(record, "browser_session_id")
  };
}

function managedBrowserSessionStatusFromRecord(
  record: Record<string, unknown>
): ManagedBrowserSessionStatus {
  const state = requireBrowserString(record, "state");
  if (state !== "running" && state !== "crashed") {
    throw invalidBrowserResponse("state");
  }
  const activeUrl = record.active_url;
  if (activeUrl !== null && typeof activeUrl !== "string") {
    throw invalidBrowserResponse("active_url");
  }
  const networkScope = requireBrowserString(record, "network_scope");
  if (networkScope !== "public" && networkScope !== "public_loopback" && networkScope !== "local_network") {
    throw invalidBrowserResponse("network_scope");
  }
  const legacyAllowLocal = record.allow_local_destinations;
  if (legacyAllowLocal !== undefined && typeof legacyAllowLocal !== "boolean") {
    throw invalidBrowserResponse("allow_local_destinations");
  }
  return {
    browser_session_id: requireBrowserString(record, "browser_session_id"),
    product: requireBrowserString(record, "product"),
    state,
    created_at: requireBrowserNonNegativeNumber(record, "created_at"),
    network_scope: networkScope,
    ...(typeof legacyAllowLocal === "boolean" ? { allow_local_destinations: legacyAllowLocal } : {}),
    active_url: activeUrl,
    artifact_count: requireBrowserNonNegativeNumber(record, "artifact_count")
  };
}

function managedBrowserStatusFromRecord(
  record: Record<string, unknown>
): ManagedBrowserStatusResult {
  return {
    session_id: requireBrowserString(record, "session_id"),
    ...managedBrowserSessionStatusFromRecord(record)
  };
}

function managedBrowserBoundedPayloadFromRecord(
  record: Record<string, unknown>
): ManagedBrowserBoundedPayload {
  if (!Object.prototype.hasOwnProperty.call(record, "data") || record.data === undefined) {
    throw invalidBrowserResponse("data");
  }
  return {
    data: record.data,
    truncated: requireBrowserBoolean(record, "truncated"),
    size_bytes: requireBrowserNonNegativeNumber(record, "size_bytes")
  };
}

function managedBrowserSnapshotFromRecord(
  record: Record<string, unknown>
): ManagedBrowserSnapshotResult {
  const scope = managedBrowserScopeFromRecord(record);
  const kind = requireBrowserString(record, "kind");
  const truncated = requireBrowserBoolean(record, "truncated");
  const sizeBytes = requireBrowserNonNegativeNumber(record, "size_bytes");
  if (kind === "text") {
    return {
      ...scope,
      kind,
      text: requireBrowserString(record, "text", true),
      truncated,
      size_bytes: sizeBytes
    };
  }
  if (kind !== "semantic" && kind !== "accessibility" && kind !== "dom") {
    throw invalidBrowserResponse("kind");
  }
  if (!Object.prototype.hasOwnProperty.call(record, "data") || record.data === undefined) {
    throw invalidBrowserResponse("data");
  }
  return {
    ...scope,
    kind,
    data: record.data,
    truncated,
    size_bytes: sizeBytes
  };
}

function managedBrowserDiagnosticEventFromRecord(
  record: Record<string, unknown>
): ManagedBrowserDiagnosticEvent {
  const category = requireBrowserString(record, "category");
  if (category !== "console" && category !== "network") {
    throw invalidBrowserResponse("events.category");
  }
  return {
    category,
    method: requireBrowserString(record, "method"),
    params: managedBrowserBoundedPayloadFromRecord(requiredRecord(record, "params"))
  };
}

function normalizeMode(value: string): "readonly" | "review" | "auto" {
  return value === "readonly" || value === "auto" ? value : "review";
}

function requiredRecord(record: Record<string, unknown>, field: string): Record<string, unknown> {
  const value = record[field];
  if (!isRecord(value)) {
    throw new ProtocolClientError("invalid_response", `Bridge response field ${field} is invalid.`);
  }
  return value;
}

function forgeSwarmDurableStatusFromRecord(
  record: Record<string, unknown>
): ForgeSwarmDurableStatus {
  const state = record.state;
  if (
    state !== "queued" &&
    state !== "running" &&
    state !== "interrupted" &&
    state !== "succeeded" &&
    state !== "failed" &&
    state !== "cancelled"
  ) {
    throw new ProtocolClientError(
      "invalid_response",
      "Bridge returned an invalid durable Forge swarm state."
    );
  }
  const usage = requiredRecord(record, "usage");
  return {
    job_id: requireString(record, "job_id"),
    state,
    revision: requireNumber(record, "revision"),
    attempts: requireNumber(record, "attempts"),
    resume_count: requireNumber(record, "resume_count"),
    created_at: requireNumber(record, "created_at"),
    updated_at: requireNumber(record, "updated_at"),
    started_at: nullableNumber(record.started_at),
    terminal_at: nullableNumber(record.terminal_at),
    lease_expires_at: nullableNumber(record.lease_expires_at),
    result_available: record.result_available === true,
    error_code: nullableString(record.error_code),
    error_summary: nullableString(record.error_summary),
    usage: {
      calls: requireNumber(usage, "calls"),
      input_tokens: requireNumber(usage, "input_tokens"),
      output_tokens: requireNumber(usage, "output_tokens"),
      cached_input_tokens: requireNumber(usage, "cached_input_tokens"),
      total_tokens: requireNumber(usage, "total_tokens")
    },
    resumable: record.resumable === true
  };
}

function checkpointFromRecord(record: Record<string, unknown>): CheckpointRecord {
  return {
    checkpoint_id: requireString(record, "checkpoint_id"),
    session_id: requireString(record, "session_id"),
    turn_id: requireString(record, "turn_id"),
    step_id: nullableString(record.step_id),
    parent_id: nullableString(record.parent_id),
    kind: requireString(record, "kind"),
    created_at: requireString(record, "created_at"),
    message: requireString(record, "message"),
    changes: Array.isArray(record.changes)
      ? record.changes.filter(isRecord).map((change) => ({
          path: requireString(change, "path"),
          kind: requireString(change, "kind")
        }))
      : [],
    omitted_paths: Array.isArray(record.omitted_paths)
      ? record.omitted_paths.filter((item): item is string => typeof item === "string")
      : [],
    reverts_id: nullableString(record.reverts_id),
    redoes_id: nullableString(record.redoes_id)
  };
}

function permissionRuleFromRecord(record: Record<string, unknown>): PermissionRule {
  const rawEffect = requireString(record, "effect");
  const effect: PermissionRule["effect"] =
    rawEffect === "allow" || rawEffect === "deny" ? rawEffect : "ask";
  return {
    id: requireString(record, "id"),
    effect,
    order: typeof record.order === "number" ? record.order : 0,
    source: requireString(record, "source"),
    tool_pattern: nullableString(record.tool_pattern),
    path_pattern: nullableString(record.path_pattern),
    has_command_pattern: record.has_command_pattern === true
  };
}

function permissionEvaluationFromRecord(record: Record<string, unknown>): PermissionEvaluation {
  const rawDecision = requireString(record, "decision");
  const decision: PermissionEvaluation["decision"] =
    rawDecision === "allow" || rawDecision === "deny" ? rawDecision : "ask";
  return {
    decision,
    reason: requireString(record, "reason"),
    matched_rule_id: nullableString(record.matched_rule_id),
    matched_rule_source: nullableString(record.matched_rule_source),
    specificity: typeof record.specificity === "number" ? record.specificity : 0
  };
}

function normalizeCodeReviewScope(value: unknown): CodeReviewScope {
  if (value === "working_tree" || value === "branch" || value === "commit" || value === "range") {
    return value;
  }
  throw new ProtocolClientError("invalid_response", "Bridge returned an invalid code review scope.");
}

function codeReviewResultFromRecord(record: Record<string, unknown>): CodeReviewResult {
  const summary = isRecord(record.summary)
    ? {
        verdict: normalizeCodeReviewVerdict(record.summary.verdict),
        overview: requireString(record.summary, "overview"),
        finding_counts: isRecord(record.summary.finding_counts)
          ? Object.fromEntries(
              Object.entries(record.summary.finding_counts)
                .filter((entry): entry is [string, number] => typeof entry[1] === "number")
            )
          : {},
        changed_file_count: nullableNumber(record.summary.changed_file_count) ?? 0,
        reviewed_file_count: nullableNumber(record.summary.reviewed_file_count) ?? 0,
        omitted_file_count: nullableNumber(record.summary.omitted_file_count) ?? 0,
        truncated: record.summary.truncated === true,
        warnings: stringArray(record.summary.warnings)
      }
    : undefined;
  const diff = isRecord(record.diff)
    ? {
        scope: normalizeCodeReviewScope(record.diff.scope),
        changed_files: stringArray(record.diff.changed_files),
        included_files: stringArray(record.diff.included_files),
        omitted_files: Array.isArray(record.diff.omitted_files)
          ? record.diff.omitted_files.filter(isRecord).map((item) => ({
              path: requireString(item, "path"),
              reason: requireString(item, "reason")
            }))
          : [],
        truncated: record.diff.truncated === true,
        warnings: stringArray(record.diff.warnings),
        metadata: isRecord(record.diff.metadata)
          ? Object.fromEntries(
              Object.entries(record.diff.metadata)
                .filter((entry): entry is [string, string] => typeof entry[1] === "string")
            )
          : {},
        byte_count: nullableNumber(record.diff.byte_count) ?? 0
      }
    : undefined;
  return {
    ...record,
    session_id: requireString(record, "session_id"),
    job_id: requireString(record, "job_id"),
    status: requireString(record, "status"),
    scope: record.scope === undefined ? undefined : normalizeCodeReviewScope(record.scope),
    complete: typeof record.complete === "boolean" ? record.complete : undefined,
    cancelled: typeof record.cancelled === "boolean" ? record.cancelled : undefined,
    findings: Array.isArray(record.findings)
      ? record.findings.filter(isRecord).map((finding) => ({
          severity:
            finding.severity === "critical" ||
            finding.severity === "high" ||
            finding.severity === "low"
              ? finding.severity
              : "medium",
          title: requireString(finding, "title"),
          explanation: requireString(finding, "explanation"),
          path: requireString(finding, "path"),
          line_start: nullableNumber(finding.line_start),
          line_end: nullableNumber(finding.line_end),
          evidence: requireString(finding, "evidence"),
          suggested_fix: requireString(finding, "suggested_fix"),
          confidence:
            finding.confidence === "high" || finding.confidence === "low"
              ? finding.confidence
              : "medium"
        }))
      : undefined,
    summary,
    diff
  };
}

function normalizeCodeReviewVerdict(value: unknown): "approve" | "comment" | "request_changes" {
  return value === "approve" || value === "request_changes" ? value : "comment";
}

function structuredTaskLedgerFromRecord(record: Record<string, unknown>): StructuredTaskLedger {
  return {
    session_id: requireString(record, "session_id"),
    target_session_id: nullableString(record.target_session_id) ?? undefined,
    revision: nullableNumber(record.revision) ?? undefined,
    tasks: Array.isArray(record.tasks)
      ? record.tasks.filter(isRecord).map((task) => ({
          task_id: requireString(task, "task_id"),
          title: requireString(task, "title"),
          status: normalizeStructuredTaskStatus(task.status)
        }))
      : undefined,
    updated_at: record.updated_at === null ? null : nullableNumber(record.updated_at),
    updated: typeof record.updated === "boolean" ? record.updated : undefined,
    conflict: typeof record.conflict === "boolean" ? record.conflict : undefined,
    current_revision: nullableNumber(record.current_revision) ?? undefined
  };
}

function normalizeStructuredTaskStatus(value: unknown): StructuredTaskItem["status"] {
  return value === "in_progress" || value === "completed" || value === "blocked"
    ? value
    : "pending";
}

function structuredQuestionSetFromRecord(record: Record<string, unknown>): StructuredQuestionSet {
  return {
    session_id: requireString(record, "session_id"),
    question_set_id: requireString(record, "question_set_id"),
    status: normalizeStructuredQuestionStatus(record.status),
    revision: nullableNumber(record.revision) ?? 0,
    questions: Array.isArray(record.questions)
      ? record.questions.filter(isRecord).map((question) => ({
          question_id: requireString(question, "question_id"),
          prompt: requireString(question, "prompt"),
          options: Array.isArray(question.options)
            ? question.options.filter(isRecord).map((option) => ({
                option_id: requireString(option, "option_id"),
                label: requireString(option, "label"),
                description: requireString(option, "description")
              }))
            : []
        }))
      : [],
    answers: Array.isArray(record.answers)
      ? record.answers.filter(isRecord).map((answer) => ({
          question_id: requireString(answer, "question_id"),
          option_id: requireString(answer, "option_id")
        }))
      : [],
    created_at: nullableNumber(record.created_at) ?? 0,
    updated_at: nullableNumber(record.updated_at) ?? 0,
    expires_at: nullableNumber(record.expires_at) ?? 0,
    terminal_at: record.terminal_at === null ? null : nullableNumber(record.terminal_at),
    resolution_attempts: nullableNumber(record.resolution_attempts) ?? 0,
    created: typeof record.created === "boolean" ? record.created : undefined
  };
}

function normalizeStructuredQuestionStatus(
  value: unknown
): StructuredQuestionSet["status"] {
  return value === "resolving" ||
    value === "answered" ||
    value === "cancelled" ||
    value === "expired"
    ? value
    : "pending";
}

function sessionPermissionGrantFromRecord(
  record: Record<string, unknown>
): SessionPermissionGrant {
  return {
    id: requireString(record, "id"),
    kind: requireString(record, "kind"),
    scope_type: requireString(record, "scope_type"),
    source: requireString(record, "source")
  };
}

function promptQueueItemFromRecord(record: Record<string, unknown>): PromptQueueItem {
  const rawState = requireString(record, "state");
  const state: PromptQueueItem["state"] =
    rawState === "pending" ||
    rawState === "running" ||
    rawState === "completed" ||
    rawState === "cancelled" ||
    rawState === "failed"
      ? rawState
      : "failed";
  return {
    session_id: requireString(record, "session_id"),
    prompt_id: requireString(record, "prompt_id"),
    sequence: typeof record.sequence === "number" ? record.sequence : 0,
    state,
    created_at: requireString(record, "created_at"),
    updated_at: requireString(record, "updated_at"),
    attempts: typeof record.attempts === "number" ? record.attempts : 0,
    terminal_at: nullableString(record.terminal_at),
    error_code: nullableString(record.error_code),
    message_preview: typeof record.message_preview === "string" ? record.message_preview : undefined,
    message_truncated: typeof record.message_truncated === "boolean" ? record.message_truncated : undefined
  };
}

function jobStatusFromRecord(record: Record<string, unknown>): JobStatusResult {
  return {
    job_id: requireString(record, "job_id"),
    session_id: requireString(record, "session_id"),
    kind: requireString(record, "kind") || undefined,
    status: requireString(record, "status"),
    state: requireString(record, "state") || requireString(record, "status"),
    created_at: requireString(record, "created_at") || null,
    started_at: requireString(record, "started_at") || null,
    updated_at: requireString(record, "updated_at") || null,
    completed_at: requireString(record, "completed_at") || null,
    cancellable: typeof record.cancellable === "boolean" ? record.cancellable : undefined,
    cancellation_reason: nullableString(record.cancellation_reason),
    cancellation_requested_at: nullableString(record.cancellation_requested_at),
    last_error: nullableString(record.last_error),
    exit_code: nullableNumber(record.exit_code),
    error: requireString(record, "error") || null,
    plan_id: nullableString(record.plan_id),
    event_count: typeof record.event_count === "number" ? record.event_count : undefined,
    dropped_event_count:
      typeof record.dropped_event_count === "number" ? record.dropped_event_count : undefined
  };
}

function sessionSummaryFromRecord(record: Record<string, unknown>) {
  return {
    session_id: requireString(record, "session_id"),
    workspace_root: requireString(record, "workspace_root"),
    mode: normalizeMode(requireString(record, "mode")),
    closed: typeof record.closed === "boolean" ? record.closed : false,
    active_job: isRecord(record.active_job) ? jobStatusFromRecord(record.active_job) : null,
    last_job: isRecord(record.last_job) ? jobStatusFromRecord(record.last_job) : null
  };
}

function sessionStatusFromRecord(record: Record<string, unknown>): SessionStatusResult {
  return {
    session_id: requireString(record, "session_id"),
    workspace_root: requireString(record, "workspace_root"),
    mode: normalizeMode(requireString(record, "mode")),
    closed: typeof record.closed === "boolean" ? record.closed : false,
    active_job: isRecord(record.active_job) ? jobStatusFromRecord(record.active_job) : null,
    last_job: isRecord(record.last_job) ? jobStatusFromRecord(record.last_job) : null,
    model: requireString(record, "model"),
    base_url: requireString(record, "base_url"),
    temperature: nullableNumber(record.temperature),
    stream: typeof record.stream === "boolean" ? record.stream : false,
    max_steps: typeof record.max_steps === "number" ? record.max_steps : 0,
    no_log: typeof record.no_log === "boolean" ? record.no_log : false,
    yes: typeof record.yes === "boolean" ? record.yes : false,
    subagents_enabled:
      typeof record.subagents_enabled === "boolean" ? record.subagents_enabled : false,
    active_workdir: nullableString(record.active_workdir),
    active_workdir_relpath: nullableString(record.active_workdir_relpath),
    effective_verification_commands: stringArray(record.effective_verification_commands),
    message_count: typeof record.message_count === "number" ? record.message_count : 0,
    pending_images: typeof record.pending_images === "number" ? record.pending_images : 0,
    pending_approvals: typeof record.pending_approvals === "number" ? record.pending_approvals : 0
  };
}

function sessionImagesListFromRecord(record: Record<string, unknown>): SessionImagesListResult {
  return {
    session_id: requireString(record, "session_id"),
    images: sessionImagesFromRecord(record),
    count: typeof record.count === "number" ? record.count : 0,
    max_bytes: typeof record.max_bytes === "number" ? record.max_bytes : 0,
    binary_jsonl: typeof record.binary_jsonl === "boolean" ? record.binary_jsonl : false
  };
}

function sessionImagesFromRecord(record: Record<string, unknown>) {
  return Array.isArray(record.images)
    ? record.images.filter(isRecord).map((image) => ({
        path: requireString(image, "path"),
        relpath: nullableString(image.relpath),
        mime_type: nullableString(image.mime_type),
        size_bytes: nullableNumber(image.size_bytes)
      }))
    : [];
}

function sessionSubagentsStatusFromRecord(record: Record<string, unknown>): SessionSubagentsStatusResult {
  return {
    session_id: requireString(record, "session_id"),
    enabled: typeof record.enabled === "boolean" ? record.enabled : false,
    available: stringArray(record.available),
    available_count: typeof record.available_count === "number" ? record.available_count : 0,
    explicit_execution_supported:
      typeof record.explicit_execution_supported === "boolean"
        ? record.explicit_execution_supported
        : false,
    explicit_execution_policy:
      typeof record.explicit_execution_policy === "string"
        ? record.explicit_execution_policy
        : "",
    lifecycle_event:
      typeof record.lifecycle_event === "string" ? record.lifecycle_event : "",
    execution_lifecycle:
      typeof record.execution_lifecycle === "string" ? record.execution_lifecycle : "",
    cancellation:
      typeof record.cancellation === "string" ? record.cancellation : "",
    independently_resumable: record.independently_resumable === true,
    background_worker_surface:
      typeof record.background_worker_surface === "string"
        ? record.background_worker_surface
        : "",
    forge_execute_policy:
      typeof record.forge_execute_policy === "string" ? record.forge_execute_policy : "",
    secret_values_included:
      typeof record.secret_values_included === "boolean"
        ? record.secret_values_included
        : false,
    changed: typeof record.changed === "boolean" ? record.changed : undefined,
    previous_enabled:
      typeof record.previous_enabled === "boolean" ? record.previous_enabled : undefined,
    audit: isRecord(record.audit) ? record.audit : undefined
  };
}

function traceLevelFromValue(value: unknown): SessionTraceLevel {
  return value === "off" || value === "full" ? value : "compact";
}

function sessionTraceStatusFromRecord(record: Record<string, unknown>): SessionTraceStatusResult {
  return {
    ...record,
    session_id: requireString(record, "session_id"),
    supported: record.supported === true,
    level: traceLevelFromValue(record.level),
    levels: Array.isArray(record.levels)
      ? record.levels.map(traceLevelFromValue).filter((level, index, levels) => levels.indexOf(level) === index)
      : ["compact"],
    retained_events: nullableNumber(record.retained_events) ?? undefined,
    redacted: record.redacted === true,
    secret_values_included: record.secret_values_included === true,
    max_events: typeof record.max_events === "number" ? record.max_events : 0,
    max_bytes: typeof record.max_bytes === "number" ? record.max_bytes : 0,
    full_trace_requires_confirmation: record.full_trace_requires_confirmation === true,
    changed: typeof record.changed === "boolean" ? record.changed : undefined,
    previous_level: typeof record.previous_level === "string" ? traceLevelFromValue(record.previous_level) : undefined,
    full_trace_confirmed:
      typeof record.full_trace_confirmed === "boolean" ? record.full_trace_confirmed : undefined,
    audit: isRecord(record.audit) ? record.audit : undefined
  };
}

function sessionTerminalsListFromRecord(record: Record<string, unknown>): SessionTerminalsListResult {
  return {
    session_id: requireString(record, "session_id"),
    supported: record.supported === true,
    available: record.available === true,
    reason: nullableString(record.reason) ?? undefined,
    terminals: Array.isArray(record.terminals)
      ? record.terminals.filter(isRecord).map((terminal) => ({
          process_id: requireString(terminal, "process_id"),
          cmd: requireString(terminal, "cmd"),
          cwd: requireString(terminal, "cwd"),
          cwd_relpath: nullableString(terminal.cwd_relpath),
          status: requireString(terminal, "status"),
          exit_code: nullableNumber(terminal.exit_code),
          runtime_s: nullableNumber(terminal.runtime_s),
          started_at: nullableString(terminal.started_at)
        }))
      : [],
    count: typeof record.count === "number" ? record.count : 0,
    redacted: record.redacted === true,
    secret_values_included: record.secret_values_included === true,
    arbitrary_shell_execution: record.arbitrary_shell_execution === true,
    interactive_pty_streaming: record.interactive_pty_streaming === true
  };
}

function sessionTerminalShowFromRecord(record: Record<string, unknown>): SessionTerminalShowResult {
  const base = sessionTerminalsListFromRecord(record);
  return {
    ...base,
    process_id: requireString(record, "process_id"),
    status: nullableString(record.status) ?? undefined,
    exit_code: nullableNumber(record.exit_code),
    failure_reason: nullableString(record.failure_reason),
    lines: Array.isArray(record.lines)
      ? record.lines.filter(isRecord).map((line) => ({
          seq: typeof line.seq === "number" ? line.seq : 0,
          stream: requireString(line, "stream"),
          text: requireString(line, "text"),
          ts: nullableString(line.ts)
        }))
      : [],
    line_count: nullableNumber(record.line_count) ?? undefined,
    next_seq: nullableNumber(record.next_seq) ?? undefined,
    dropped_lines: nullableNumber(record.dropped_lines) ?? undefined,
    runtime_s: nullableNumber(record.runtime_s),
    started_at: nullableString(record.started_at),
    total_bytes: nullableNumber(record.total_bytes) ?? undefined,
    bytes: nullableNumber(record.bytes) ?? undefined,
    max_lines: nullableNumber(record.max_lines) ?? undefined,
    max_bytes: nullableNumber(record.max_bytes) ?? undefined,
    truncated: typeof record.truncated === "boolean" ? record.truncated : undefined,
    truncated_by_line_count:
      typeof record.truncated_by_line_count === "boolean"
        ? record.truncated_by_line_count
        : undefined,
    truncated_by_bytes:
      typeof record.truncated_by_bytes === "boolean" ? record.truncated_by_bytes : undefined,
    killed: typeof record.killed === "boolean" ? record.killed : undefined,
    cleared: typeof record.cleared === "boolean" ? record.cleared : undefined
  };
}

function forgePlanFromRecord(record: Record<string, unknown>): ForgePlanResult {
  return {
    plan_id: requireString(record, "plan_id"),
    session_id: requireString(record, "session_id"),
    job_id: requireString(record, "job_id") || null,
    status: requireString(record, "status"),
    source: requireString(record, "source") || "active_memory",
    created_session: typeof record.created_session === "boolean" ? record.created_session : false,
    project_goal: requireString(record, "project_goal"),
    summary: requireString(record, "summary"),
    warnings: stringArray(record.warnings),
    incomplete: typeof record.incomplete === "boolean" ? record.incomplete : false,
    tasks: Array.isArray(record.tasks) ? record.tasks.filter(isRecord).map(forgeTaskFromRecord) : [],
    artifacts: Array.isArray(record.artifacts)
      ? record.artifacts.filter(isRecord).map((artifact) => ({
          kind: requireString(artifact, "kind"),
          artifact_id: requireString(artifact, "artifact_id"),
          path: requireString(artifact, "path")
        }))
      : [],
    plan_artifact_id: requireString(record, "plan_artifact_id"),
    plan_markdown_artifact_id: requireString(record, "plan_markdown_artifact_id"),
    diff_count: nullableNumber(record.diff_count) ?? undefined
  };
}

function forgeShowFromRecord(record: Record<string, unknown>): ForgeShowResult {
  return {
    ...forgePlanFromRecord(record),
    assets: Array.isArray(record.assets)
      ? record.assets.filter(isRecord).map(forgeAssetEntryFromRecord)
      : [],
    legacy_assets: Array.isArray(record.legacy_assets)
      ? record.legacy_assets.filter(isRecord)
      : [],
    artifact_count: typeof record.artifact_count === "number" ? record.artifact_count : 0
  };
}

function forgePlanStateFromRecord(record: Record<string, unknown>): ForgePlanStateResult {
  const assistant = isRecord(record.assistant) ? record.assistant : {};
  return {
    ...forgeShowFromRecord(record),
    ide_revision: typeof record.ide_revision === "number" ? record.ide_revision : 0,
    assistant: {
      instruction: requireString(assistant, "instruction"),
      updated_at: nullableString(assistant.updated_at),
      source: requireString(assistant, "source") || "default"
    },
    goal: requireString(record, "goal") || requireString(record, "project_goal"),
    validation: isRecord(record.validation) ? record.validation : {},
    changed: typeof record.changed === "boolean" ? record.changed : undefined,
    audit: isRecord(record.audit) ? record.audit : undefined,
    task: isRecord(record.task) ? forgeTaskFromRecord(record.task) : undefined
  };
}

function forgeJobProgressFromRecord(record: Record<string, unknown>): ForgeJobProgress {
  return {
    session_id: requireString(record, "session_id"),
    job_id: requireString(record, "job_id"),
    plan_id: typeof record.plan_id === "string" ? record.plan_id : null,
    status: requireString(record, "status"),
    state: requireString(record, "state"),
    complete: typeof record.complete === "boolean" ? record.complete : false,
    cancellable: typeof record.cancellable === "boolean" ? record.cancellable : undefined,
    cancellation_requested:
      typeof record.cancellation_requested === "boolean" ? record.cancellation_requested : undefined,
    cancelled: typeof record.cancelled === "boolean" ? record.cancelled : undefined,
    cancellation_reason:
      typeof record.cancellation_reason === "string" ? record.cancellation_reason : null
  };
}

function forgePlanRegenerateFromRecord(record: Record<string, unknown>): ForgePlanRegenerateResult {
  return {
    ...forgePlanStateFromRecord(record),
    old_revision: typeof record.old_revision === "number" ? record.old_revision : 0,
    new_revision: typeof record.new_revision === "number" ? record.new_revision : 0,
    redacted: typeof record.redacted === "boolean" ? record.redacted : true,
    secret_values_included: typeof record.secret_values_included === "boolean" ? record.secret_values_included : false
  };
}

function forgeReviewFromRecord(record: Record<string, unknown>): ForgeReviewResult {
  return {
    session_id: requireString(record, "session_id"),
    plan_id: requireString(record, "plan_id"),
    task_id: requireString(record, "task_id"),
    approved: typeof record.approved === "boolean" ? record.approved : false,
    confidence: requireString(record, "confidence"),
    summary: requireString(record, "summary"),
    blocking_issues_count:
      typeof record.blocking_issues_count === "number" ? record.blocking_issues_count : 0,
    non_blocking_issues_count:
      typeof record.non_blocking_issues_count === "number" ? record.non_blocking_issues_count : 0,
    review_json: isRecord(record.review_json) ? record.review_json : null,
    review_markdown: requireString(record, "review_markdown"),
    json_artifact_id: requireString(record, "json_artifact_id"),
    markdown_artifact_id: requireString(record, "markdown_artifact_id"),
    requires_human_approval:
      typeof record.requires_human_approval === "boolean" ? record.requires_human_approval : false,
    action: isRecord(record.action) ? record.action : null
  };
}

function forgeAssetMutationFromRecord(record: Record<string, unknown>): ForgeAssetsMutationResult {
  const asset = isRecord(record.asset)
    ? "record" in record.asset && isRecord(record.asset.record)
      ? forgeAssetDetailFromRecord(record.asset)
      : forgeAssetRecordFromRecord(record.asset)
    : forgeEmptyAssetDetail();
  return {
    session_id: requireString(record, "session_id"),
    plan_id: requireString(record, "plan_id"),
    asset,
    status: requireString(record, "status"),
    bound_task_ids: stringArray(record.bound_task_ids),
    comprehension_record: isRecord(record.comprehension_record)
      ? record.comprehension_record
      : null
  };
}

function forgeAssetEntryFromRecord(record: Record<string, unknown>): ForgeAssetEntry {
  return {
    record: isRecord(record.record) ? forgeAssetRecordFromRecord(record.record) : forgeEmptyAssetRecord(),
    comprehension_status: requireString(record, "comprehension_status"),
    comprehension_source: nullableString(record.comprehension_source),
    comprehension_summary_preview: requireString(record, "comprehension_summary_preview"),
    detected_language: nullableString(record.detected_language)
  };
}

function forgeAssetDetailFromRecord(record: Record<string, unknown>): ForgeAssetDetail {
  return {
    record: isRecord(record.record) ? forgeAssetRecordFromRecord(record.record) : forgeEmptyAssetRecord(),
    comprehension_status: requireString(record, "comprehension_status"),
    comprehension: isRecord(record.comprehension) ? record.comprehension : null,
    versions: Array.isArray(record.versions)
      ? record.versions.filter((item): item is number => typeof item === "number")
      : [],
    extracted_text_preview: requireString(record, "extracted_text_preview")
  };
}

function forgeAssetRecordFromRecord(record: Record<string, unknown>): ForgeAssetRecord {
  return {
    id: requireString(record, "id"),
    title: requireString(record, "title"),
    description: requireString(record, "description"),
    kind: requireString(record, "kind"),
    mime: requireString(record, "mime"),
    original_filename: requireString(record, "original_filename"),
    size_bytes: typeof record.size_bytes === "number" ? record.size_bytes : 0,
    sha256: requireString(record, "sha256"),
    stored_path: requireString(record, "stored_path"),
    extracted_text_path: nullableString(record.extracted_text_path),
    thumbnail_path: nullableString(record.thumbnail_path),
    pinned: typeof record.pinned === "boolean" ? record.pinned : false,
    added_at: requireString(record, "added_at"),
    added_by: isRecord(record.added_by) ? record.added_by : {},
    deleted_at: nullableString(record.deleted_at),
    comprehension_status: requireString(record, "comprehension_status"),
    comprehension_current_version: nullableNumber(record.comprehension_current_version)
  };
}

function forgeEmptyAssetDetail(): ForgeAssetDetail {
  return {
    record: forgeEmptyAssetRecord(),
    comprehension_status: "",
    comprehension: null,
    versions: [],
    extracted_text_preview: ""
  };
}

function forgeEmptyAssetRecord(): ForgeAssetRecord {
  return {
    id: "",
    title: "",
    description: "",
    kind: "",
    mime: "",
    original_filename: "",
    size_bytes: 0,
    sha256: "",
    stored_path: "",
    extracted_text_path: null,
    thumbnail_path: null,
    pinned: false,
    added_at: "",
    added_by: {},
    deleted_at: null,
    comprehension_status: "",
    comprehension_current_version: null
  };
}

function stringRecordFromRecord(record: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(record).map(([key, value]) => [key, typeof value === "string" ? value : String(value ?? "")])
  );
}

function forgePlanSummaryFromRecord(record: Record<string, unknown>) {
  return {
    plan_id: requireString(record, "plan_id"),
    session_id: nullableString(record.session_id),
    workspace_root: requireString(record, "workspace_root"),
    status: requireString(record, "status"),
    source: requireString(record, "source") || "persisted",
    project_goal: requireString(record, "project_goal"),
    summary: requireString(record, "summary"),
    task_count: typeof record.task_count === "number" ? record.task_count : 0,
    created_at: requireString(record, "created_at"),
    updated_at: requireString(record, "updated_at"),
    plan_artifact_id: requireString(record, "plan_artifact_id"),
    plan_markdown_artifact_id: requireString(record, "plan_markdown_artifact_id")
  };
}

function forgeTaskFromRecord(record: Record<string, unknown>) {
  const fileScope = isRecord(record.file_scope) ? record.file_scope : {};
  return {
    task_id: requireString(record, "task_id"),
    title: requireString(record, "title"),
    objective: requireString(record, "objective"),
    file_scope: {
      estimated_files: stringArray(fileScope.estimated_files),
      write_scope: stringArray(fileScope.write_scope)
    },
    acceptance_criteria: stringArray(record.acceptance_criteria),
    verification_commands: stringArray(record.verification_commands),
    risk_notes: stringArray(record.risk_notes),
    dependencies: stringArray(record.dependencies),
    order: nullableNumber(record.order),
    scope_unknown_reason: requireString(record, "scope_unknown_reason"),
    warnings: stringArray(record.warnings),
    status: requireString(record, "status")
  };
}

function forgeExecutePreviewFromRecord(record: Record<string, unknown>): ForgeExecutePreviewResult {
  return {
    session_id: requireString(record, "session_id"),
    plan_id: requireString(record, "plan_id"),
    selected_task_ids: stringArray(record.selected_task_ids),
    execution_mode_requested: normalizeMode(requireString(record, "execution_mode_requested")),
    workspace_trust_required:
      typeof record.workspace_trust_required === "boolean" ? record.workspace_trust_required : true,
    workspace_trusted: typeof record.workspace_trusted === "boolean" ? record.workspace_trusted : null,
    estimated_file_scopes: Array.isArray(record.estimated_file_scopes)
      ? record.estimated_file_scopes.filter(isRecord).map((scope) => ({
          task_id: requireString(scope, "task_id"),
          title: requireString(scope, "title"),
          estimated_files: stringArray(scope.estimated_files),
          write_scope: stringArray(scope.write_scope),
          scope_unknown_reason: requireString(scope, "scope_unknown_reason")
        }))
      : [],
    verification_commands: Array.isArray(record.verification_commands)
      ? record.verification_commands.filter(isRecord).map((verification) => ({
          task_id: requireString(verification, "task_id"),
          commands: stringArray(verification.commands),
          source: requireString(verification, "source"),
          missing_reason: requireString(verification, "missing_reason")
        }))
      : [],
    required_approvals: Array.isArray(record.required_approvals)
      ? record.required_approvals.filter(isRecord).map((approval) => ({
          kind: requireString(approval, "kind"),
          task_id: requireString(approval, "task_id"),
          reason: requireString(approval, "reason"),
          scope: isRecord(approval.scope) ? approval.scope : null,
          allow_for_session_scope: isRecord(approval.allow_for_session_scope)
            ? approval.allow_for_session_scope
            : null,
          allow_for_session_supported:
            typeof approval.allow_for_session_supported === "boolean"
              ? approval.allow_for_session_supported
              : false
        }))
      : [],
    runtime_approval_requirements: Array.isArray(record.runtime_approval_requirements)
      ? record.runtime_approval_requirements.filter(isRecord).map((approval) => ({
          kind: requireString(approval, "kind"),
          reason: requireString(approval, "reason"),
          scope_requirement: isRecord(approval.scope_requirement)
            ? approval.scope_requirement
            : null,
          allow_for_session_supported:
            typeof approval.allow_for_session_supported === "boolean"
              ? approval.allow_for_session_supported
              : false,
          warning: nullableString(approval.warning)
        }))
      : [],
    approval_scopes_safe:
      typeof record.approval_scopes_safe === "boolean" ? record.approval_scopes_safe : false,
    sandbox_profile: isRecord(record.sandbox_profile)
      ? {
          requested: requireString(record.sandbox_profile, "requested"),
          supported:
            typeof record.sandbox_profile.supported === "boolean"
              ? record.sandbox_profile.supported
              : typeof record.sandbox_profile.available === "boolean"
                ? record.sandbox_profile.available
                : false,
          available:
            typeof record.sandbox_profile.available === "boolean"
              ? record.sandbox_profile.available
              : false,
          diagnostic: nullableString(record.sandbox_profile.diagnostic)
        }
      : { requested: "", supported: false, available: false, diagnostic: null },
    known_risks: stringArray(record.known_risks),
    missing_prerequisites: stringArray(record.missing_prerequisites),
    preview_ready: typeof record.preview_ready === "boolean" ? record.preview_ready : false,
    real_execution_supported:
      typeof record.real_execution_supported === "boolean" ? record.real_execution_supported : false,
    unsupported_reason: requireString(record, "unsupported_reason"),
    active_cancellation_supported:
      typeof record.active_cancellation_supported === "boolean"
        ? record.active_cancellation_supported
        : false,
    cancellation: isRecord(record.cancellation)
      ? {
          supported:
            typeof record.cancellation.supported === "boolean"
              ? record.cancellation.supported
              : false,
          kind: typeof record.cancellation.kind === "string" ? record.cancellation.kind : "",
          hard_interrupt:
            typeof record.cancellation.hard_interrupt === "boolean"
              ? record.cancellation.hard_interrupt
              : false
        }
      : undefined,
    max_steps: typeof record.max_steps === "number" ? record.max_steps : null,
    no_log: typeof record.no_log === "boolean" ? record.no_log : false,
    subagents_supported:
      typeof record.subagents_supported === "boolean" ? record.subagents_supported : false,
    subagents_enabled:
      typeof record.subagents_enabled === "boolean" ? record.subagents_enabled : false,
    subagents_policy: typeof record.subagents_policy === "string" ? record.subagents_policy : "",
    next_recommended_action: requireString(record, "next_recommended_action"),
    status: requireString(record, "status")
  };
}

function diffSummaryFromRecord(record: Record<string, unknown>) {
  return {
    diff_id: requireString(record, "diff_id"),
    session_id: requireString(record, "session_id"),
    plan_id: requireString(record, "plan_id"),
    job_id: requireString(record, "job_id") || null,
    file_path: requireString(record, "file_path"),
    status: requireString(record, "status"),
    old_label: requireString(record, "old_label"),
    new_label: requireString(record, "new_label"),
    size_bytes: typeof record.size_bytes === "number" ? record.size_bytes : 0
  };
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function createBridgeLaunch(
  config: AlysisConfig,
  options: BridgeStartOptions
): { env: NodeJS.ProcessEnv; profile: BridgeLaunchProfile; credentialFingerprint: string | null } {
  const env = bridgeEnvironment(config, options);
  return {
    env,
    profile: createLaunchProfile(config, options, env),
    credentialFingerprint: credentialFingerprint(env, options)
  };
}

function createLaunchProfile(
  config: AlysisConfig,
  options: BridgeStartOptions,
  env?: NodeJS.ProcessEnv
): BridgeLaunchProfile {
  const effectiveEnv = env ?? bridgeEnvironment(config, options);
  const apiKeyForwardingAllowed = canForwardApiKey(config);
  const stripApiKey = options.stripApiKey === true;
  const alysisApiKeyForwarded =
    typeof effectiveEnv.ALYSIS_API_KEY === "string" &&
    effectiveEnv.ALYSIS_API_KEY.trim().length > 0;
  return {
    resolvedExecutablePath: resolveCliExecutionPath(config),
    apiKeyForwardingAllowed,
    stripApiKey,
    alysisApiKeyForwarded,
    credentialCapable: apiKeyForwardingAllowed && !stripApiKey,
    model: config.defaultModel,
    baseUrl: config.baseUrl,
    workspaceTrusted:
      typeof config.security?.isWorkspaceTrusted === "boolean"
        ? config.security.isWorkspaceTrusted
        : null,
    executableTrusted:
      typeof config.security?.cliPath.trusted === "boolean"
        ? config.security.cliPath.trusted
        : null,
    executableOrigin: config.security?.cliPath.source ?? "unknown"
  };
}

function assertLaunchProfileSupportsRequirement(
  profile: BridgeLaunchProfile,
  requiredProfile: BridgeRequiredProfile
): void {
  if (!requiredProfile.credentialsRequired) {
    return;
  }
  if (!profile.credentialCapable) {
    throw new ProtocolClientError(
      "bridge_credentials_unavailable",
      "Alysis Code bridge credentials cannot be forwarded for the resolved executable origin."
    );
  }
}

function launchProfileSatisfies(
  current: BridgeLaunchProfile,
  requested: BridgeLaunchProfile,
  requiredProfile: BridgeRequiredProfile,
  currentCredentialFingerprint: string | null,
  requestedCredentialFingerprint: string | null
): boolean {
  if (current.resolvedExecutablePath !== requested.resolvedExecutablePath) {
    return false;
  }
  if (!requiredProfile.credentialsRequired) {
    return true;
  }
  if (!current.credentialCapable) {
    return false;
  }
  if (currentCredentialFingerprint !== requestedCredentialFingerprint) {
    return false;
  }
  return true;
}

// Fingerprints every credential this launch would forward, not just ALYSIS_API_KEY: storing a
// new provider key (e.g. ANTHROPIC_API_KEY) changes what the CLI can authenticate with, so a
// running bridge launched without it must be restarted rather than silently reused. Reads the
// values back off the built env so anything the forwarding gate stripped is excluded.
function credentialFingerprint(
  env: NodeJS.ProcessEnv,
  options: BridgeStartOptions
): string | null {
  const names = new Set<string>(["ALYSIS_API_KEY"]);
  for (const credential of options.providerCredentials ?? []) {
    names.add(credential.envVar.trim().toUpperCase());
    const profileEnv = ideProfileKeyEnvironmentName(credential.profile);
    if (profileEnv) {
      names.add(profileEnv);
    }
  }
  const parts: string[] = [];
  for (const name of [...names].sort()) {
    const value = env[name]?.trim();
    if (value) {
      parts.push(`${name}=${value}`);
    }
  }
  if (parts.length === 0) {
    return null;
  }
  return createHash("sha256").update(parts.join("\u0000"), "utf8").digest("hex");
}

function approvalKey(sessionId: string, approvalId: string): string {
  return `${sessionId}\u0000${approvalId}`;
}

function bridgeEnvironment(config: AlysisConfig, options: BridgeStartOptions): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env };
  // These overrides belong to this extension's SecretStorage, never an inherited IDE process.
  for (const name of Object.keys(env)) {
    if (/^ALYSIS_IDE_PROFILE_.*_API_KEY$/i.test(name)) {
      delete env[name];
    }
  }
  applyUtf8Io(env);
  // Proxy / CA plumbing and the parent PID are not credentials: they apply on every launch,
  // including credential-stripped ones, so a diagnostics run behind a corporate proxy still works.
  applyHostNetworkEnvironment(env, config.hostNetwork);
  applyParentProcessEnvironment(env);
  if (options.stripApiKey) {
    deleteCredentialEnvironmentVariables(env);
    return env;
  }
  if (!canForwardApiKey(config)) {
    deleteCredentialEnvironmentVariables(env);
    return env;
  }
  if (options.apiKey && options.apiKey.trim().length > 0) {
    env.ALYSIS_API_KEY = options.apiKey.trim();
  }
  // Provider-scoped keys are exported last so they win over any inherited value of the same name.
  // Names are re-validated here (not only at storage time) because this string is written straight
  // into the spawned process environment.
  for (const credential of options.providerCredentials ?? []) {
    const envVar = credential.envVar.trim().toUpperCase();
    const value = credential.value.trim();
    if (value.length === 0) {
      continue;
    }
    if (/^[A-Z_][A-Z0-9_]{0,63}$/.test(envVar)) {
      env[envVar] = value;
    }
    // Custom and authenticated local endpoints need not declare a vendor env alias.
    // Their key still belongs exclusively to the selected profile, never the global key.
    const profileEnv = ideProfileKeyEnvironmentName(credential.profile);
    if (profileEnv) {
      env[profileEnv] = value;
    }
  }
  return env;
}

function ideProfileKeyEnvironmentName(profile?: string): string | undefined {
  if (!profile || !isStorableProfileName(profile)) {
    return undefined;
  }
  // Hex preserves case and punctuation without collisions on case-insensitive Windows env names.
  // The API_KEY suffix also keeps existing credential stripping and log redaction effective.
  return `ALYSIS_IDE_PROFILE_${Buffer.from(profile.trim(), "utf8").toString("hex").toUpperCase()}_API_KEY`;
}

// Force the spawned Python CLI to use UTF-8 for stdio regardless of the host's locale code page.
// On Windows with a non-UTF-8 locale (e.g. Greek cp1253), Python defaults stdout/stderr to the
// legacy code page; the moment the agent streams a Unicode glyph (box-drawing │, status marks, etc.)
// it raises UnicodeEncodeError and the job dies mid-run. PYTHONUTF8=1 (PEP 540 UTF-8 mode) plus an
// explicit PYTHONIOENCODING make the CLI encode I/O as UTF-8 everywhere. No-op on already-UTF-8 hosts.
function applyUtf8Io(env: NodeJS.ProcessEnv): void {
  env.PYTHONUTF8 = "1";
  env.PYTHONIOENCODING = "utf-8";
}

function onceProcessClosed(child: BridgeProcess): Promise<void> {
  return new Promise((resolve) => {
    let settled = false;
    const done = () => {
      if (settled) {
        return;
      }
      settled = true;
      child.off("exit", done);
      child.off("close", done);
      resolve();
    };
    if (hasProcessExited(child)) {
      done();
      return;
    }
    // ChildProcess `error` means an operation failed; it does not prove the OS process exited.
    // Teardown/start ordering therefore fences exclusively on the terminal lifecycle events.
    child.once("exit", done);
    child.once("close", done);
    // Close the inspection/listener race: the child may have exited immediately after the first
    // check but before both terminal listeners were attached.
    if (hasProcessExited(child)) {
      done();
    }
  });
}

function hasProcessExited(child: BridgeProcess): boolean {
  return (child.exitCode !== undefined && child.exitCode !== null)
    || (child.signalCode !== undefined && child.signalCode !== null);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function protocolStatus(result: Record<string, unknown>): string | undefined {
  const status = typeof result.status === "string" ? result.status : undefined;
  if (status) {
    return status;
  }
  return typeof result.state === "string" ? result.state : undefined;
}

function protocolErrorMessage(error: unknown): string {
  if (error instanceof ProtocolClientError) {
    return `${error.message} (${error.code})`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

async function withTimeout(promise: Promise<void>, timeoutMs: number): Promise<boolean> {
  let timeout: NodeJS.Timeout | undefined;
  const result = await Promise.race([
    promise.then(() => true),
    new Promise<boolean>((resolve) => {
      timeout = setTimeout(() => resolve(false), timeoutMs);
    })
  ]);
  if (timeout) {
    clearTimeout(timeout);
  }
  return result;
}
