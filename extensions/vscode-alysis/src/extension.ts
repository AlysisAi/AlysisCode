import { spawn } from "node:child_process";
import { createHash } from "node:crypto";

import * as vscode from "vscode";
import { activeWorkspaceFolder, activeWorkspaceRoot } from "./workspace/activeWorkspace";
import { searchWorkspaceMentions } from "./workspace/workspaceMentions";

import {
  CliDiscovery,
  CliRunner,
  SafeConfigNotice,
  AlysisConfig,
  defaultCliRunner,
  getAlysisConfig,
  redactForDisplay
} from "./client/CliDiscovery";
import { HostEnvironmentNotice, HostNetworkSettings } from "./client/hostEnvironment";
import { pythonScriptDirsFromInterpreter } from "./client/cliLocator";
import { BoundedBridgeReconnect } from "./client/bridgeReconnect";
import {
  BridgeProcess,
  BridgeProcessFactory,
  BridgeStartOptions,
  AlysisBridgeClient
} from "./client/AlysisBridgeClient";
import { PROTOCOL_VERSION, type AlysisMode } from "./client/AlysisProtocol";
import { featureDisabledReason } from "./client/compatibility";
import { ActionResultStore } from "./backend/ActionResultStore";
import { BackendActionController } from "./backend/BackendActionController";
import { ProfileSnapshotStore } from "./backend/ProfileSnapshotStore";
import {
  BrowserCockpitController,
  browserCockpitSummary
} from "./browser/BrowserCockpitController";
import { BrowserPreviewStore } from "./browser/BrowserPreviewStore";
import { ChatController } from "./chat/ChatController";
import { BUNDLED_RUNTIME_MISSING_CODE } from "./chat/ChatErrorTaxonomy";
import { emptySessionModelState } from "./chat/CockpitState";
import { CockpitRuntimeState } from "./chat/CockpitRuntimeState";
import { registerAlysisChatParticipant } from "./chatParticipant/AlysisChatParticipant";
import { registerCancelCurrentRunCommand } from "./commands/cancelCurrentRun";
import { registerCliSetupCommands } from "./commands/cliSetup";
import { registerConfigureProviderCommand } from "./commands/configureProvider";
import { registerPickModelCommand, runModelPicker, type ModelPickerDeps } from "./commands/pickModel";
import { registerForgeExecuteCommand } from "./commands/forgeExecute";
import { registerForgePlanCommand } from "./commands/forgePlan";
import { registerRunSwarmCommand } from "./commands/runSwarm";
import { registerLocateCliCommand } from "./commands/locateCli";
import { registerOpenChatCommands } from "./commands/openChat";
import { registerManageCommands } from "./commands/manageCommands";
import { COMMANDS } from "./commands/registry";
import { IdeContextCollector } from "./context/IdeContextCollector";
import { createVsCodeContextSource } from "./context/VsCodeContextSource";
import { CodeReviewPresenter } from "./review/CodeReviewPresenter";
import { registerReviewCommands } from "./commands/reviewCommands";
import { registerRunDoctorCommand, registerShowBridgeHealthCommand } from "./commands/runDoctor";
import { registerSessionCommands } from "./commands/sessionCommands";
import { ForgeDiffContentProvider } from "./forge/ForgeDiffContentProvider";
import { ForgeController } from "./forge/ForgeController";
import { AlysisSecretStore } from "./secrets/secretStorage";
import { evaluateProcessExecutionCommand } from "./security/commandGuards";
import { SlashCommandRouter } from "./slash/SlashCommandRouter";
import { availableSlashCommands } from "./slash/SlashCommandRegistry";
import { routeSlashModeChange } from "./slash/modeRouting";
import { AlysisStatusBar } from "./status/statusBar";
import { ArtifactsViewProvider } from "./views/artifactsView";
import { ForgePlanViewProvider } from "./views/forgePlanView";
import { ManageViewProvider } from "./views/manageView";
import { SessionsViewProvider } from "./views/sessionsView";
import { StartViewProvider } from "./views/StartViewProvider";
import { WorktreeController } from "./workspace/WorktreeController";
import { consumeWorktreeHandoff } from "./workspace/WorktreeHandoff";
import { ProviderCatalogController, providerSelectionReadiness } from "./providers/ProviderCatalogController";
import { consumeInstalledProductionQaFlag } from "./qa/InstalledProductionQa";
import { ManagedCliRuntime, type InstalledRuntime } from "./runtime/ManagedCliRuntime";
import { installBundledManagedCli } from "./runtime/BundledManagedCli";
import { createManagedCliReleaseSignatureVerifier } from "./runtime/ManagedCliReleaseSecurity";
import {
  ManagedRuntimeCoordinator,
  ManagedRuntimeStore
} from "./runtime/ManagedRuntimeCoordinator";
import { DiagnosticsVerificationGate } from "./verification/DiagnosticsVerificationGate";
import { createVsCodeDiagnosticsSource } from "./verification/VsCodeDiagnosticsSource";
import { HostActionController } from "./hostActions/HostActionController";
import { VsCodeTasksHostAdapter, createVsCodeTasksApi } from "./hostActions/VsCodeTasksHostAdapter";
import { VsCodeDebugHostAdapter, createVsCodeDebugApi } from "./hostActions/VsCodeDebugHostAdapter";
import { migrateLegacySettings } from "./migration/legacySettings";

let activeChatController: ChatController | undefined;
let activeForgeController: ForgeController | undefined;
let activeBridgeClient: AlysisBridgeClient | undefined;
let activeBrowserController: BrowserCockpitController | undefined;
let activeHostActionController: HostActionController | undefined;

export interface AlysisExtensionApi {
  test?: {
    /** Resolves once background runtime validation and the initial bridge check have settled. */
    whenReady(): Promise<void>;
    state(): {
      chat: ReturnType<ChatController["testState"]> | null;
      forge: ReturnType<ForgeController["testState"]> | null;
      browser: ReturnType<BrowserCockpitController["state"]> | null;
      statusBar: ReturnType<AlysisStatusBar["snapshot"]> | null;
    };
    submitChatText(text: string): Promise<void>;
    startTask(text: string, mode?: AlysisMode): Promise<boolean>;
    hasConfiguredApiKey(): Promise<boolean>;
    setConfiguredApiKey(value: string): Promise<void>;
    clearConfiguredApiKey(): Promise<void>;
    respondForgeApproval(sessionId: string, approvalId: string, decision: "allow_once" | "allow_for_session" | "deny"): Promise<void>;
    browserStart(): Promise<void>;
    browserStartLocal(): Promise<void>;
    browserNavigate(url: string): Promise<void>;
    browserSnapshot(kind?: "semantic" | "accessibility" | "dom" | "text"): Promise<void>;
    browserScreenshot(fullPage?: boolean): Promise<void>;
    browserDiagnostics(): Promise<void>;
    browserClose(): Promise<void>;
    secretStorageRoundTrip(): Promise<{ stored: boolean; cleared: boolean }>;
    hostActions(): ReturnType<HostActionController["snapshot"]>;
  };
  /**
   * Non-command QA seam for an installed VSIX. It exists only in ExtensionMode.Production when
   * the one-shot runner flag was consumed at activation, and it never returns response text.
   */
  installedProductionQa?: {
    hasProviderSecret(): Promise<boolean>;
    storeProviderSecret(value: string): Promise<void>;
    clearProviderSecret(): Promise<void>;
    runReadonlyChat(prompt: string, expectedMarker: string): Promise<{
      responseLength: number;
      responseSha256: string;
      markerMatched: boolean;
      elapsedMs: number;
    }>;
  };
}

export async function activate(context: vscode.ExtensionContext): Promise<AlysisExtensionApi | undefined> {
  const installedProductionQaEnabled = consumeInstalledProductionQaFlag(
    context.extensionMode === vscode.ExtensionMode.Production
  );
  const output = vscode.window.createOutputChannel("Alysis Code");

  // Runs before anything reads configuration, so a user upgrading from the
  // Sylliptor-named extension keeps their cliPath and model settings.
  try {
    const migration = await migrateLegacySettings(context);
    if (migration.migrated.length > 0) {
      output.appendLine(
        `Migrated settings from the previous extension name: ${migration.migrated.join(", ")}`
      );
    }
  } catch (error) {
    // Never block activation on the migration; the settings UI is still there.
    output.appendLine(`Could not migrate legacy settings: ${String(error)}`);
  }

  const statusBar = new AlysisStatusBar();
  const runtime = new CockpitRuntimeState();
  const extensionHostTestMode = context.extensionMode === vscode.ExtensionMode.Test;
  const cliDiscovery = new CliDiscovery(extensionHostTestMode ? extensionHostTestCliRunner : undefined);
  const bridgeClient = new AlysisBridgeClient(
    cliDiscovery,
    extensionHostTestMode ? extensionHostTestBridgeProcessFactory : undefined
  );
  activeBridgeClient = bridgeClient;
  const hostActionController = new HostActionController({
    bridge: bridgeClient,
    adapters: [
      new VsCodeTasksHostAdapter(createVsCodeTasksApi(vscode)),
      new VsCodeDebugHostAdapter(createVsCodeDebugApi(vscode))
    ],
    isWorkspaceTrusted: () => vscode.workspace.isTrusted,
    workspaceIdentities: () => (vscode.workspace.workspaceFolders ?? []).map((folder) => ({
      root: folder.uri.fsPath,
      scheme: folder.uri.scheme,
      authority: folder.uri.authority,
      name: folder.name
    })),
    report: (message) => output.appendLine(message)
  });
  activeHostActionController = hostActionController;
  bridgeClient.setHostCapabilityProvider(() => hostActionController.advertisement());
  const secrets = new AlysisSecretStore(context.secrets);
  const sessionsView = new SessionsViewProvider({
    get: () => context.workspaceState.get<Record<string, string>>("alysis.sessionTitles"),
    update: (titles) => context.workspaceState.update("alysis.sessionTitles", titles),
    getSessions: () => context.workspaceState.get("alysis.sessionHistory"),
    updateSessions: (sessions) => context.workspaceState.update("alysis.sessionHistory", sessions)
  });
  const forgePlanView = new ForgePlanViewProvider();
  const artifactsView = new ArtifactsViewProvider();
  const diffContentProvider = new ForgeDiffContentProvider();
  const reportedConfigNotices = new Set<string>();
  const readBaseConfig = (): AlysisConfig =>
    getAlysisConfig(vscode.workspace.getConfiguration("alysis"), {
      isWorkspaceTrusted: vscode.workspace.isTrusted,
      workspaceRoots: (vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath),
      onNotice: (notice) => reportConfigNotice(output, reportedConfigNotices, notice),
      hostNetwork: readHostNetworkSettings(),
      onHostNetworkNotice: (notice) => reportHostNetworkNotice(output, reportedConfigNotices, notice)
    });
  const managedRuntime = createExtensionManagedRuntime(context, output);
  // Validate the current immutable pointer before any health probe or long-lived bridge is allowed to
  // spawn. Extension Development/Test hosts retain an explicitly-labelled PATH escape hatch; packaged
  // production builds fail with an actionable message when no managed artifact exists.
  //
  // Validation is full-file SHA-256 + signature verification + child probes, so it never runs on the
  // activation critical path. Until it settles the config stays fail-closed with an explicit
  // "validating" reason, and the first bridge spawn waits for the promise below.
  let runtimeValidationSettled = false;
  const runtimeValidation = managedRuntime
    .refresh(readBaseConfig())
    .catch((error: unknown) => {
      output.appendLine(
        `Managed runtime: validation did not complete: ${redactForDisplay(errorText(error))}`
      );
    })
    .then(() => {
      runtimeValidationSettled = true;
    });
  const getConfig = (): AlysisConfig =>
    runtimeValidationSettled
      ? managedRuntime.apply(readBaseConfig())
      : runtimeValidatingConfig(managedRuntime.apply(readBaseConfig()));
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.runtimeEvidence", async () => {
      // Activation returns before runtime validation finishes. Report the settled runtime identity.
      await runtimeValidation;
      const baseConfig = readBaseConfig();
      const config = managedRuntime.apply(baseConfig);
      const evidence = managedRuntime.evidence();
      const guard = evaluateProcessExecutionCommand(config, "runtimeEvidence");
      if (!guard.allowed || !evidence.production) {
        return {
          ...evidence,
          extensionMode: context.extensionMode === vscode.ExtensionMode.Production ? "production" : "development",
          cliPathOverride: baseConfig.cliPath,
          health: {
            status: "blocked",
            reason: redactForDisplay(guard.reason ?? evidence.message ?? "Managed runtime is unavailable.")
          }
        };
      }
      const detection = await cliDiscovery.detect(config);
      return {
        ...evidence,
        extensionMode: context.extensionMode === vscode.ExtensionMode.Production ? "production" : "development",
        cliPathOverride: baseConfig.cliPath,
        health: detection.ok
          ? {
              status: "passed",
              protocolVersion: detection.health.protocol_version,
              alysisVersion: detection.health.alysis_version
            }
          : {
              status: "failed",
              code: detection.code,
              reason: redactForDisplay(detection.message)
            }
      };
    })
  );
  const ideContextSource = createVsCodeContextSource(vscode);
  const ideContextCollector = new IdeContextCollector(ideContextSource);
  const diagnosticsVerification = new DiagnosticsVerificationGate(
    createVsCodeDiagnosticsSource(vscode)
  );
  const handoffRoot = activeWorkspaceRoot();
  const worktreeHandoff = handoffRoot ? await consumeWorktreeHandoff(context.globalState, context.workspaceState, handoffRoot) : undefined;
  const chatController = new ChatController(
    bridgeClient,
    getConfig,
    secrets,
    statusBar,
    sessionsView,
    output,
    runtime,
    {
      get: () => context.workspaceState.get("alysis.activeSessionId"),
      update: (reference) => context.workspaceState.update("alysis.activeSessionId", reference)
    },
    ideContextCollector,
    diagnosticsVerification
  );
  activeChatController = chatController;
  let startView: StartViewProvider | undefined;
  const browserPreviewRoot = vscode.Uri.joinPath(context.globalStorageUri, "private", "browser-previews");
  const browserPreviewStore = new BrowserPreviewStore(browserPreviewRoot.fsPath);
  // Recursive delete of last session's ephemeral previews is background work: the store serializes
  // its own operations, so a screenshot taken during cleanup still lands after the directory is gone.
  void browserPreviewStore.clear().catch((error: unknown) => {
    output.appendLine(`Managed browser preview cleanup skipped: ${redactForDisplay(errorText(error))}`);
  });
  const browserController = new BrowserCockpitController({
    bridge: bridgeClient,
    previewStore: browserPreviewStore,
    ensureOwnerSession: () => chatController.ensureLiveSession(),
    isWorkspaceTrusted: () => vscode.workspace.isTrusted,
    compatibility: () => {
      const compatibility = runtime.snapshot().compatibility;
      const feature = compatibility.features.managedBrowser;
      const localFeature = compatibility.features.managedBrowserDirectLoopback;
      const supported = compatibility.protocol.compatible && feature.supported;
      const localTestingSupported = compatibility.protocol.compatible && localFeature.supported;
      return {
        supported,
        reason: supported ? null : featureDisabledReason(compatibility, "managedBrowser"),
        localTestingSupported,
        localTestingReason: localTestingSupported
          ? null
          : featureDisabledReason(compatibility, "managedBrowserDirectLoopback")
      };
    },
    confirmLocalStart: async () => {
      if (extensionHostTestMode) {
        return true;
      }
      const answer = await vscode.window.showWarningMessage(
        "Start a Direct IDE browser that can reach public sites and loopback development servers? Alysis Code agents cannot access this browser. LAN and link-local destinations remain blocked.",
        { modal: true },
        "Start local testing"
      );
      return answer === "Start local testing";
    },
    confirmClose: async (browser) => {
      if (extensionHostTestMode) {
        return true;
      }
      const answer = await vscode.window.showWarningMessage(
        `Close ${browser.product || "managed browser"} and permanently delete its ephemeral screenshots?`,
        { modal: true },
        "Close and delete"
      );
      return answer === "Close and delete";
    },
    onChange: () => {
      chatController.refreshCockpit();
      startView?.updateBrowser();
    },
    report: (message) => output.appendLine(message)
  });
  activeBrowserController = browserController;
  const forgeController = new ForgeController(
    bridgeClient,
    getConfig,
    secrets,
    statusBar,
    sessionsView,
    forgePlanView,
    artifactsView,
    output,
    diffContentProvider,
    runtime,
    {
      get: () => context.workspaceState.get("alysis.activeForgeContext"),
      update: (value) => context.workspaceState.update("alysis.activeForgeContext", value)
    }
  );
  activeForgeController = forgeController;
  const actionResultStore = new ActionResultStore(() => chatController.refreshCockpit());
  const codeReviewPresenter = new CodeReviewPresenter(
    bridgeClient,
    ideContextSource,
    output
  );
  context.subscriptions.push(codeReviewPresenter);
  const profileStore = new ProfileSnapshotStore(() => {
    chatController.refreshCockpit();
    startView?.update();
  });
  const backendActions = new BackendActionController({
    bridge: bridgeClient,
    getConfig,
    output,
    isWorkspaceTrusted: () => vscode.workspace.isTrusted,
    activeContext: () => ({
      sessionId: chatController.activeSessionId(),
      workspaceRoot: chatController.activeSessionWorkspaceRoot(),
      forgePlan: forgeController.activePlanContext()
    }),
    ensureLiveSession: () => chatController.ensureLiveSession(),
    resumeSession: (sessionId, targetSessionId) => chatController.resumeRetainedContext(sessionId, targetSessionId),
    ensureCredentialBridge: (config) => ensureCredentialBridge(bridgeClient, secrets, config, output),
    useProfile: async name => {
      await providerCatalog.useConnection(name);
      const state = providerCatalog.snapshot();
      if (state.error) throw new Error(state.error);
      return state.activeProfile === name
        ? { active_profile: name, changed: true }
        : { cancelled: true };
    },
    actionResults: actionResultStore,
    codeReviewPresenter,
    attachContextBlock: (block) => chatController.attachContextBlock(block, "past task"),
    remoteName: () => vscode.env.remoteName
  });
  // Assigned once the provider catalog exists further down. The slash router is built first, so
  // its handlers close over this binding rather than the controller itself; a slash command that
  // somehow fires before activation finishes gets an honest "not ready yet" instead of a crash.
  let openModelPicker: ((model?: string) => Promise<string>) | undefined;

  const slashRouter = new SlashCommandRouter({
    isWorkspaceTrusted: () => vscode.workspace.isTrusted,
    pickPermissions: async () => {
      const options = [
        { label: "Review changes", description: "Ask before writes and important actions", mode: "review" as const },
        { label: "Auto-approve", description: "Allow actions within the agent's safeguards", mode: "auto" as const },
        { label: "Read-only", description: "Inspect and explain without changing files", mode: "readonly" as const }
      ].filter((option) => vscode.workspace.isTrusted || option.mode === "readonly");
      return (await vscode.window.showQuickPick(options, { title: "Alysis Code Permissions", placeHolder: "Choose what the agent may do" }))?.mode;
    },
    setMode: (mode) => chatController.changePermissions(mode, () => routeSlashModeChange(mode, {
      activeSessionId: () => chatController.activeSessionId(),
      executeBackendAction: (actionId, args) => backendActions.executeSlashAction(actionId, args),
      updateDefaultMode: (nextMode) =>
        Promise.resolve(
          vscode.workspace.getConfiguration("alysis").update(
            "defaultMode",
            nextMode,
            vscode.ConfigurationTarget.Global
          )
        )
    })),
    persona: (name) => chatController.routeSlashPersona(name),
    plan: async (instruction) => {
      await forgeController.planWithInstruction(instruction);
    },
    executePlan: () => forgeController.execute(),
    executePreview: () => forgeController.executePreview(),
    listPlans: () => forgeController.listPlans(),
    openPlan: (planId) => forgeController.openPlanById(planId),
    openDiffs: () => forgeController.openDiff(),
    refreshArtifacts: () => forgeController.refreshArtifacts(),
    cancel: async () => {
      await vscode.commands.executeCommand("alysis.cancelCurrentRun");
    },
    doctor: async () => {
      await vscode.commands.executeCommand("alysis.runDoctor");
    },
    config: async () => {
      if (!openModelPicker) {
        await vscode.commands.executeCommand("alysis.configureProvider");
        return;
      }
      return openModelPicker();
    },
    pickModel: async (model) => {
      if (!openModelPicker) {
        return "The model picker is still starting up. Try again in a moment.";
      }
      return openModelPicker(model);
    },
    backendAction: (actionId, args) => backendActions.executeSlashAction(actionId, args),
    commandCatalog: () => {
      const state = chatController.primaryCockpitState();
      const forge = state.cockpit.forge;
      const swarm = state.cockpit.swarm;
      return availableSlashCommands({
        workspaceTrusted: vscode.workspace.isTrusted,
        activeSession: Boolean(state.sessionId),
        activePlan: Boolean(forge.planId),
        forgeEnabled: state.cockpit.status.enableForge,
        personasAvailable: state.personas.supported && state.personas.enabled,
        compatibility: state.cockpit.status.runtime.compatibility,
        activeRun: ["queued", "starting", "running", "cancellation_requested"].includes(state.jobStatus)
          || Boolean(forge.activeJobId)
          || swarm.busy
          || Boolean(swarm.jobId)
          || ["starting", "running", "cancelling"].includes(swarm.status),
        isBackendActionSupported: (actionId) => backendActions.isActionIdSupported(actionId)
      });
    }
  });
  chatController.setSlashCommandRouter(slashRouter);
  chatController.setCockpitProvider(() => {
    const config = getConfig();
    return {
      status: {
        mode: config.defaultMode,
        composerMode: "chat",
        model: config.defaultModel || "default",
        provider: config.provider || (config.baseUrl ? "custom" : "default"),
        sandbox: config.sandboxProfile,
        workspaceTrusted: vscode.workspace.isTrusted,
        bridgeStatus: statusBar.snapshot(),
        enableForge: config.enableForge,
        cliTrusted: config.security?.cliPath.executionAllowed ?? true,
        cliReason: config.security?.cliPath.reason ?? null,
        providerProfile: profileStore.snapshot(),
        sessionModel: emptySessionModelState(),
        runtime: runtime.snapshot()
      },
      forge: forgeController.cockpitState(),
      swarm: forgeController.swarmCockpitState(runtime.snapshot().compatibility),
      browser: browserCockpitSummary(browserController.state()),
      actionResults: actionResultStore.list()
    };
  });
  // Bounded background reconnect: after a recoverable bridge-health failure, re-check a few times
  // with backoff and clear the recovery cards automatically once a check passes. The probe routes
  // through refreshBridgeStatus, which re-checks the Workspace-Trust / executable-origin guard on
  // every attempt and never spawns a blocked CLI. checkBridgeStatus re-reads the live config (so a
  // newly-set alysis.cliPath is honored) and a manual check always cancels any pending backoff.
  const probeBridgeStatus = async (): Promise<BridgeCheckOutcome> => {
    // The managed pointer is validated at activation and whenever a runtime-affecting setting changes.
    // Re-validating on every probe (three per backoff cycle) is pure overhead, so only re-run it when
    // there is no usable runtime — the one case a retry can actually fix.
    await runtimeValidation;
    if (getConfig().runtimeSelection?.origin === "unavailable") {
      await managedRuntime.refresh(readBaseConfig());
    }
    let config = getConfig();
    let outcome = await refreshBridgeStatus(cliDiscovery, config, statusBar, runtime);
    if (!outcome.ok && managedRuntime.isManaged(config) && await managedRuntime.recoverAfterHealthFailure()) {
      config = getConfig();
      output.appendLine("Managed runtime: retrying IDE health with the last-known-good CLI.");
      outcome = await refreshBridgeStatus(cliDiscovery, config, statusBar, runtime);
    }
    return outcome;
  };
  const autoReconnect = new BoundedBridgeReconnect(
    async () => {
      const outcome = await probeBridgeStatus();
      // Stop the backoff once healthy OR once the failure is no longer recoverable (e.g. the
      // workspace became untrusted mid-backoff) — never keep re-checking a now-blocked state.
      return outcome.ok || !outcome.recoverable;
    },
    RECONNECT_DELAYS_MS
  );
  context.subscriptions.push({ dispose: () => autoReconnect.reset() });
  const checkBridgeStatus = async (): Promise<void> => {
    autoReconnect.reset();
    const outcome = await probeBridgeStatus();
    if (!outcome.ok && outcome.recoverable) {
      autoReconnect.arm();
    }
    if (outcome.ok) {
      // Bridge is healthy — cache the profile list so the header provider/profile pill is populated.
      void profileStore.refresh(bridgeClient);
    }
  };
  chatController.setCockpitActions({
    cancel: async () => {
      await vscode.commands.executeCommand("alysis.cancelCurrentRun");
    },
    executePreview: (auto) => forgeController.executePreview(auto),
    executeReview: () => forgeController.execute(),
    refreshForgeStatus: () => forgeController.refreshStatus(),
    openDiff: (diffId) => forgeController.openDiff(diffId),
    openForgeArtifact: (sessionId, artifactId) => forgeController.openArtifact(sessionId, artifactId),
    respondForgeApproval: (sessionId, approvalId, decision) => forgeController.respondToCockpitApproval(sessionId, approvalId, decision),
    reviewChanges: (taskId) => forgeController.reviewChanges(taskId),
    refreshAssets: () => forgeController.refreshAssets(),
    openAsset: (assetId) => forgeController.openAsset(assetId),
    runSwarm: (parallel) => forgeController.runSwarm(parallel),
    cancelSwarm: () => forgeController.cancelSwarm(),
    refreshSwarmRecovery: () => forgeController.refreshSwarmRecovery(),
    resumeSwarm: (jobId, revision) => forgeController.resumeSwarm(jobId, revision),
    dismissSwarmRecovery: (jobId, revision) => forgeController.dismissSwarmRecovery(jobId, revision),
    refreshSwarmReview: () => forgeController.refreshSwarmReview(),
    applySwarmTask: (taskId) => forgeController.applySwarmTask(taskId),
    discardSwarmTask: (taskId) => forgeController.discardSwarmTask(taskId),
    regenerateSwarmTask: (taskId, instruction) => forgeController.regenerateSwarmTask(taskId, instruction),
    reconnect: () => checkBridgeStatus(),
    useProfile: async (name) => {
      // Use the same guarded provider transition as the composer and model picker.
      try {
        await providerCatalog.useConnection(name);
      } catch (error) {
        await vscode.window.showWarningMessage(
          redactForDisplay(error instanceof Error ? error.message : String(error))
        );
        return;
      }
      void profileStore.refresh(bridgeClient);
      void chatController.refreshSessionModelInfo();
    },
    browserRefresh: () => browserController.refresh(),
    browserStart: () => browserController.start(),
    browserStartLocal: () => browserController.startLocal(),
    browserSelect: (browserSessionId) => browserController.select(browserSessionId),
    browserNavigate: (url) => browserController.navigate(url),
    browserSnapshot: (kind) => browserController.snapshot(kind),
    browserScreenshot: (fullPage) => browserController.screenshot(fullPage),
    browserDiagnostics: () => browserController.diagnostics(),
    browserClick: (selector) => browserController.click(selector),
    browserType: (selector, text, replace) => browserController.type(selector, text, replace),
    browserClose: () => browserController.close(),
    browserSaveScreenshot: async () => {
      const destination = await vscode.window.showSaveDialog({
        title: "Save verified browser screenshot",
        saveLabel: "Save screenshot",
        filters: { "PNG image": ["png"] }
      });
      if (destination) {
        // VS Code owns the destination and overwrite confirmation. No path ever
        // crosses the webview message boundary.
        await browserController.saveScreenshot(destination.fsPath, true);
      }
    },
    browserOwnerChanged: (sessionId) => browserController.setOwnerSession(sessionId),
    browserDisconnected: (reason) => browserController.disconnect(reason)
  });
  forgeController.setCockpitCallbacks({
    reveal: () => startView ? startView.showSurface("forge") : chatController.openChat(),
    refresh: () => chatController.refreshCockpit()
  });

  const providerCatalog = new ProviderCatalogController(
    bridgeClient,
    secrets,
    getConfig,
    () => {
      // A catalog change moves the active profile/model, so refresh the cached profile list the
      // header pill reads from as well — otherwise the two disagree until the next poll. It also
      // moves the provider half of the readiness gate, so republish the cockpit state.
      void profileStore.refresh(bridgeClient);
      chatController.refreshCockpit();
      startView?.update();
    },
    () => vscode.workspace.isTrusted,
    // Lets the catalog run its live key check against a bridge that actually carries the key the
    // user just stored (idle-only, fingerprint-guarded restart — never mid-task).
    () => ensureCredentialBridge(bridgeClient, secrets, getConfig(), output),
    (profile, model) => chatController.withProviderTransition(
      async () => {
        await ensureCredentialBridge(bridgeClient, secrets, getConfig(), output);
        if (!bridgeClient.supportsMethod("session.setProfile")) {
          throw new Error("Upgrade the Alysis Code CLI before switching providers in the extension.");
        }
      },
      async (sessionId) => {
        if (sessionId) {
          await bridgeClient.sessionSetProfile(sessionId, profile, model);
        } else {
          await bridgeClient.profileUse({ workspace_trusted: vscode.workspace.isTrusted, name: profile });
          if (model) await bridgeClient.configSet({ workspace_trusted: vscode.workspace.isTrusted, key: "model", value: model });
        }
      }
    )
  );
  chatController.setProviderReadiness(() => providerSelectionReadiness(getConfig(), providerCatalog.snapshot()));
  const modelPickerDeps: ModelPickerDeps = {
    catalog: providerCatalog,
    // A running session carries its own model, so the picker has to reach it too - otherwise the
    // change only lands on the *next* session and the header pill disagrees with the transcript.
    setSessionModel: (model) => backendActions.executeSlashAction("session.setModel", model),
    hasActiveSession: () => Boolean(chatController.activeSessionId())
  };
  openModelPicker = async (model?: string) => (await runModelPicker(modelPickerDeps, model)).notice;
  registerPickModelCommand(context, modelPickerDeps);
  const worktrees = new WorktreeController(chatController, () => startView?.update(), context.globalState, () => {
    const state = chatController.primaryCockpitState().cockpit;
    return Boolean(state.forge.activeJobId || state.swarm.busy || ["running", "queued"].includes(state.swarm.status));
  });
  const reviewGitChanges = registerReviewCommands(context, backendActions);
  context.subscriptions.push(worktrees,
    vscode.commands.registerCommand(COMMANDS.newWorktree, () => worktrees.create()),
    vscode.commands.registerCommand(COMMANDS.moveToWorktree, () => worktrees.create(true)));
  startView = new StartViewProvider(
    context.extensionUri,
    () => runtime.snapshot(),
    getConfig,
    () => profileStore.snapshot(),
    () => vscode.workspace.isTrusted,
    () => chatController.primaryCockpitState(),
    () => sessionsView.snapshot(),
    (instruction, mode, requestId) => chatController.submitPrimaryMessage(instruction, mode, requestId),
    (instruction, mode, requestId) =>
      requestId
        ? forgeController.submitPlanWithInstruction(instruction, mode, requestId)
        : forgeController.planWithInstruction(instruction, mode),
    async () => {
      await vscode.commands.executeCommand("alysis.cancelCurrentRun");
    },
    (approvalId, decision) => chatController.respondToPrimaryApproval(approvalId, decision),
    (message) => chatController.handlePrimaryCockpitAction(message),
    () => providerCatalog.snapshot(),
    {
      refreshModels: () => providerCatalog.refresh(),
      getWorktreeState: () => worktrees.snapshot(),
      worktreeAction: (action) => action === "review" ? reviewGitChanges() : worktrees.create(action === "move"),
      connectProvider: (request) => providerCatalog.connect(request),
      useProvider: (profile) => providerCatalog.useConnection(profile),
      updateProviderKey: (profile) => providerCatalog.updateKey(profile),
      forgetProviderKey: (profile) => providerCatalog.forgetKey(profile),
      searchMentions: (query) => searchWorkspaceMentions(vscode, activeWorkspaceFolder(), query),
      attachContext: () => chatController.chooseContextToAttach(),
      attachFiles: () => chatController.addFilesToTask(),
      getImages: async (sessionId) => bridgeClient.supportsMethod("session.images.list")
        ? bridgeClient.sessionImagesList(sessionId) : undefined
    },
    () => browserController.state(),
    () => browserController.currentPreview(),
    browserPreviewRoot,
    (actionId) => backendActions.isActionIdSupported(actionId)
  );
  chatController.setPrimaryChatView({
    reveal: () => startView!.reveal(),
    prefill: (text) => startView!.prefill(text)
  });
  context.subscriptions.push(
    output,
    statusBar,
    chatController,
    forgeController,
    backendActions,
    hostActionController,
    { dispose: () => { void browserController.dispose(); } },
    diffContentProvider,
    startView,
    chatController.onDidChangeState(() => startView?.update()),
    sessionsView.onDidChangeTreeData(() => startView?.update()),
    vscode.commands.registerCommand(COMMANDS.showHistory, async () => {
      await chatController.openChat();
      await startView?.showSurface("history");
    }),
    vscode.commands.registerCommand(COMMANDS.showSettings, () => startView?.showSurface("settings")),
    vscode.commands.registerCommand(COMMANDS.showModels, async () => {
      await startView?.showSurface("models");
      await providerCatalog.refresh();
    }),
    vscode.commands.registerCommand(COMMANDS.showBrowser, () => startView?.showSurface("browser")),
    vscode.commands.registerCommand(COMMANDS.showForge, () => startView?.showSurface("forge"))
  );
  const manageView = new ManageViewProvider(
    bridgeClient,
    () => runtime.snapshot().compatibility,
    () => vscode.workspace.isTrusted,
    () => checkBridgeStatus(),
    () => vscode.env.remoteName
  );
  // FE-19: the runtime's view of capabilities/CLI-health must follow the LIVE bridge. Feed it the live
  // bridge state on every status-bar change AND the instant the bridge handshake completes or drops, so
  // the Manage tree / provider pill / drawer reflect a healthy connected CLI without a window reload.
  const liveBridge = () => ({ running: bridgeClient.isRunning(), health: bridgeClient.currentHealth() });
  const syncCommandContexts = (): void => {
    const health = bridgeClient.currentHealth();
    // alysis.cancelCurrentRun is the only unwedge path, so its enablement key must reflect BOTH
    // run owners. A Forge-only run previously left the command disabled exactly when it was needed.
    const hasActiveJob = chatController.hasActiveJob() || forgeController.hasActiveJob();
    void Promise.all([
      vscode.commands.executeCommand("setContext", "alysis.bridgeReady", bridgeClient.isRunning() && health !== undefined),
      vscode.commands.executeCommand("setContext", "alysis.hasActiveSession", chatController.hasSession()),
      vscode.commands.executeCommand("setContext", "alysis.hasActiveJob", hasActiveJob),
      vscode.commands.executeCommand("setContext", "alysis.workspaceScopeResolved", activeWorkspaceRoot() !== undefined),
      vscode.commands.executeCommand("setContext", "alysis.workspaceTrusted", vscode.workspace.isTrusted)
    ]).catch((error: unknown) => {
      output.appendLine(`Could not refresh Alysis Code command state: ${redactForDisplay(String(error))}`);
    });
  };
  const syncRuntimeToBridge = () => {
    runtime.applyStatusBarSnapshot(statusBar.snapshot(), liveBridge());
    syncCommandContexts();
  };
  bridgeClient.on("health", syncRuntimeToBridge);
  context.subscriptions.push({ dispose: () => bridgeClient.off("health", syncRuntimeToBridge) });
  // The Manage domains are surfaced through the sidebar quick picks rather than a tree view, so the
  // capability snapshot is refreshed on runtime changes instead of on tree visibility.
  const refreshManageWhenStale = (): void => {
    if (manageView.compatibilityNeedsCheck()) {
      void manageView.refresh();
    }
  };
  const syncArtifactViewContext = (): void => {
    const hasArtifacts = artifactsView.state().some((group) => group.artifacts.length > 0);
    void vscode.commands.executeCommand("setContext", "alysis.hasArtifacts", hasArtifacts);
  };
  context.subscriptions.push(
    runtime.onDidChange(() => {
      browserController.syncCompatibility();
      chatController.refreshCockpit();
      startView?.update();
    }),
    // Manage gates each domain on the live capability snapshot, so re-render when it changes.
    runtime.onDidChange(() => {
      manageView.refreshTree();
      refreshManageWhenStale();
    }),
    statusBar.onDidChangeSnapshot((snapshot) => runtime.applyStatusBarSnapshot(snapshot, liveBridge())),
    chatController.onDidChangeState(syncCommandContexts),
    // Forge state transitions are the other half of alysis.hasActiveJob; nothing else observes them.
    forgeController.onDidChangeActivity(syncCommandContexts),
    vscode.window.onDidChangeActiveTextEditor(syncCommandContexts),
    vscode.workspace.onDidChangeWorkspaceFolders(syncCommandContexts),
    vscode.window.registerWebviewViewProvider("alysis.start", startView),
    forgePlanView.onDidChangeTreeData(() => chatController.refreshCockpit()),
    artifactsView.onDidChangeTreeData(() => {
      syncArtifactViewContext();
      chatController.refreshCockpit();
    })
  );
  refreshManageWhenStale();
  syncArtifactViewContext();

  registerOpenChatCommands(context, chatController);
  registerCancelCurrentRunCommand(context, chatController, forgeController);
  registerForgePlanCommand(context, forgeController);
  registerForgeExecuteCommand(context, forgeController);
  registerRunSwarmCommand(context, forgeController);
  registerRunDoctorCommand(context, cliDiscovery, getConfig, output, statusBar, runtime, () => runtimeValidation);
  registerShowBridgeHealthCommand(context, cliDiscovery, getConfig, output, statusBar, runtime, () => runtimeValidation);
  registerConfigureProviderCommand(context, secrets, bridgeClient, getConfig, () =>
    ensureCredentialBridge(bridgeClient, secrets, getConfig(), output)
  );
  registerCliSetupCommands(context);
  registerLocateCliCommand(context, {
    runner: defaultCliRunner,
    getConfig,
    reconnect: () => checkBridgeStatus(),
    pythonScriptDirs: getPythonScriptDirs
  });
  backendActions.register(context);
  registerSessionCommands(context, backendActions);
  registerManageCommands(context, backendActions, manageView, syncRuntimeToBridge);
  registerAlysisChatParticipant(context, {
    openCockpit: () => chatController.openChat(),
    slashRouter,
    runtime
  });

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (!event.affectsConfiguration("alysis")) {
        return;
      }
      // Only settings that change which executable is selected may re-run managed-runtime validation.
      // Cosmetic settings (showStatusBar and friends) previously triggered SHA-256 + signature checks.
      const revalidated = RUNTIME_AFFECTING_SETTINGS.some((setting) => event.affectsConfiguration(setting))
        ? managedRuntime.refresh(readBaseConfig())
        : Promise.resolve();
      void revalidated.then(() => {
        const config = getConfig();
        runtime.applyConfig(config);
        applyStatusBarVisibility(statusBar, config);
        void profileStore.refresh(bridgeClient);
        chatController.refreshCockpit();
        startView?.update();
      });
    })
  );
  context.subscriptions.push(
    vscode.workspace.onDidGrantWorkspaceTrust(() => {
      const config = getConfig();
      runtime.applyConfig(config);
      if (config.autoStartBridge) {
        void checkBridgeStatus();
      } else {
        statusBar.setIdle();
      }
      manageView.refreshTree();
      chatController.refreshCockpit();
      startView?.update();
      startView?.updateBrowser();
    })
  );

  runtime.applyConfig(getConfig());
  runtime.applyStatusBarSnapshot(statusBar.snapshot(), liveBridge());
  applyStatusBarVisibility(statusBar, getConfig());
  syncCommandContexts();
  if (!runtimeValidationSettled) {
    // Distinguish "still validating the signed runtime" from "no runtime is installed" in the UI.
    runtime.setCliHealth("checking", RUNTIME_VALIDATION_PENDING_MESSAGE);
  }
  if (!vscode.workspace.isTrusted) {
    statusBar.setUntrustedWorkspace();
  } else {
    statusBar.setIdle();
  }
  // Activation returns once commands and views are registered. The first bridge spawn is the only
  // thing gated on managed-runtime validation; the UI is refreshed again when it settles.
  const backgroundActivation = runtimeValidation.then(async () => {
    const config = getConfig();
    runtime.applyConfig(config);
    applyStatusBarVisibility(statusBar, config);
    chatController.refreshCockpit();
    startView?.update();
    if (config.autoStartBridge) {
      await checkBridgeStatus();
    }
    if (worktreeHandoff) {
      try {
        if (worktreeHandoff.reference && vscode.workspace.isTrusted) await chatController.ensureLiveSession();
        await chatController.openChat();
      } catch (error) {
        output.appendLine(`Worktree conversation can be resumed from Task History: ${redactForDisplay(errorText(error))}`);
      }
    }
  });
  chatController.refreshCockpit();
  startView.update();

  return createExtensionApi(
    context,
    chatController,
    forgeController,
    browserController,
    statusBar,
    secrets,
    installedProductionQaEnabled,
    getConfig,
    hostActionController,
    backgroundActivation
  );
}

// VS Code terminates the extension host shortly after deactivate settles, so the whole teardown gets
// one budget. A wedged CLI must never make Reload Window hang on the bridge's own longer timeouts.
export const DEACTIVATE_DEADLINE_MS = 4_000;

export async function deactivate(): Promise<void> {
  const chatController = activeChatController;
  const forgeController = activeForgeController;
  const browserController = activeBrowserController;
  const hostActionController = activeHostActionController;
  const bridgeClient = activeBridgeClient;
  activeChatController = undefined;
  activeForgeController = undefined;
  activeBrowserController = undefined;
  activeHostActionController = undefined;
  activeBridgeClient = undefined;
  hostActionController?.dispose();
  await withDeadline(
    (async () => {
      await chatController?.shutdown({ shutdownBridge: false });
      await forgeController?.shutdown();
      await browserController?.dispose();
      await bridgeClient?.shutdown("bridge_stopped");
    })(),
    DEACTIVATE_DEADLINE_MS
  );
}

/**
 * Resolve when `work` settles or the deadline expires, whichever comes first. Rejections are absorbed
 * because process teardown is already underway and there is no user-visible surface left to report to.
 */
export async function withDeadline(work: Promise<unknown>, timeoutMs: number): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      work.then(() => undefined, () => undefined),
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, timeoutMs);
        timer.unref?.();
      })
    ]);
  } finally {
    if (timer) {
      clearTimeout(timer);
    }
  }
}

function createExtensionApi(
  context: vscode.ExtensionContext,
  chatController: ChatController,
  forgeController: ForgeController,
  browserController: BrowserCockpitController,
  statusBar: AlysisStatusBar,
  secrets: AlysisSecretStore,
  installedProductionQaEnabled: boolean,
  getConfig: () => AlysisConfig,
  hostActionController: HostActionController,
  backgroundActivation: Promise<void>
): AlysisExtensionApi | undefined {
  if (
    context.extensionMode === vscode.ExtensionMode.Production &&
    installedProductionQaEnabled
  ) {
    return {
      installedProductionQa: {
        hasProviderSecret: async () => Boolean((await secrets.getApiKey())?.trim()),
        storeProviderSecret: async (value: string) => secrets.setApiKey(value),
        clearProviderSecret: async () => secrets.deleteApiKey(),
        runReadonlyChat: async (prompt, expectedMarker) => {
          // Runtime validation and the first bridge check now settle in the background.
          await backgroundActivation;
          return runInstalledProductionQaChat(chatController, getConfig, prompt, expectedMarker);
        }
      }
    };
  }
  if (context.extensionMode !== vscode.ExtensionMode.Test) {
    return undefined;
  }
  const testSecrets = new AlysisSecretStore(context.secrets, "alysis.test.apiKey");
  return {
    test: {
      // Activation no longer blocks on runtime validation or the first health probe; tests that
      // assert post-handshake state await this instead of racing the background task.
      whenReady: () => backgroundActivation,
      state: () => ({
        chat: chatController.testState(),
        forge: forgeController.testState(),
        browser: browserController.state(),
        statusBar: statusBar.snapshot()
      }),
      submitChatText: (text: string) => chatController.testSubmit(text),
      startTask: (text, mode = "review") => chatController.submitPrimaryMessage(text, mode),
      hasConfiguredApiKey: async () => Boolean((await secrets.getApiKey())?.trim()),
      setConfiguredApiKey: async (value: string) => secrets.setApiKey(value),
      clearConfiguredApiKey: async () => secrets.deleteApiKey(),
      respondForgeApproval: (sessionId, approvalId, decision) => forgeController.respondToCockpitApproval(sessionId, approvalId, decision),
      browserStart: () => browserController.start(),
      browserStartLocal: () => browserController.startLocal(),
      browserNavigate: (url) => browserController.navigate(url),
      browserSnapshot: (kind = "text") => browserController.snapshot(kind),
      browserScreenshot: (fullPage = false) => browserController.screenshot(fullPage),
      browserDiagnostics: () => browserController.diagnostics(),
      browserClose: () => browserController.close(),
      secretStorageRoundTrip: async () => {
        await testSecrets.deleteApiKey();
        await testSecrets.setApiKey("test-secret-value");
        const stored = await testSecrets.getApiKey();
        await testSecrets.deleteApiKey();
        const cleared = await testSecrets.getApiKey();
        return { stored: stored === "test-secret-value", cleared: cleared === undefined };
      },
      hostActions: () => hostActionController.snapshot()
    }
  };
}

async function runInstalledProductionQaChat(
  chatController: ChatController,
  getConfig: () => AlysisConfig,
  prompt: string,
  expectedMarker: string
): Promise<{
  responseLength: number;
  responseSha256: string;
  markerMatched: boolean;
  elapsedMs: number;
}> {
  const trimmedPrompt = prompt.trim();
  const marker = expectedMarker.trim();
  if (!trimmedPrompt || trimmedPrompt.length > 1_000) {
    throw new Error("Installed production QA prompt must contain 1-1000 characters.");
  }
  if (!/^[A-Z0-9_]{16,96}$/.test(marker)) {
    throw new Error("Installed production QA marker is invalid.");
  }
  if (getConfig().defaultMode !== "readonly") {
    throw new Error("Installed production QA requires readonly mode.");
  }

  const started = Date.now();
  const baselineItems = chatController.testState().items.length;
  const accepted = await chatController.submitPrimaryMessage(trimmedPrompt, "readonly");
  if (!accepted) {
    throw new Error("Installed production QA chat request was not accepted.");
  }
  const deadline = started + 180_000;
  while (Date.now() < deadline) {
    const state = chatController.testState();
    const newItems = state.items.slice(baselineItems);
    const failure = newItems.find((item) => item.kind === "error");
    if (failure) {
      const evidence = redactedTextEvidence(failure.text);
      throw new Error(
        `Installed production QA chat failed (length=${evidence.length}, sha256=${evidence.sha256}).`
      );
    }
    const response = [...newItems]
      .reverse()
      .find((item) => item.kind === "assistant" && item.text.trim().length > 0);
    if (response && state.activeJobId === null) {
      const text = response.text.trim();
      const evidence = redactedTextEvidence(text);
      return {
        responseLength: evidence.length,
        responseSha256: evidence.sha256,
        markerMatched: text === marker,
        elapsedMs: Date.now() - started
      };
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
  }
  throw new Error("Installed production QA chat timed out without retaining response content.");
}

function redactedTextEvidence(value: string): { length: number; sha256: string } {
  return {
    length: value.length,
    sha256: createHash("sha256").update(value, "utf8").digest("hex")
  };
}

// A JavaScript fixture is directly executable through its shebang on POSIX, but Windows requires an
// interpreter. Keep the production launchers unchanged (and shell-free); Extension Host test mode uses
// Node only when the configured mock CLI is a .js file.
const extensionHostTestCliRunner: CliRunner = (command, args, options) =>
  defaultCliRunner(
    isWindowsJavaScriptFixture(command) ? extensionHostFixtureRuntime() : command,
    isWindowsJavaScriptFixture(command) ? [command, ...args] : args,
    options
  );

const extensionHostTestBridgeProcessFactory: BridgeProcessFactory = (command, args, options): BridgeProcess => {
  const fixture = isWindowsJavaScriptFixture(command);
  return spawn(fixture ? extensionHostFixtureRuntime() : command, fixture ? [command, ...args] : [...args], {
    env: options.env,
    stdio: "pipe",
    windowsHide: true
  });
};

function isWindowsJavaScriptFixture(command: string): boolean {
  return process.platform === "win32" && command.toLowerCase().endsWith(".js");
}

// Test-only: an editor fork's Electron executable is not a portable Node interpreter. The external
// test runner supplies its own Node executable; production never installs either fixture launcher.
function extensionHostFixtureRuntime(): string {
  return process.env.ALYSIS_TEST_NODE_PATH || process.execPath;
}

// Bounded backoff for the automatic bridge reconnect: one delay per retry (3 tries), increasing.
const RECONNECT_DELAYS_MS = [2000, 4000, 8000];

// The managed runtime pointer is resolved from the CLI path override alone; no other alysis.*
// setting changes which executable is selected, so no other setting may trigger re-validation.
const RUNTIME_AFFECTING_SETTINGS = ["alysis.cliPath"] as const;

// Codes a bounded background re-check can never clear on its own: Workspace Trust and executable
// origin only change through an explicit user action, which re-arms the check by itself.
const NON_RECOVERABLE_BRIDGE_FAILURE_CODES: readonly string[] = ["cli_untrusted"];

// "The managed runtime is absent" is reported through the same blocked-CLI guard as a trust failure,
// but the user can fix it out of band (install or repair the release), so it stays recoverable.
// BUNDLED_RUNTIME_MISSING ("this build ships no bundle") is exactly that case. BUNDLED_RUNTIME_INVALID
// is deliberately NOT listed: a bundle that failed verification will keep failing verification, so a
// bounded background re-check would only spin — it needs a reinstall.
const RUNTIME_ABSENT_BRIDGE_FAILURE_CODES: readonly string[] = [
  "cli_runtime_unavailable",
  BUNDLED_RUNTIME_MISSING_CODE
];

/** Decide whether a failed bridge health check is worth a bounded background re-check. */
export function isRecoverableBridgeFailure(code: string, runtimeOrigin: string | undefined): boolean {
  if (RUNTIME_ABSENT_BRIDGE_FAILURE_CODES.includes(code) || runtimeOrigin === "unavailable") {
    return true;
  }
  return !NON_RECOVERABLE_BRIDGE_FAILURE_CODES.includes(code);
}

interface BridgeCheckOutcome {
  ok: boolean;
  // True when a bounded background re-check is worth attempting (the failure may clear on its own or
  // once the user fixes the CLI out-of-band). False for trust/origin-blocked failures.
  recoverable: boolean;
}

async function refreshBridgeStatus(
  cliDiscovery: CliDiscovery,
  config: AlysisConfig,
  statusBar: AlysisStatusBar,
  runtime?: CockpitRuntimeState
): Promise<BridgeCheckOutcome> {
  const guard = evaluateProcessExecutionCommand(config, "autoStartBridge");
  if (!guard.allowed) {
    runtime?.setCliOrigin("blocked", guard.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust.");
    runtime?.setBridgeProcess("error", guard.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust.");
    statusBar.setError(guard.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust.");
    // A missing managed runtime blocks execution through this same guard, and that is user-fixable.
    return {
      ok: false,
      recoverable: isRecoverableBridgeFailure("cli_untrusted", config.runtimeSelection?.origin)
    };
  }
  runtime?.setCliHealth("checking", "Checking Alysis Code CLI and IDE bridge health.");
  runtime?.setBridgeProcess("starting", "Checking Alysis Code IDE bridge health.");
  const result = await cliDiscovery.detect(config);
  runtime?.applyDetectionResult(result);
  if (result.ok) {
    statusBar.setBridgeOk();
    runtime?.recordEvent({
      severity: "info",
      source: "bridge",
      title: "Bridge health ok",
      message: `Protocol ${result.health.protocol_version}, Alysis Code ${result.health.alysis_version}.`
    });
    return { ok: true, recoverable: false };
  }
  if (result.code === "cli_missing") {
    statusBar.setMissingCli();
    return { ok: false, recoverable: true };
  }
  statusBar.setError(result.message);
  // FE-10: no standalone "Bridge health failed" event. applyDetectionResult already drove the
  // canonical recovery card from the structured status; a duplicate sibling event would render
  // beside the card (and in the Timeline) and never clear on recovery. The card's "Open Output"
  // action still surfaces full logs.
  return { ok: false, recoverable: isRecoverableBridgeFailure(result.code, config.runtimeSelection?.origin) };
}

function applyStatusBarVisibility(statusBar: AlysisStatusBar, config: AlysisConfig): void {
  statusBar.setVisible(config.showStatusBar);
}

export const RUNTIME_VALIDATION_PENDING_MESSAGE =
  "Validating the signed Alysis Code runtime. The local engine starts as soon as this finishes.";

/**
 * Present the short window before managed-runtime validation settles as an explicit "validating"
 * state instead of the permanent "no managed runtime is installed" failure. Execution stays blocked
 * (fail closed); only the user-visible reason changes.
 */
export function runtimeValidatingConfig(config: AlysisConfig): AlysisConfig {
  if (config.runtimeSelection?.origin !== "unavailable") {
    return config;
  }
  return {
    ...config,
    security: config.security
      ? {
          ...config.security,
          cliPath: { ...config.security.cliPath, reason: RUNTIME_VALIDATION_PENDING_MESSAGE }
        }
      : undefined,
    runtimeSelection: { ...config.runtimeSelection, validating: true, message: RUNTIME_VALIDATION_PENDING_MESSAGE }
  };
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function getPythonScriptDirs(): string[] {
  // Best-effort: the configured Python interpreter (if any) shares a bin/Scripts dir with a
  // pipx/venv-installed `alysis`. No hard dependency on the Python extension.
  const interpreter = vscode.workspace.getConfiguration("python").get<string>("defaultInterpreterPath", "");
  return pythonScriptDirsFromInterpreter(interpreter);
}

// VS Code's proxy settings live in the workbench, not in process.env; the bridge client turns
// these into HTTP_PROXY / HTTPS_PROXY / NO_PROXY for the CLI. Read fresh on every config read so
// a settings change is picked up by the next bridge launch.
function readHostNetworkSettings(): Omit<HostNetworkSettings, "extraCaCerts"> {
  const http = vscode.workspace.getConfiguration("http");
  const noProxy = http.get<unknown>("noProxy", []);
  return {
    proxy: http.get<string>("proxy", ""),
    noProxy: Array.isArray(noProxy) ? noProxy.map((entry) => String(entry)) : [],
    proxySupport: http.get<string>("proxySupport", "override"),
    proxyStrictSSL: http.get<boolean>("proxyStrictSSL", true)
  };
}

function reportHostNetworkNotice(
  output: vscode.OutputChannel,
  reported: Set<string>,
  notice: HostEnvironmentNotice
): void {
  const key = `host-network:${notice.code}:${notice.message}`;
  if (reported.has(key)) {
    return;
  }
  reported.add(key);
  output.appendLine(`Network: ${notice.message}`);
}

function reportConfigNotice(
  output: vscode.OutputChannel,
  reported: Set<string>,
  notice: SafeConfigNotice
): void {
  const key = `${notice.code}:${notice.setting}:${notice.message}`;
  if (reported.has(key)) {
    return;
  }
  reported.add(key);
  output.appendLine(`Security: ${notice.message}`);
}

function createExtensionManagedRuntime(
  context: vscode.ExtensionContext,
  output: vscode.OutputChannel
): ManagedRuntimeCoordinator {
  const trustedReleaseHosts = [
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com"
  ] as const;
  let store: ManagedRuntimeStore;
  let reconcileBundled: ((signal?: AbortSignal) => Promise<InstalledRuntime>) | undefined;
  try {
    const version = String((context.extension.packageJSON as { version?: unknown }).version ?? "0.0.0");
    const bundleRoot = vscode.Uri.joinPath(context.extensionUri, "resources", "managed-cli").fsPath;
    const runtime = new ManagedCliRuntime({
      storageRoot: vscode.Uri.joinPath(context.globalStorageUri, "managed-cli").fsPath,
      bundledRuntimeRoot: bundleRoot,
      compatibility: {
        extensionVersion: version,
        protocol: { min: PROTOCOL_VERSION, max: PROTOCOL_VERSION }
      },
      releasePolicy: {
        sourceRepository: "https://github.com/AlysisAi/AlysisCode",
        provenanceIssuer: "https://token.actions.githubusercontent.com",
        provenanceWorkflow: "https://github.com/AlysisAi/AlysisCode/.github/workflows/managed-cli-vsix-release.yml"
      },
      requireSignature: true,
      signatureVerifier: createManagedCliReleaseSignatureVerifier(),
      trustedDownloadHosts: trustedReleaseHosts
    });
    store = runtime;
    reconcileBundled = (signal) => installBundledManagedCli(
      bundleRoot,
      runtime,
      trustedReleaseHosts,
      signal
    );
  } catch (error) {
    const failure = error instanceof Error ? error : new Error(String(error));
    store = {
      getActive: async () => { throw failure; },
      rollback: async () => { throw failure; },
      cleanupOldVersions: async () => []
    };
  }
  return new ManagedRuntimeCoordinator(store, {
    allowDevelopmentPathFallback: context.extensionMode !== vscode.ExtensionMode.Production,
    report: (message) => output.appendLine(`Managed runtime: ${redactForDisplay(message)}`),
    reconcileBundled
  });
}

async function ensureCredentialBridge(
  bridge: AlysisBridgeClient,
  secrets: AlysisSecretStore,
  config: AlysisConfig,
  output?: vscode.OutputChannel
): Promise<void> {
  const options: BridgeStartOptions = {};
  const forwardingAllowed = !config.security || config.security.cliPath.apiKeyForwardingAllowed;
  if (forwardingAllowed) {
    const apiKey = await secrets.getApiKey();
    if (apiKey && apiKey.trim().length > 0) {
      options.apiKey = apiKey;
    }
    const providerCredentials = await secrets.listProviderCredentials();
    if (providerCredentials.length > 0) {
      options.providerCredentials = providerCredentials;
    }
  }
  // Names and counts only — never values. When the CLI later reports "Missing API key", this line
  // says which store the extension expected it to read: forwarded VS Code secrets or its own file.
  output?.appendLine(
    `Credential bridge: forwarding=${forwardingAllowed ? "allowed" : `blocked (${redactForDisplay(config.security?.cliPath.reason ?? "executable origin")})`}; `
      + `vscode secrets: global=${options.apiKey ? "yes" : "no"}, profiles=${(options.providerCredentials ?? []).map((entry) => entry.envVar).join(",") || "none"}; `
      + `origin=${config.runtimeSelection?.origin ?? "unknown"}`
  );
  await bridge.ensureStartedForProfile(config, options, { credentialsRequired: true });
}
