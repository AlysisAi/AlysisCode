import path from "node:path";

import * as vscode from "vscode";

import { AlysisConfig, redactDeep, redactForDisplay } from "../client/CliDiscovery";
import { ProtocolClientError, AlysisBridgeClient } from "../client/AlysisBridgeClient";
import { evaluateProcessExecutionCommand } from "../security/commandGuards";
import { mcpOAuthRemoteUnavailableReason } from "../security/remoteHost";
import type { CodeReviewPresenter } from "../review/CodeReviewPresenter";
import { activeWorkspaceRoot, workspaceScopeRequiredMessage } from "../workspace/activeWorkspace";
import { assertNoDirtyWorkspaceDocuments } from "../workspace/DirtyWorkspaceGuard";
import { ActionResultStore } from "./ActionResultStore";
import {
  BACKEND_ACTION_GROUPS,
  BackendActionGroupId,
  BackendActionMetadata,
  backendActionById,
  backendActionConfirmation,
  backendActionMutationLabel,
  backendActionMutatesWithParams,
  backendActionsForGroup,
  isBackendActionSupportedByCapabilities
} from "./BackendActionMetadata";

type SessionMode = "readonly" | "review" | "auto";

export interface BackendActionContext {
  sessionId?: string;
  workspaceRoot?: string;
}

export interface BackendActionDependencies {
  bridge: AlysisBridgeClient;
  getConfig(): AlysisConfig;
  output: vscode.OutputChannel;
  isWorkspaceTrusted(): boolean;
  activeContext(): BackendActionContext;
  ensureLiveSession?(): Promise<string>;
  resumeSession?(sessionId: string, targetSessionId: string): Promise<unknown>;
  ensureCredentialBridge?(config: AlysisConfig): Promise<void>;
  useProfile?(name: string): Promise<unknown>;
  // Optional sink that surfaces the action lifecycle (running -> ok/error + redacted payload) in the
  // cockpit. Optional so existing wiring/tests that only need the Output channel still compile.
  actionResults?: ActionResultStore;
  /** Native Problems presenter; optional for older hosts and isolated tests. */
  codeReviewPresenter?: Pick<CodeReviewPresenter, "run" | "present">;
  /** Trusted extension-host sink for an attachable, backend-bounded context block. */
  attachContextBlock?(block: Record<string, unknown>): Promise<void> | void;
  /** Extension-host location seam; production defaults to vscode.env.remoteName. */
  remoteName?: () => string | undefined;
}

export interface BackendActionRunResult {
  action: BackendActionMetadata;
  result: unknown;
  notice: string;
}

export class BackendActionController implements vscode.Disposable {
  private readonly disposables: vscode.Disposable[] = [];

  public constructor(private readonly deps: BackendActionDependencies) {}

  public register(context: vscode.ExtensionContext): void {
    for (const group of BACKEND_ACTION_GROUPS) {
      const disposable = vscode.commands.registerCommand(group.commandId, async () => {
        try {
          return await this.runGroup(group.id);
        } catch (error) {
          await this.showCommandError(`Could not open ${group.title}`, error);
          return undefined;
        }
      });
      context.subscriptions.push(disposable);
      this.disposables.push(disposable);
    }
    for (const action of backendActionsForHiddenRegistration()) {
      const disposable = vscode.commands.registerCommand(action.commandId, async (args?: string) => {
        try {
          return await this.executeAction(action.id, typeof args === "string" ? args : "");
        } catch (error) {
          await this.showCommandError(`${action.title} could not be completed`, error);
          return undefined;
        }
      });
      context.subscriptions.push(disposable);
      this.disposables.push(disposable);
    }
  }

  public dispose(): void {
    for (const disposable of this.disposables.splice(0)) {
      disposable.dispose();
    }
  }

  public async runGroup(group: BackendActionGroupId): Promise<BackendActionRunResult | undefined> {
    await this.ensureBridge();
    const groupMetadata = BACKEND_ACTION_GROUPS.find((entry) => entry.id === group);
    const actions = backendActionsForGroup(group).filter((action) => this.isVisible(action));
    if (actions.length === 0) {
      void vscode.window.showWarningMessage(
        `${groupMetadata?.title ?? "These tools"} are not available with the connected Alysis Code CLI.`
      );
      return undefined;
    }
    const picked = await vscode.window.showQuickPick(
      actions.map((action) => ({
        label: action.title,
        description: backendActionMutationLabel(action),
        detail: action.description,
        action
      })),
      {
        title: groupMetadata?.title ?? "Alysis Code tools",
        placeHolder: groupMetadata?.description ?? "Choose what you want to do",
        ignoreFocusOut: true,
        matchOnDescription: true,
        matchOnDetail: true
      }
    );
    if (!picked) {
      return undefined;
    }
    return this.executeAction(picked.action.id);
  }

  public async executeSlashAction(actionId: string, args = ""): Promise<string> {
    const outcome = await this.executeAction(actionId, args, { fromSlash: true });
    // FE-18: a completed backend action already shows its running -> ok card in the timeline (the FE-11
    // lifecycle) and logs full detail to the Output channel, so the redundant "X completed." notice is
    // suppressed — the card is the single canonical feedback. A cancellation produces NO card, so its
    // notice is kept as the only feedback.
    const cancelled = Boolean((outcome.result as { cancelled?: boolean } | undefined)?.cancelled);
    return cancelled ? outcome.notice : "";
  }

  public async executeAction(
    actionId: string,
    args = "",
    options: { fromSlash?: boolean } = {}
  ): Promise<BackendActionRunResult> {
    const action = backendActionById(actionId);
    if (!action) {
      throw new Error(`Unknown Alysis Code backend action: ${actionId}`);
    }
    // Review belongs to the active session, even if the editor moves to another root.
    const reviewContext = action.id === "code.review.start" ? { ...this.deps.activeContext() } : undefined;
    await this.guardActionPreflight(action);
    if (reviewContext) this.guardCodeReviewContext(reviewContext);
    await this.ensureBridge(action);
    this.guardActionSupport(action);
    if (reviewContext) this.guardCodeReviewContext(reviewContext);
    let params: Record<string, unknown> | undefined;
    try {
      params = await this.collectParams(action, args);
    } catch (error) {
      if (error instanceof ProtocolClientError && error.code === "input_cancelled") {
        return { action, result: { cancelled: true }, notice: `${action.title} cancelled.` };
      }
      throw error;
    }
    if (params === undefined) {
      return { action, result: { cancelled: true }, notice: `${action.title} cancelled.` };
    }
    if (reviewContext) this.guardCodeReviewContext(reviewContext);
    const actionMutates = backendActionMutatesWithParams(action, params);
    const mutationWorkspace = actionMutates && backendActionCanTouchWorkspace(action)
      ? reviewContext?.workspaceRoot ?? workspaceRoot()
      : undefined;
    if (mutationWorkspace) {
      assertNoDirtyWorkspaceDocuments(vscode, mutationWorkspace, `using ${action.title}`);
    }
    // The gate follows the mutation, not the entry point: a read-only action whose parameters reach a
    // confirmation-gated mutation (/subagents on, /trace clear) confirms under that mutation's title.
    const confirmation = backendActionConfirmation(action, params);
    if (confirmation && !(await this.confirmAction(confirmation.title, confirmation.mutates))) {
      return { action, result: { cancelled: true }, notice: `${action.title} cancelled.` };
    }
    // A document can become dirty while the confirmation modal is open. Recheck
    // at the final host-controlled boundary before the backend request is sent.
    if (mutationWorkspace) {
      assertNoDirtyWorkspaceDocuments(vscode, mutationWorkspace, `using ${action.title}`);
    }
    if (reviewContext) this.guardCodeReviewContext(reviewContext);
    // Lifecycle starts AFTER param collection + confirmation so the running card never appears while
    // a modal prompt is still open. The store redacts the payload before it reaches the cockpit.
    const resultId = this.deps.actionResults?.start(action.id, action.title, actionMutates);
    try {
      const result = await this.invoke(action, params);
      const handledResult = await this.handleStructuredAction(action, params, result);
      if (resultId !== undefined) {
        this.deps.actionResults?.finish(resultId, actionCardProjection(action.id, handledResult));
      }
      this.showResult(action, handledResult, options.fromSlash === true);
      return {
        action,
        result: handledResult,
        notice: action.id.startsWith("code.review.") && isRecord(handledResult)
          ? codeReviewNotice(handledResult)
          : `${action.title} completed.`
      };
    } catch (error) {
      if (resultId !== undefined) {
        this.deps.actionResults?.fail(resultId, error instanceof Error ? error.message : String(error));
      }
      throw error;
    }
  }

  public isActionSupported(action: BackendActionMetadata): boolean {
    if (action.id === "mcp.auth.login.start" && this.mcpOAuthUnavailableReason()) {
      return false;
    }
    return isBackendActionSupportedByCapabilities(action, {
      supportsMethod: (method) =>
        this.deps.bridge.supportsMethod(method as Parameters<AlysisBridgeClient["supportsMethod"]>[0]),
      featureValue: (path) => this.deps.bridge.featureValue(path)
    });
  }

  public isActionIdSupported(actionId: string): boolean {
    const action = backendActionById(actionId);
    return action !== undefined && this.isActionSupported(action);
  }

  private isVisible(action: BackendActionMetadata): boolean {
    if (!this.isActionSupported(action)) {
      return false;
    }
    if (action.workspaceRequired && !(action.id === "code.review.start" ? this.deps.activeContext().workspaceRoot : workspaceRoot())) {
      return false;
    }
    const context = this.deps.activeContext();
    if (action.requiresActiveSession && !context.sessionId && !(action.id === "session.search" && this.deps.ensureLiveSession)) {
      return false;
    }
    return true;
  }

  private async ensureBridge(action?: BackendActionMetadata): Promise<void> {
    const config = this.deps.getConfig();
    const guard = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!guard.allowed) {
      throw new Error(guard.reason ?? "Alysis Code CLI execution is blocked.");
    }
    if (action?.credentialsRequired) {
      if (!this.deps.ensureCredentialBridge) {
        throw new ProtocolClientError(
          "bridge_credentials_unavailable",
          `${action.title} requires a credential-capable Alysis Code bridge.`
        );
      }
      await this.deps.ensureCredentialBridge(config);
      return;
    }
    await this.deps.bridge.ensureStarted(config, { stripApiKey: true });
  }

  private async guardActionPreflight(action: BackendActionMetadata): Promise<void> {
    if (action.id === "mcp.auth.login.start") {
      const reason = this.mcpOAuthUnavailableReason();
      if (reason) {
        throw new ProtocolClientError("mcp_oauth_remote_unavailable", reason);
      }
    }
    if (action.workspaceTrustRequired && !this.deps.isWorkspaceTrusted()) {
      throw new ProtocolClientError(
        "workspace_trust_required",
        `${action.title} requires Workspace Trust.`
          + (action.id === "code.review.start" ? " Trust this workspace before running Review Git Changes again." : "")
      );
    }
    if (action.workspaceRequired && action.id !== "code.review.start" && !workspaceRoot()) {
      throw new ProtocolClientError(
        "workspace_required",
        workspaceScopeRequiredMessage(`using ${action.title}`)
      );
    }
    const context = this.deps.activeContext();
    if (action.requiresActiveSession && !context.sessionId && !(action.id === "session.search" && this.deps.ensureLiveSession)) {
      throw new ProtocolClientError(
        "active_session_required",
        `${action.title} requires an active Alysis Code chat session.`
          + (action.id === "code.review.start" ? " Use Alysis Code: New Session or Task History to start or resume a task, then run Review Git Changes again." : "")
      );
    }
  }

  private mcpOAuthUnavailableReason(): string | undefined {
    return mcpOAuthRemoteUnavailableReason(this.deps.remoteName?.() ?? vscode.env.remoteName);
  }

  private guardCodeReviewContext(expected: BackendActionContext): void {
    if (!this.deps.isWorkspaceTrusted()) {
      throw new ProtocolClientError("workspace_trust_required", "Trust this workspace before running Review Git Changes again.");
    }
    const current = this.deps.activeContext();
    const reviewWorkspace = expected.workspaceRoot;
    if (!reviewWorkspace) {
      throw new ProtocolClientError("workspace_required", "The active session has no workspace. Use Alysis Code: New Session or Task History to open a workspace-bound task before reviewing.");
    }
    const workspaceStillOpen = vscode.workspace.workspaceFolders?.some((folder) =>
      path.relative(path.resolve(folder.uri.fsPath), path.resolve(reviewWorkspace)) === ""
    );
    if (!expected.sessionId || current.sessionId !== expected.sessionId
      || current.workspaceRoot !== expected.workspaceRoot || !workspaceStillOpen) {
      throw new ProtocolClientError("review_context_changed", "The active session or its workspace changed. Run Review Git Changes again from the intended task.");
    }
  }

  private guardActionSupport(action: BackendActionMetadata): void {
    if (!this.isActionSupported(action)) {
      const capabilityPath = action.capabilityPath;
      const explicitlyUnsupported =
        capabilityPath !== undefined && this.deps.bridge.featureValue(capabilityPath) === false;
      const unsupportedCapability = capabilityPath?.join(".") ?? "the required capability";
      throw new ProtocolClientError(
        "backend_action_unsupported",
        explicitlyUnsupported
          ? `${action.title} is unavailable because the current Alysis Code bridge reports ${unsupportedCapability} as unsupported.`
          : `${action.title} is unavailable because the current Alysis Code bridge does not advertise ${action.requiredMethods.join(", ")}.`
            + (action.id === "code.review.start" ? " Update the Alysis Code CLI and reconnect, then try again." : "")
      );
    }
  }

  private async collectParams(
    action: BackendActionMetadata,
    args: string
  ): Promise<Record<string, unknown> | undefined> {
    const context = this.deps.activeContext();
    const workspace = workspaceRoot();
    const trusted = this.deps.isWorkspaceTrusted();
    const raw = args.trim();
    const baseWorkspace = { workspace, path: workspace };
    switch (action.id) {
      case "session.status":
      case "session.context":
        return { session_id: requireActiveSession(context) };
      case "session.modelInfo":
        return {
          session_id: requireActiveSession(context),
          model: raw || undefined
        };
      case "session.subagents.status":
        return subagentParams(raw, requireActiveSession(context), trusted, (method) =>
          this.deps.bridge.supportsMethod(method as Parameters<AlysisBridgeClient["supportsMethod"]>[0])
        );
      case "session.subagents.setEnabled":
        return {
          session_id: requireActiveSession(context),
          enabled: subagentToggleValue(await inputOrArg(raw, "Subagent toggle: on or off")),
          workspace_trusted: trusted
        };
      case "session.trace.status":
        return traceParams(raw, requireActiveSession(context), false);
      case "session.trace.setLevel":
        return traceParams(await inputOrArg(raw, "Trace level: off, compact, or full"), requireActiveSession(context), false);
      case "session.trace.listEvents":
        return { session_id: requireActiveSession(context) };
      case "session.trace.readArtifact":
        return { session_id: requireActiveSession(context), artifact_id: await inputOrArg(raw, "Trace artifact id") };
      case "session.trace.clear":
        return { session_id: requireActiveSession(context) };
      case "session.terminals.list":
        return terminalParams(raw, requireActiveSession(context), trusted);
      case "session.terminals.show":
        return { session_id: requireActiveSession(context), process_id: await inputOrArg(raw, "Terminal process id") };
      case "session.terminals.kill":
      case "session.terminals.clear":
        return {
          session_id: requireActiveSession(context),
          process_id: await inputOrArg(raw, "Terminal process id"),
          workspace_trusted: trusted,
          confirm: true
        };
      case "session.usage":
        return { session_id: raw || context.sessionId || (await inputOrArg("", "Session id")) };
      case "session.history":
        return { session_id: requireActiveSession(context), pattern: await inputOrArg(raw, "Search session history") };
      case "session.compact":
        return { session_id: requireActiveSession(context), focus: raw || undefined };
      case "session.resume": {
        const targetSessionId = await inputOrArg(raw, "Retained session id to resume");
        return {
          session_id: context.sessionId || (await this.ensureLiveSessionForAction(action)),
          target_session_id: targetSessionId
        };
      }
      case "session.images.list":
        return { session_id: requireActiveSession(context) };
      case "session.images.add":
        return {
          session_id: requireActiveSession(context),
          images: [workspaceScopedPath(await imagePathOrArg(raw), workspace, "Session image path")]
        };
      case "session.images.clear":
        return { session_id: requireActiveSession(context) };
      case "session.setMode": {
        const mode = sessionModeParam(await inputOrArg(raw, "Session permissions: readonly, review, or auto"));
        assertSessionModeTrust(mode, trusted);
        return { session_id: requireActiveSession(context), mode };
      }
      case "session.setModel":
        return { session_id: requireActiveSession(context), model: await inputOrArg(raw, "Model name") };
      case "session.setStream":
        return { session_id: requireActiveSession(context), stream: streamValue(raw) ?? (await pickStream()) };
      case "session.setActiveWorkdir":
        return {
          session_id: requireActiveSession(context),
          path: workspaceScopedPath(
            await inputOrArg(raw, "Workspace-relative path"),
            workspace,
            "Active workdir"
          )
        };
      case "session.clear":
        return { session_id: requireActiveSession(context) };
      case "session.show":
        return { session_id: raw || context.sessionId || (await inputOrArg("", "Session id")) };
      case "session.score":
        return raw ? { session_id: raw } : { latest: 1 };
      case "session.search":
        return {
          session_id: context.sessionId ?? await this.requireLiveSession(),
          query: await inputOrArg(raw, "Search past Alysis Code tasks"),
          max_results: 25,
          max_sessions: 50
        };
      case "chat.queue.list":
        return { session_id: requireActiveSession(context), limit: 100 };
      case "chat.queue.get":
      case "chat.queue.delete":
        return {
          session_id: requireActiveSession(context),
          prompt_id: opaqueIdentifier(await inputOrArg(raw, "Queued message id"), "Queued message id")
        };
      case "checkpoint.list":
        return { session_id: requireActiveSession(context), limit: 100 };
      case "checkpoint.diff":
        return {
          session_id: requireActiveSession(context),
          checkpoint_id: opaqueIdentifier(await inputOrArg(raw, "Checkpoint id"), "Checkpoint id"),
          max_bytes: 128_000
        };
      case "checkpoint.revert":
        return {
          session_id: requireActiveSession(context),
          checkpoint_id: opaqueIdentifier(await inputOrArg(raw, "Checkpoint id"), "Checkpoint id"),
          workspace_trusted: trusted,
          confirm: true
        };
      case "checkpoint.redo":
        return {
          session_id: requireActiveSession(context),
          workspace_trusted: trusted,
          confirm: true
        };
      case "checkpoint.branch":
        return checkpointBranchParams(raw, requireActiveSession(context));
      case "code.review.start":
        return {
          ...(await codeReviewStartParams(raw, requireActiveSession(context), trusted)),
          ide_workspace_root: requireCodeReviewWorkspace(context.workspaceRoot)
        };
      case "code.review.result":
        return {
          job_id: opaqueIdentifier(await inputOrArg(raw, "Code review job id"), "Code review job id"),
          // Host-only correlation fence. invoke() never forwards this field.
          expected_session_id: requireActiveSession(context),
          ide_workspace_root: requireCodeReviewWorkspace(context.workspaceRoot ?? workspace)
        };
      case "report.create":
        return { ...baseWorkspace, workspace_trusted: trusted, latest: true, feedback: raw || undefined };

      case "config.get":
      case "config.schema":
        return {};
      case "config.validate":
        return { ...baseWorkspace };
      case "config.set":
        return this.configSetParams(raw, trusted);
      case "mcp.auth.login.start":
        return {
          ...baseWorkspace,
          workspace_trusted: trusted,
          server_id: await inputOrArg(raw, "MCP server id")
        };
      case "profile.list":
      case "profile.presets":
        return {};
      case "profile.show":
        return { name: await inputOrArg(raw, "Profile name") };
      case "profile.use":
      case "profile.remove":
        return { name: await inputOrArg(raw, "Profile name"), workspace_trusted: trusted, yes: true };
      case "profile.rename": {
        const current = await inputOrArg(raw, "Current profile name");
        const next = await inputOrArg("", "New profile name");
        return { old: current, new: next, workspace_trusted: trusted };
      }
      case "profile.add":
        return {
          name: await inputOrArg(raw, "Profile name"),
          base_url: await inputOrArg("", "Provider base URL"),
          default_model: (await optionalInput("Default model (leave blank to inherit)")) || undefined,
          workspace_trusted: trusted
        };
      case "profile.preset":
        return { preset: await inputOrArg(raw, "Preset key"), name: await optionalInput("Profile name (optional)"), workspace_trusted: trusted, yes: true };
      case "profile.convert":
        return { name: await inputOrArg(raw, "Profile name"), target: await inputOrArg("", "Target preset key"), workspace_trusted: trusted, yes: true };

      case "tools.catalog":
        return {};
      case "tool.list":
      case "skill.list":
      case "mcp.status":
      case "mcp.prompts.list":
      case "mcp.auth.status":
      case "hooks.list":
      case "hooks.doctor":
      case "conventions.list":
      case "ext.list":
        return { ...baseWorkspace };
      case "conventions.render":
        return raw ? { ...baseWorkspace, focus_path: raw } : { ...baseWorkspace };
      case "tool.info":
      case "tool.trust":
      case "tool.untrust":
        return { ...baseWorkspace, name: await inputOrArg(raw, "Tool name"), workspace_trusted: trusted };
      case "skill.info":
      case "skill.enable":
      case "skill.disable":
      case "skill.remove":
        return { ...baseWorkspace, name: await inputOrArg(raw, "Skill name"), workspace_trusted: trusted };
      case "skill.validate":
        return { ...baseWorkspace, ...await skillValidateSelector(raw) };
      case "skill.init":
        return { ...baseWorkspace, name: await inputOrArg(raw, "Skill name"), workspace_trusted: trusted, project: true };
      case "skill.install": {
        const source = await inputOrArg(raw, "Skill source path/URL");
        return {
          ...baseWorkspace,
          source,
          workspace_trusted: trusted,
          project: true,
          allow_remote: looksLikeRemoteSource(source) || undefined,
          yes: looksLikeRemoteSource(source) || undefined
        };
      }
      case "permission.rules.list":
        return {};
      case "permission.rules.grant":
        return permissionRuleParams(raw);
      case "permission.rules.revoke":
        return { rule_id: opaqueIdentifier(await inputOrArg(raw, "Permission rule id"), "Permission rule id") };
      case "permission.evaluate":
        return permissionEvaluationParams(raw, workspace);
      case "permission.session.list":
        return { session_id: requireActiveSession(context) };
      case "permission.session.revoke":
        return {
          session_id: requireActiveSession(context),
          grant_id: opaqueIdentifier(await inputOrArg(raw, "Task permission grant id"), "Task permission grant id")
        };

      case "mcp.prompts.get":
        return { ...baseWorkspace, server_id: await inputOrArg(raw, "MCP server id"), name: await inputOrArg("", "Prompt name") };
      case "mcp.auth.logout":
        return { ...baseWorkspace, server_id: await inputOrArg(raw, "MCP server id"), workspace_trusted: trusted, yes: true };
      case "hooks.effective":
      case "hooks.test":
        return { ...baseWorkspace, event: raw || (await optionalInput("Hook event name (optional)")) || undefined };
      case "hooks.trace":
        return { session_id: raw || context.sessionId || undefined };
      case "hooks.trust":
      case "hooks.untrust":
        return { ...baseWorkspace, target: "project_config", workspace_trusted: trusted };
      case "hooks.init":
        return { ...baseWorkspace, workspace_trusted: trusted };
      case "hooks.enable":
      case "hooks.disable":
        return { ...baseWorkspace, hook_id: await inputOrArg(raw, "Hook id"), layer: "project", workspace_trusted: trusted };

      case "ext.search":
        return { query: await inputOrArg(raw, "Extension search query") };
      case "ext.info":
        return { ...baseWorkspace, plugin_id: await inputOrArg(raw, "Extension id") };
      case "ext.uninstall":
      case "ext.enable":
      case "ext.disable":
        return { ...baseWorkspace, plugin_id: await inputOrArg(raw, "Extension id"), workspace_trusted: trusted, yes: true };
      case "ext.install":
        return { ...baseWorkspace, source: await inputOrArg(raw, "Extension source path/URL"), workspace_trusted: trusted, project: true };

      case "doctor.summary":
      case "doctor.providers":
      case "doctor.bundle":
        return {};
      case "sandbox.doctor":
        return { smoke: true, include_server: false };
      case "sandbox.setup":
        return { workspace_trusted: trusted, pull: false };
      case "sandbox.pull":
        return sandboxPullParams(raw, trusted);
      case "update.check":
        return updateCheckParams(raw);

      default:
        throw new Error(`No parameter collector exists for ${action.id}.`);
    }
  }

  private async requireLiveSession(): Promise<string> {
    if (!this.deps.ensureLiveSession) {
      throw new ProtocolClientError(
        "session_required",
        "Start or reopen an Alysis Code task before using past-task search."
      );
    }
    return this.deps.ensureLiveSession();
  }

  private async ensureLiveSessionForAction(action: BackendActionMetadata): Promise<string> {
    if (!this.deps.ensureLiveSession) {
      throw new ProtocolClientError(
        "active_session_required",
        `${action.title} requires an active Alysis Code session. Open Alysis Code or start a session first.`
      );
    }
    const sessionId = (await this.deps.ensureLiveSession()).trim();
    if (!sessionId) {
      throw new ProtocolClientError(
        "active_session_required",
        `${action.title} could not create a live Alysis Code session.`
      );
    }
    return sessionId;
  }

  private async confirmAction(title: string, actionMutates: boolean): Promise<boolean> {
    const selection = await vscode.window.showWarningMessage(
      actionMutates
        ? `"${title}" can make changes. Continue?`
        : `Run "${title}"?`,
      { modal: true },
      "Continue"
    );
    return selection === "Continue";
  }

  private async showCommandError(prefix: string, error: unknown): Promise<void> {
    const detail = redactForDisplay(error instanceof Error ? error.message : String(error));
    this.deps.output.appendLine("");
    this.deps.output.appendLine(`## ${prefix}`);
    this.deps.output.appendLine(detail);
    void vscode.window.showWarningMessage(`${prefix}. ${detail}`);
  }

  private async invoke(action: BackendActionMetadata, params: Record<string, unknown>): Promise<unknown> {
    const bridge = this.deps.bridge;
    switch (action.id) {
      case "session.status":
        return bridge.sessionStatus(String(params.session_id));
      case "session.modelInfo":
        return bridge.sessionModelInfo(String(params.session_id), { model: stringOrUndefined(params.model) });
      case "session.subagents.status":
        if (typeof params.enabled === "boolean") {
          return bridge.sessionSubagentsSetEnabled(
            String(params.session_id),
            params.enabled,
            params.workspace_trusted === true
          );
        }
        return bridge.sessionSubagentsStatus(String(params.session_id));
      case "session.subagents.setEnabled":
        return bridge.sessionSubagentsSetEnabled(
          String(params.session_id),
          Boolean(params.enabled),
          params.workspace_trusted === true
        );
      case "session.trace.status":
        if (params.level) {
          return bridge.sessionTraceSetLevel(
            String(params.session_id),
            traceLevelParam(params.level),
            { confirm: params.confirm === true, yes: params.yes === true }
          );
        }
        if (params.operation === "events") {
          return bridge.sessionTraceListEvents(String(params.session_id));
        }
        if (params.operation === "clear") {
          return bridge.sessionTraceClear(String(params.session_id));
        }
        return bridge.sessionTraceStatus(String(params.session_id));
      case "session.trace.setLevel":
        return bridge.sessionTraceSetLevel(
          String(params.session_id),
          traceLevelParam(params.level),
          { confirm: params.confirm === true, yes: params.yes === true }
        );
      case "session.trace.listEvents":
        return bridge.sessionTraceListEvents(String(params.session_id));
      case "session.trace.readArtifact":
        return bridge.sessionTraceReadArtifact(String(params.session_id), String(params.artifact_id));
      case "session.trace.clear":
        return bridge.sessionTraceClear(String(params.session_id));
      case "session.terminals.list":
        if (params.operation === "show") {
          return bridge.sessionTerminalsShow(String(params.session_id), String(params.process_id));
        }
        if (params.operation === "kill") {
          return bridge.sessionTerminalsKill(
            String(params.session_id),
            String(params.process_id),
            params.workspace_trusted === true,
            { confirm: params.confirm === true, yes: params.yes === true }
          );
        }
        if (params.operation === "clear") {
          return bridge.sessionTerminalsClear(
            String(params.session_id),
            String(params.process_id),
            params.workspace_trusted === true,
            { confirm: params.confirm === true, yes: params.yes === true }
          );
        }
        return bridge.sessionTerminalsList(String(params.session_id));
      case "session.terminals.show":
        return bridge.sessionTerminalsShow(String(params.session_id), String(params.process_id));
      case "session.terminals.kill":
        return bridge.sessionTerminalsKill(
          String(params.session_id),
          String(params.process_id),
          params.workspace_trusted === true,
          { confirm: params.confirm === true, yes: params.yes === true }
        );
      case "session.terminals.clear":
        return bridge.sessionTerminalsClear(
          String(params.session_id),
          String(params.process_id),
          params.workspace_trusted === true,
          { confirm: params.confirm === true, yes: params.yes === true }
        );
      case "session.usage":
        return bridge.sessionUsage(String(params.session_id));
      case "session.history":
        return bridge.sessionHistory(String(params.session_id), String(params.pattern));
      case "session.context":
        return bridge.sessionContext(String(params.session_id));
      case "session.compact":
        return bridge.sessionCompact(String(params.session_id), stringOrUndefined(params.focus));
      case "session.resume":
        if (this.deps.resumeSession) {
          return this.deps.resumeSession(String(params.session_id), String(params.target_session_id));
        }
        return bridge.sessionResume(String(params.session_id), String(params.target_session_id));
      case "session.images.list":
        return bridge.sessionImagesList(String(params.session_id));
      case "session.images.add":
        return bridge.sessionImagesAdd(params as never);
      case "session.images.clear":
        return bridge.sessionImagesClear(String(params.session_id));
      case "session.setMode":
        return bridge.sessionSetMode(String(params.session_id), sessionModeParam(params.mode));
      case "session.setModel":
        return bridge.sessionSetModel(String(params.session_id), String(params.model));
      case "session.setStream":
        return bridge.sessionSetStream(String(params.session_id), Boolean(params.stream));
      case "session.setActiveWorkdir":
        return bridge.sessionSetActiveWorkdir(String(params.session_id), String(params.path));
      case "session.clear":
        return bridge.sessionClear(String(params.session_id));
      case "session.show":
        return bridge.sessionShow(params as never);
      case "session.score":
        return bridge.sessionScore(params);
      case "session.search":
        return bridge.sessionSearch(
          String(params.session_id),
          String(params.query),
          { max_results: Number(params.max_results), max_sessions: Number(params.max_sessions) }
        );
      case "chat.queue.list":
        return bridge.chatQueueList(String(params.session_id), { limit: Number(params.limit) });
      case "chat.queue.get":
        return bridge.chatQueueGet(String(params.session_id), String(params.prompt_id));
      case "chat.queue.delete":
        return bridge.chatQueueDelete(String(params.session_id), String(params.prompt_id));
      case "checkpoint.list":
        return bridge.checkpointList(String(params.session_id), Number(params.limit));
      case "checkpoint.diff":
        return bridge.checkpointDiff(
          String(params.session_id),
          String(params.checkpoint_id),
          Number(params.max_bytes)
        );
      case "checkpoint.revert":
        return bridge.checkpointRevert(String(params.session_id), String(params.checkpoint_id));
      case "checkpoint.redo":
        return bridge.checkpointRedo(String(params.session_id));
      case "checkpoint.branch":
        return bridge.checkpointBranch(
          String(params.session_id),
          String(params.checkpoint_id),
          String(params.name)
        );
      case "code.review.start": {
        const reviewParams = { ...params };
        delete reviewParams.ide_workspace_root;
        return this.deps.codeReviewPresenter
          ? this.deps.codeReviewPresenter.run(reviewParams as never, String(params.ide_workspace_root))
          : bridge.codeReviewStart(reviewParams as never);
      }
      case "code.review.result": {
        const result = await bridge.codeReviewResult(String(params.job_id));
        return this.deps.codeReviewPresenter
          ? this.deps.codeReviewPresenter.present(
              result,
              String(params.expected_session_id),
              String(params.ide_workspace_root)
            )
          : result;
      }
      case "report.create":
        return bridge.reportCreate(params as never);
      case "config.get":
        return bridge.configGet();
      case "config.schema":
        return bridge.configSchema();
      case "config.validate":
        return bridge.configValidate(params);
      case "config.set":
        return bridge.configSet(params as never);
      case "mcp.auth.login.start":
        return bridge.mcpAuthLoginStart(params as never);
      case "profile.list":
        return bridge.profileList();
      case "profile.show":
        return bridge.profileShow(params as never);
      case "profile.use":
        if (this.deps.useProfile) return this.deps.useProfile(String(params.name));
        return bridge.profileUse(params as never);
      case "profile.presets":
        return bridge.profilePresets();
      case "profile.preset":
        return bridge.profilePreset(params as never);
      case "profile.convert":
        return bridge.profileConvert(params as never);
      case "profile.add":
        return bridge.profileAdd(params as never);
      case "profile.remove":
        return bridge.profileRemove(params as never);
      case "profile.rename":
        return bridge.profileRename(params as never);
      case "tools.catalog":
        return bridge.toolsCatalog();
      case "tool.list":
        return bridge.toolList(params);
      case "tool.info":
        return bridge.toolInfo(params as never);
      case "tool.trust":
        return bridge.toolTrust(params as never);
      case "tool.untrust":
        return bridge.toolUntrust(params as never);
      case "skill.list":
        return bridge.skillList(params);
      case "skill.info":
        return bridge.skillInfo(params as never);
      case "skill.validate":
        return bridge.skillValidate(params);
      case "skill.init":
        return bridge.skillInit(params as never);
      case "skill.install":
        return bridge.skillInstall(params as never);
      case "skill.enable":
        return bridge.skillEnable(params as never);
      case "skill.disable":
        return bridge.skillDisable(params as never);
      case "skill.remove":
        return bridge.skillRemove(params as never);
      case "permission.rules.list":
        return bridge.permissionRulesList();
      case "permission.rules.grant":
        return bridge.permissionRuleGrant(params as never);
      case "permission.rules.revoke":
        return bridge.permissionRuleRevoke(String(params.rule_id));
      case "permission.evaluate":
        return bridge.permissionEvaluate(params as never);
      case "permission.session.list":
        return bridge.permissionSessionList(String(params.session_id));
      case "permission.session.revoke":
        return bridge.permissionSessionRevoke(String(params.session_id), String(params.grant_id));
      case "mcp.status":
        return bridge.mcpStatus(params as never);
      case "mcp.prompts.list":
        return bridge.mcpPromptsList(params as never);
      case "mcp.prompts.get":
        return bridge.mcpPromptsGet(params as never);
      case "mcp.auth.status":
        return bridge.mcpAuthStatus(params as never);
      case "mcp.auth.logout":
        return bridge.mcpAuthLogout(params as never);
      case "hooks.list":
        return bridge.hooksList(params as never);
      case "hooks.doctor":
        return bridge.hooksDoctor(params as never);
      case "hooks.effective":
        return bridge.hooksEffective(params as never);
      case "hooks.test":
        return bridge.hooksTest(params as never);
      case "hooks.trace":
        return bridge.hooksTrace(params);
      case "hooks.trust":
        return bridge.hooksTrust(params as never);
      case "hooks.untrust":
        return bridge.hooksUntrust(params as never);
      case "hooks.init":
        return bridge.hooksInit(params as never);
      case "hooks.enable":
        return bridge.hooksEnable(params as never);
      case "hooks.disable":
        return bridge.hooksDisable(params as never);
      case "conventions.list":
        return bridge.conventionsList(params);
      case "conventions.render":
        return bridge.conventionsRender(params);
      case "ext.search":
        return bridge.extSearch(params as never);
      case "ext.list":
        return bridge.extList(params);
      case "ext.info":
        return bridge.extInfo(params as never);
      case "ext.install":
        return bridge.extInstall(params as never);
      case "ext.uninstall":
        return bridge.extUninstall(params as never);
      case "ext.enable":
        return bridge.extEnable(params as never);
      case "ext.disable":
        return bridge.extDisable(params as never);
      case "doctor.summary":
        return bridge.doctorSummary();
      case "doctor.providers":
        return bridge.doctorProviders();
      case "doctor.bundle":
        return bridge.doctorBundle();
      case "sandbox.doctor":
        return bridge.sandboxDoctor(params);
      case "sandbox.setup":
        return bridge.sandboxSetup(params as never);
      case "sandbox.pull":
        return bridge.sandboxPull(params as never);
      case "update.check":
        return bridge.updateCheck(params);
      default:
        throw new Error(`No backend invoker exists for ${action.id}.`);
    }
  }

  private async configSetParams(raw: string, trusted: boolean): Promise<Record<string, unknown>> {
    const parsed = parseConfigSetArg(raw);
    const key = parsed?.key ?? (await this.pickConfigKey());
    if (isSecretLikeConfigKey(key)) {
      throw new ProtocolClientError(
        "inline_secret_rejected",
        "Secret-bearing config keys cannot be set through Alysis Code backend actions. Use Alysis Code: Configure Provider or VS Code SecretStorage for provider credentials."
      );
    }
    const rawValue = parsed?.value ?? (await inputOrArg("", `Value for ${key}`));
    if (isSecretLikeConfigValue(rawValue)) {
      throw new ProtocolClientError(
        "inline_secret_rejected",
        "Secret-looking config values cannot be sent through IDE protocol params. Use Alysis Code: Configure Provider or VS Code SecretStorage for provider credentials."
      );
    }
    return { key, value: parseConfigValue(rawValue), workspace_trusted: trusted };
  }

  private async pickConfigKey(): Promise<string> {
    const schemaResult = await this.deps.bridge.configSchema();
    const keys = configSchemaEditableKeys(schemaResult);
    if (keys.length === 0) {
      return inputOrArg("", "Config key");
    }
    const picked = await vscode.window.showQuickPick(
      keys.map((key) => ({ label: key })),
      { title: "Alysis Code config key", ignoreFocusOut: true }
    );
    if (!picked) {
      throw new ProtocolClientError("input_cancelled", "Config key selection was cancelled.");
    }
    return picked.label.trim();
  }

  private async handleStructuredAction(
    action: BackendActionMetadata,
    params: Record<string, unknown>,
    result: unknown
  ): Promise<unknown> {
    if (action.id === "mcp.auth.login.start" && isRecord(result)) {
      return this.completeMcpOAuthLogin(params, result);
    }
    if (action.id === "session.search" && isRecord(result)) {
      return this.handleSessionSearchResult(result);
    }
    if (action.id !== "ext.install" || !isRecord(result)) {
      return result;
    }
    const nestedAction = isRecord(result.action) ? result.action : undefined;
    const requiredApproval = isRecord(nestedAction?.required_approval)
      ? nestedAction.required_approval
      : undefined;
    if (nestedAction?.kind !== "requires_extension_trust_review" || !requiredApproval) {
      return result;
    }
    const selection = await vscode.window.showWarningMessage(
      "Alysis Code extension install requires explicit package trust review. Install after reviewing the manifest/source metadata in the output channel?",
      { modal: true },
      "Install"
    );
    if (selection !== "Install") {
      return result;
    }
    return this.deps.bridge.extInstall({
      ...params,
      yes: true,
      confirm: true,
      trust_approval: requiredApproval
    } as never);
  }

  private async handleSessionSearchResult(result: Record<string, unknown>): Promise<Record<string, unknown>> {
    const rawResults = recordArray(result.results);
    const sanitizedResults = rawResults.map((item) => {
      const { context_block: _contextBlock, ...safe } = item;
      return safe;
    });
    const attachable = rawResults
      .map((item) => ({ item, block: isRecord(item.context_block) ? item.context_block : undefined }))
      .filter((entry): entry is { item: Record<string, unknown>; block: Record<string, unknown> } =>
        entry.block?.type === "past_session"
      )
      .slice(0, 25);
    if (!this.deps.attachContextBlock || attachable.length === 0) {
      return { ...result, results: sanitizedResults, attached_context: false };
    }
    const picked = await vscode.window.showQuickPick(
      attachable.map(({ item, block }, index) => ({
        label: displayValue(item.snippet, 120) || `Past-task match ${index + 1}`,
        description: displayValue(item.session_id, 80),
        detail: friendlyEventType(displayValue(item.event_type, 80)),
        block
      })),
      {
        title: "Attach a past-task match to the next message (optional)",
        placeHolder: "Press Escape to keep the search results without attaching context",
        ignoreFocusOut: true
      }
    );
    if (!picked) {
      return { ...result, results: sanitizedResults, attached_context: false };
    }
    await this.deps.attachContextBlock(picked.block);
    return {
      ...result,
      results: sanitizedResults,
      attached_context: true,
      attached_session_id: stringOrUndefined(picked.block.session_id)
    };
  }

  private async completeMcpOAuthLogin(
    params: Record<string, unknown>,
    started: Record<string, unknown>
  ): Promise<unknown> {
    const flowId = stringOrUndefined(started.flow_id);
    const serverId = stringOrUndefined(started.server_id) ?? stringOrUndefined(params.server_id);
    const browserUrl = stringOrUndefined(started.browser_url);
    if (!flowId || !serverId || !browserUrl || started.state !== "pending") {
      return started;
    }
    const uri = vscode.Uri.parse(browserUrl, true);
    if (uri.scheme !== "https") {
      throw new ProtocolClientError(
        "mcp_oauth_browser_url_rejected",
        "Alysis Code rejected a non-HTTPS MCP OAuth authorization URL."
      );
    }
    const opened = await vscode.env.openExternal(uri);
    if (!opened) {
      await this.deps.bridge.mcpAuthLoginCancel({
        workspace: String(params.workspace),
        path: stringOrUndefined(params.path),
        workspace_trusted: true,
        server_id: serverId,
        flow_id: flowId
      });
      throw new ProtocolClientError(
        "mcp_oauth_browser_open_failed",
        "VS Code could not open the MCP OAuth authorization page."
      );
    }
    return vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: `Authorizing MCP server ${serverId}`,
        cancellable: true
      },
      async (_progress, cancellation) => {
        let cancellationSent = false;
        while (true) {
          if (cancellation.isCancellationRequested) {
            if (!cancellationSent) {
              cancellationSent = true;
              const cancelled = await this.deps.bridge.mcpAuthLoginCancel({
                workspace: String(params.workspace),
                path: stringOrUndefined(params.path),
                workspace_trusted: true,
                server_id: serverId,
                flow_id: flowId
              });
              return { ...cancelled, cancelled: true };
            }
          }
          const status = await this.deps.bridge.mcpAuthLoginStatus({
            workspace: String(params.workspace),
            path: stringOrUndefined(params.path),
            server_id: serverId,
            flow_id: flowId
          });
          if (status.state !== "pending" && status.state !== "completing") {
            return status;
          }
          await oauthPollDelay(cancellation);
        }
      }
    );
  }

  private showResult(action: BackendActionMetadata, result: unknown, fromSlash: boolean): void {
    const safeResult = redactDeep(result);
    const text = formatBackendResult(action.id, safeResult);
    this.deps.output.appendLine("");
    this.deps.output.appendLine(`## ${action.title}`);
    this.deps.output.appendLine(text);
    if (!fromSlash) {
      this.deps.output.show(true);
    }
    const actionRecord = isRecord(result) && isRecord(result.action) ? result.action : undefined;
    if (actionRecord?.kind === "requires_secret_storage") {
      void vscode.window.showWarningMessage("This action requires provider secrets to be stored through Alysis Code: Configure Provider or VS Code SecretStorage.");
      return;
    }
    if (actionRecord?.kind === "requires_confirmation") {
      void vscode.window.showWarningMessage("The backend requires explicit confirmation before continuing. Review the Alysis Code output details.");
      return;
    }
    void vscode.window.showInformationMessage(
      action.id.startsWith("code.review.") && isRecord(safeResult)
        ? codeReviewNotice(safeResult)
        : `${action.title} completed.`
    );
  }
}

function backendActionsForHiddenRegistration(): BackendActionMetadata[] {
  return BACKEND_ACTION_GROUPS.flatMap((group) => backendActionsForGroup(group.id));
}

async function inputOrArg(args: string, title: string): Promise<string> {
  if (args.trim()) {
    return args.trim();
  }
  const value = await vscode.window.showInputBox({
    title,
    ignoreFocusOut: true,
    validateInput: (input) => (input.trim().length === 0 ? `${title} is required.` : undefined)
  });
  if (value === undefined) {
    throw new ProtocolClientError("input_cancelled", `${title} was cancelled.`);
  }
  return value.trim();
}

async function imagePathOrArg(args: string): Promise<string> {
  if (args.trim()) {
    return args.trim();
  }
  const picked = await vscode.window.showOpenDialog({
    title: "Attach workspace-scoped image",
    canSelectFiles: true,
    canSelectFolders: false,
    canSelectMany: false,
    defaultUri: vscode.workspace.workspaceFolders?.[0]?.uri,
    filters: {
      Images: ["png", "jpg", "jpeg", "gif", "webp"]
    }
  });
  const path = picked?.[0]?.fsPath?.trim();
  if (!path) {
    throw new ProtocolClientError("input_cancelled", "Image selection was cancelled.");
  }
  return path;
}

async function optionalInput(title: string): Promise<string | undefined> {
  const value = await vscode.window.showInputBox({ title, ignoreFocusOut: true });
  return value?.trim() || undefined;
}

function opaqueIdentifier(value: string, label: string): string {
  const trimmed = value.trim();
  if (!/^[A-Za-z0-9_.:-]{1,160}$/.test(trimmed) || trimmed.includes("..")) {
    throw new ProtocolClientError(
      "invalid_identifier",
      `${label} must be an opaque Alysis Code identifier, not a path.`
    );
  }
  return trimmed;
}

async function checkpointBranchParams(
  raw: string,
  sessionId: string
): Promise<Record<string, unknown>> {
  const [rawCheckpointId = "", ...nameParts] = raw.trim().split(/\s+/).filter(Boolean);
  const checkpointId = opaqueIdentifier(
    rawCheckpointId || (await inputOrArg("", "Checkpoint id")),
    "Checkpoint id"
  );
  const name = validateBranchName(
    nameParts.join(" ") || (await inputOrArg("", "New checkpoint branch name"))
  );
  return { session_id: sessionId, checkpoint_id: checkpointId, name };
}

function validateBranchName(value: string): string {
  const name = value.trim();
  if (
    !/^[A-Za-z0-9][A-Za-z0-9._/-]{0,159}$/.test(name) ||
    name.includes("..") ||
    name.includes("//") ||
    name.endsWith(".") ||
    name.endsWith("/") ||
    name.includes("@{")
  ) {
    throw new ProtocolClientError(
      "invalid_branch_name",
      "Branch name contains characters or sequences Git cannot safely use."
    );
  }
  return name;
}

async function codeReviewStartParams(
  raw: string,
  sessionId: string,
  trusted: boolean
): Promise<Record<string, unknown>> {
  const selected = raw.trim() || (await pickCodeReviewScope());
  const [rawScope = "", ...rest] = selected.split(/\s+/).filter(Boolean);
  const scope = rawScope.toLowerCase();
  const baseParams = { session_id: sessionId, workspace_trusted: trusted };
  switch (scope) {
    case "working_tree":
    case "working-tree":
    case "working":
      if (rest.length > 0) {
        throw new ProtocolClientError("invalid_review_scope", "Working-tree review does not accept revisions.");
      }
      return { ...baseParams, scope: "working_tree" };
    case "branch": {
      const base = revisionToken(rest.shift() || (await inputOrArg("", "Base branch or revision")));
      const head = rest.length > 0 ? revisionToken(rest.shift() ?? "") : "HEAD";
      assertNoExtraReviewArgs(rest);
      return { ...baseParams, scope, base, head };
    }
    case "commit": {
      const revision = revisionToken(rest.shift() || (await inputOrArg("", "Commit revision")));
      assertNoExtraReviewArgs(rest);
      return { ...baseParams, scope, revision };
    }
    case "range": {
      const base = revisionToken(rest.shift() || (await inputOrArg("", "Base revision")));
      const head = revisionToken(rest.shift() || (await inputOrArg("", "Head revision")));
      assertNoExtraReviewArgs(rest);
      return { ...baseParams, scope, base, head };
    }
    default:
      throw new ProtocolClientError(
        "invalid_review_scope",
        "Review scope must be working_tree, branch <base> [head], commit <revision>, or range <base> <head>."
      );
  }
}

async function pickCodeReviewScope(): Promise<string> {
  const picked = await vscode.window.showQuickPick(
    [
      { label: "Working tree", detail: "Review staged, unstaged, and untracked changes.", value: "working_tree" },
      { label: "Branch", detail: "Compare a base branch or revision with HEAD.", value: "branch" },
      { label: "Commit", detail: "Review one commit.", value: "commit" },
      { label: "Revision range", detail: "Compare an explicit base and head revision.", value: "range" }
    ],
    { title: "Code review scope", ignoreFocusOut: true, matchOnDetail: true }
  );
  if (!picked) {
    throw new ProtocolClientError("input_cancelled", "Code review selection was cancelled.");
  }
  return picked.value;
}

function revisionToken(value: string): string {
  const revision = value.trim();
  if (!revision || revision.length > 200 || revision.startsWith("-") || /[\0\r\n\s]/.test(revision)) {
    throw new ProtocolClientError("invalid_review_revision", "Git revision must be a single bounded revision token.");
  }
  return revision;
}

function assertNoExtraReviewArgs(rest: string[]): void {
  if (rest.length > 0) {
    throw new ProtocolClientError("invalid_review_scope", "Too many revisions were provided for this review scope.");
  }
}

async function permissionRuleParams(raw: string): Promise<Record<string, unknown>> {
  if (raw.trim()) {
    const match = /^(allow|ask|deny)\s+(tool|path|command)\s+(.+)$/i.exec(raw.trim());
    if (!match) {
      throw new ProtocolClientError(
        "invalid_permission_rule",
        "Use: <allow|ask|deny> <tool|path|command> <pattern>. Rules must have an explicit scope."
      );
    }
    return permissionRuleRecord(match[1], match[2], match[3]);
  }
  const effectPick = await vscode.window.showQuickPick(
    [
      { label: "Ask", detail: "Require confirmation when this rule matches.", value: "ask" },
      { label: "Deny", detail: "Block matching use.", value: "deny" },
      { label: "Allow", detail: "Allow matching use without another prompt.", value: "allow" }
    ],
    { title: "Permission rule decision", ignoreFocusOut: true, matchOnDetail: true }
  );
  if (!effectPick) {
    throw new ProtocolClientError("input_cancelled", "Permission rule selection was cancelled.");
  }
  const selectorPick = await vscode.window.showQuickPick(
    [
      { label: "Tool name pattern", detail: "Scope the decision to a tool name.", value: "tool" },
      { label: "Workspace path pattern", detail: "Scope the decision to workspace paths.", value: "path" },
      { label: "Command pattern", detail: "Scope the decision to a command pattern; the backend redacts it when listing rules.", value: "command" }
    ],
    { title: "Permission rule scope", ignoreFocusOut: true, matchOnDetail: true }
  );
  if (!selectorPick) {
    throw new ProtocolClientError("input_cancelled", "Permission rule scope selection was cancelled.");
  }
  const pattern = await inputOrArg("", `${selectorPick.label}`);
  return permissionRuleRecord(effectPick.value, selectorPick.value, pattern);
}

function permissionRuleRecord(effectValue: string, selectorValue: string, patternValue: string): Record<string, unknown> {
  const effect = effectValue.toLowerCase();
  if (effect !== "allow" && effect !== "ask" && effect !== "deny") {
    throw new ProtocolClientError("invalid_permission_rule", "Permission effect must be allow, ask, or deny.");
  }
  const pattern = boundedPolicyText(patternValue, "Permission pattern");
  if (selectorValue.toLowerCase() === "tool") {
    if (!/^[A-Za-z0-9_.:*?/-]{1,256}$/.test(pattern)) {
      throw new ProtocolClientError("invalid_permission_rule", "Tool pattern contains unsupported characters.");
    }
    return { effect, tool_pattern: pattern };
  }
  if (selectorValue.toLowerCase() === "path") {
    return { effect, path_pattern: pattern };
  }
  if (selectorValue.toLowerCase() === "command") {
    rejectInlineSecret(pattern, "Command pattern");
    return { effect, command_pattern: pattern };
  }
  throw new ProtocolClientError("invalid_permission_rule", "Permission selector must be tool, path, or command.");
}

async function permissionEvaluationParams(
  raw: string,
  workspace: string | undefined
): Promise<Record<string, unknown>> {
  let toolName: string;
  let pathText: string | undefined;
  let command: string | undefined;
  if (raw.trim()) {
    const parsed = parsePermissionEvaluationArg(raw);
    toolName = parsed.toolName;
    pathText = parsed.pathText;
    command = parsed.command;
  } else {
    toolName = await inputOrArg("", "Tool name to evaluate");
    pathText = await optionalInput("Workspace-relative paths, comma separated (optional)");
    command = await optionalInput("Command to evaluate (optional; never include credentials)");
  }
  const normalizedTool = boundedToolName(toolName);
  if (command) {
    command = boundedPolicyText(command, "Command");
    rejectInlineSecret(command, "Command");
  }
  const paths = pathText
    ?.split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 50)
    .map((item) => boundedPolicyText(item, "Path"));
  return {
    tool_name: normalizedTool,
    paths: paths && paths.length > 0 ? paths : undefined,
    command,
    workspace
  };
}

function parsePermissionEvaluationArg(raw: string): {
  toolName: string;
  pathText?: string;
  command?: string;
} {
  const trimmed = raw.trim();
  const firstSpace = trimmed.search(/\s/);
  if (firstSpace < 0) {
    return { toolName: trimmed };
  }
  const toolName = trimmed.slice(0, firstSpace);
  const tail = trimmed.slice(firstSpace).trim();
  if (/^path\s+/i.test(tail)) {
    return { toolName, pathText: tail.replace(/^path\s+/i, "") };
  }
  if (/^command\s+/i.test(tail)) {
    return { toolName, command: tail.replace(/^command\s+/i, "") };
  }
  throw new ProtocolClientError(
    "invalid_permission_evaluation",
    "Use: <tool>, <tool> path <path[,path]>, or <tool> command <command>."
  );
}

function boundedToolName(value: string): string {
  const name = value.trim();
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(name)) {
    throw new ProtocolClientError("invalid_tool_name", "Tool name must be a bounded registered tool identifier.");
  }
  return name;
}

function boundedPolicyText(value: string, label: string): string {
  const text = value.trim();
  if (!text || text.length > 2_000 || text.includes("\0") || /[\r\n]/.test(text)) {
    throw new ProtocolClientError("invalid_permission_value", `${label} must be one non-empty line of at most 2,000 characters.`);
  }
  return text;
}

function rejectInlineSecret(value: string, label: string): void {
  if (isSecretLikeConfigValue(value)) {
    throw new ProtocolClientError(
      "inline_secret_rejected",
      `${label} appears to contain a credential. Permission workflows never accept secrets.`
    );
  }
}

function actionCardProjection(actionId: string, result: unknown): unknown {
  const safe = redactDeep(result);
  if (actionId === "chat.queue.list" && isRecord(safe)) {
    const items = recordArray(safe.items);
    return {
      summary: `${items.length} queued or recent message${items.length === 1 ? "" : "s"}`,
      messages: items.slice(0, 20).map((item) => ({
        id: displayValue(item.prompt_id, 160),
        state: displayValue(item.state, 40),
        preview: displayValue(item.message_preview, 240)
      })),
      more_available: items.length > 20
    };
  }
  if (actionId === "chat.queue.get" || actionId === "chat.queue.delete") {
    const item = isRecord(safe) ? safe : {};
    return {
      summary: actionId === "chat.queue.delete" ? "Queued message updated" : "Queued message details",
      id: displayValue(item.prompt_id, 160),
      state: displayValue(item.state, 40),
      preview: displayValue(item.message_preview, 240)
    };
  }
  if (actionId === "checkpoint.list") {
    const checkpoints = Array.isArray(safe) ? safe.filter(isRecord) : [];
    return {
      summary: `${checkpoints.length} checkpoint${checkpoints.length === 1 ? "" : "s"}`,
      checkpoints: checkpoints.slice(0, 20).map((item) => ({
        id: displayValue(item.checkpoint_id, 160),
        kind: displayValue(item.kind, 60),
        message: displayValue(item.message, 240),
        files: Array.isArray(item.changes) ? item.changes.length : 0
      })),
      more_available: checkpoints.length > 20
    };
  }
  if (actionId === "checkpoint.diff") {
    const item = isRecord(safe) ? safe : {};
    const changes = Array.isArray(item.changes) ? item.changes.length : undefined;
    return {
      summary: "Checkpoint change preview is available in Alysis Code Output",
      checkpoint_id: displayValue(item.checkpoint_id, 160),
      files: changes,
      truncated: item.truncated === true
    };
  }
  if (actionId.startsWith("checkpoint.")) {
    const item = isRecord(safe) ? safe : {};
    return {
      summary: checkpointActionSummary(actionId),
      checkpoint_id: displayValue(item.checkpoint_id, 160),
      ref: displayValue(item.ref, 200),
      kind: displayValue(item.kind, 60),
      files: Array.isArray(item.changes) ? item.changes.length : undefined
    };
  }
  if (actionId === "session.search" && isRecord(safe)) {
    const results = recordArray(safe.results);
    return {
      summary: `${results.length} redacted task-history match${results.length === 1 ? "" : "es"}`,
      matches: results.slice(0, 12).map((item) => ({
        session: displayValue(item.session_id, 120),
        type: displayValue(item.event_type, 80),
        snippet: displayValue(item.snippet, 240)
      })),
      truncated: safe.truncated === true || results.length > 12,
      redacted: true
    };
  }
  if (actionId === "permission.rules.list" || actionId === "permission.session.list") {
    const items = Array.isArray(safe) ? safe.filter(isRecord) : [];
    return {
      summary: `${items.length} permission ${actionId === "permission.rules.list" ? "rule" : "grant"}${items.length === 1 ? "" : "s"}`,
      items: items.slice(0, 20),
      command_patterns_redacted: actionId === "permission.rules.list"
    };
  }
  if (actionId.startsWith("permission.")) {
    const item = isRecord(safe) ? safe : {};
    return {
      summary: permissionActionSummary(actionId, safe),
      decision: displayValue(item.decision, 40),
      reason: friendlyPolicyReason(displayValue(item.reason, 160)),
      matched_rule: displayValue(item.matched_rule_id, 160),
      rule_id: displayValue(item.id, 160)
    };
  }
  if (actionId === "code.review.start") {
    const item = isRecord(safe) ? safe : {};
    if (item.complete === true || item.cancelled === true || !["started", "running", "queued"].includes(String(item.status))) {
      return codeReviewActionProjection(item);
    }
    return {
      summary: "Structured code review started",
      job_id: displayValue(item.job_id, 160),
      scope: friendlyReviewScope(displayValue(item.scope, 60)),
      status: displayValue(item.status, 60)
    };
  }
  if (actionId === "code.review.result") {
    return codeReviewActionProjection(isRecord(safe) ? safe : {});
  }
  return safe;
}

function formatBackendResult(actionId: string, safeResult: unknown): string {
  if (actionId === "chat.queue.list" && isRecord(safeResult)) {
    const items = recordArray(safeResult.items);
    return items.length === 0
      ? "No queued or recent messages."
      : [
          `${items.length} queued or recent message${items.length === 1 ? "" : "s"}:`,
          ...items.map((item) => `- ${displayValue(item.prompt_id, 160)} · ${displayValue(item.state, 40)} · ${displayValue(item.message_preview, 360) || "(preview unavailable)"}`)
        ].join("\n");
  }
  if ((actionId === "chat.queue.get" || actionId === "chat.queue.delete") && isRecord(safeResult)) {
    return [
      `Message: ${displayValue(safeResult.prompt_id, 160)}`,
      `Status: ${displayValue(safeResult.state, 40)}`,
      `Created: ${displayValue(safeResult.created_at, 80) || "(unknown)"}`,
      `Preview: ${displayValue(safeResult.message_preview, 2_000) || "(unavailable)"}`
    ].join("\n");
  }
  if (actionId === "checkpoint.list") {
    const checkpoints = Array.isArray(safeResult) ? safeResult.filter(isRecord) : [];
    return checkpoints.length === 0
      ? "No recoverable checkpoints for this task."
      : [
          `${checkpoints.length} checkpoint${checkpoints.length === 1 ? "" : "s"}:`,
          ...checkpoints.map((item) => {
            const fileCount = Array.isArray(item.changes) ? item.changes.length : 0;
            return `- ${displayValue(item.checkpoint_id, 160)} · ${displayValue(item.kind, 60)} · ${fileCount} file${fileCount === 1 ? "" : "s"} · ${displayValue(item.message, 300)}`;
          })
        ].join("\n");
  }
  if (actionId === "checkpoint.diff" && isRecord(safeResult)) {
    const diff = displayValue(safeResult.diff ?? safeResult.unified_diff, 128_000);
    const heading = [
      `Checkpoint: ${displayValue(safeResult.checkpoint_id, 160) || "(selected checkpoint)"}`,
      `Preview${safeResult.truncated === true ? " (truncated by backend)" : ""}:`
    ].join("\n");
    return diff ? `${heading}\n\n${diff}` : `${heading}\n\nNo textual changes were returned.`;
  }
  if (actionId.startsWith("checkpoint.")) {
    const item = isRecord(safeResult) ? safeResult : {};
    return [
      checkpointActionSummary(actionId),
      item.checkpoint_id ? `Checkpoint: ${displayValue(item.checkpoint_id, 160)}` : "",
      item.ref ? `Git reference: ${displayValue(item.ref, 200)}` : "",
      Array.isArray(item.changes) ? `Files: ${item.changes.length}` : ""
    ].filter(Boolean).join("\n");
  }
  if (actionId === "session.search" && isRecord(safeResult)) {
    const results = recordArray(safeResult.results);
    return results.length === 0
      ? "No matching past-task events were found in this workspace."
      : [
          `${results.length} redacted match${results.length === 1 ? "" : "es"}${safeResult.truncated === true ? " (truncated)" : ""}:`,
          ...results.map((item) => `- ${displayValue(item.session_id, 120)} · ${friendlyEventType(displayValue(item.event_type, 80))}\n  ${displayValue(item.snippet, 1_000)}`),
          "",
          "Search results are workspace-scoped, bounded, and redacted."
        ].join("\n");
  }
  if (actionId === "permission.rules.list") {
    const rules = Array.isArray(safeResult) ? safeResult.filter(isRecord) : [];
    return rules.length === 0
      ? "No persistent permission rules."
      : [
          `${rules.length} persistent permission rule${rules.length === 1 ? "" : "s"}:`,
          ...rules.map((rule) => `- ${displayValue(rule.id, 160)} · ${displayValue(rule.effect, 40).toUpperCase()} · ${permissionRuleScope(rule)}`),
          "",
          "Command patterns are intentionally hidden."
        ].join("\n");
  }
  if (actionId === "permission.session.list") {
    const grants = Array.isArray(safeResult) ? safeResult.filter(isRecord) : [];
    return grants.length === 0
      ? "No temporary permission grants for this task."
      : [
          `${grants.length} temporary task grant${grants.length === 1 ? "" : "s"}:`,
          ...grants.map((grant) => `- ${displayValue(grant.id, 160)} · ${displayValue(grant.kind, 80)} · ${displayValue(grant.scope_type, 100)} · ${displayValue(grant.source, 100)}`)
        ].join("\n");
  }
  if (actionId.startsWith("permission.")) {
    const item = isRecord(safeResult) ? safeResult : {};
    return [
      permissionActionSummary(actionId, safeResult),
      item.decision ? `Decision: ${displayValue(item.decision, 40).toUpperCase()}` : "",
      item.reason ? `Reason: ${friendlyPolicyReason(displayValue(item.reason, 160))}` : "",
      item.matched_rule_id ? `Matched rule: ${displayValue(item.matched_rule_id, 160)}` : "",
      item.id ? `Rule: ${displayValue(item.id, 160)}` : ""
    ].filter(Boolean).join("\n");
  }
  if (actionId === "code.review.start" && isRecord(safeResult)) {
    if (
      safeResult.complete === true
      || safeResult.cancelled === true
      || !["started", "running", "queued"].includes(String(safeResult.status))
    ) {
      return formatCodeReviewResult(safeResult);
    }
    return [
      "Structured code review started.",
      `Job: ${displayValue(safeResult.job_id, 160)}`,
      `Scope: ${friendlyReviewScope(displayValue(safeResult.scope, 60))}`,
      "Use Code Review Results with this job id after the review completes."
    ].join("\n");
  }
  if (actionId === "code.review.result" && isRecord(safeResult)) {
    return formatCodeReviewResult(safeResult);
  }
  return JSON.stringify(safeResult, null, 2) ?? String(safeResult);
}

function codeReviewActionProjection(item: Record<string, unknown>): Record<string, unknown> {
  const findings = recordArray(item.findings);
  const summary = isRecord(item.summary) ? item.summary : {};
  return {
    summary: isCompletedCodeReview(item)
      ? displayValue(summary.overview, 360) || codeReviewNotice(item)
      : codeReviewNotice(item),
    status: displayValue(item.status, 60),
    verdict: displayValue(summary.verdict, 60),
    findings: findings.slice(0, 12).map((finding) => ({
      severity: displayValue(finding.severity, 40),
      title: displayValue(finding.title, 160),
      location: reviewLocation(finding)
    })),
    truncated: summary.truncated === true || findings.length > 12,
    redacted: true,
    published_to_problems: isCompletedCodeReview(item)
  };
}

function isCompletedCodeReview(result: Record<string, unknown>): boolean {
  return result.cancelled !== true && result.status === "completed" && result.complete !== false && Array.isArray(result.findings);
}

function codeReviewNotice(result: Record<string, unknown>): string {
  if (result.cancelled === true || ["cancelled", "canceled"].includes(String(result.status))) {
    return "Structured code review cancelled.";
  }
  if (isCompletedCodeReview(result)) {
    const count = recordArray(result.findings).length;
    const limited = isRecord(result.summary) && result.summary.truncated === true;
    return `Structured code review completed with ${count} finding${count === 1 ? "" : "s"} available.`
      + (limited ? " Results were limited; see Alysis Code Output for details." : "");
  }
  if (["started", "running", "queued"].includes(String(result.status))) {
    return `Structured code review ${result.status}.`;
  }
  return "Structured code review results unavailable; review did not complete.";
}

function formatCodeReviewResult(result: Record<string, unknown>): string {
  const findings = recordArray(result.findings);
  const summary = isRecord(result.summary) ? result.summary : {};
  const lines = [
    `Status: ${displayValue(result.status, 60) || "unknown"}`,
    `Scope: ${friendlyReviewScope(displayValue(result.scope, 60))}`
  ];
  if (!isCompletedCodeReview(result)) {
    return [...lines, codeReviewNotice(result), "Findings are unavailable because the review did not complete."].join("\n");
  }
  if (summary.verdict) {
    lines.push(`Verdict: ${displayValue(summary.verdict, 60).replace(/_/g, " ").toUpperCase()}`);
  }
  if (summary.overview) {
    lines.push(`Summary: ${displayValue(summary.overview, 2_000)}`);
  }
  lines.push(`Findings: ${findings.length}`);
  for (const finding of findings) {
    lines.push("");
    lines.push(`[${displayValue(finding.severity, 40).toUpperCase()}] ${displayValue(finding.title, 300)}`);
    lines.push(`Location: ${reviewLocation(finding)}`);
    if (finding.explanation) {
      lines.push(`Why it matters: ${displayValue(finding.explanation, 3_000)}`);
    }
    if (finding.evidence) {
      lines.push(`Evidence: ${displayValue(finding.evidence, 3_000)}`);
    }
    if (finding.suggested_fix) {
      lines.push(`Suggested fix: ${displayValue(finding.suggested_fix, 3_000)}`);
    }
    if (finding.confidence) {
      lines.push(`Confidence: ${displayValue(finding.confidence, 40)}`);
    }
  }
  if (summary.truncated === true) {
    lines.push("", "The backend bounded this review result; some details were omitted.");
  }
  lines.push("", "Review output is structured, bounded, and redacted.");
  return lines.join("\n");
}

function checkpointActionSummary(actionId: string): string {
  switch (actionId) {
    case "checkpoint.revert":
      return "Workspace restored to the selected checkpoint.";
    case "checkpoint.redo":
      return "The most recently reverted checkpoint was reapplied.";
    case "checkpoint.branch":
      return "Checkpoint Git reference created.";
    default:
      return "Checkpoint operation completed.";
  }
}

function permissionActionSummary(actionId: string, result: unknown): string {
  const record = isRecord(result) ? result : {};
  switch (actionId) {
    case "permission.rules.grant":
      return "Scoped permission rule added.";
    case "permission.rules.revoke":
      return result === true || record.status === "revoked" ? "Permission rule removed." : "Permission rule removal completed.";
    case "permission.session.revoke":
      return result === true || record.status === "revoked" ? "Temporary task permission revoked." : "Task permission update completed.";
    case "permission.evaluate":
      return "Permission decision evaluated without running the tool.";
    default:
      return "Permission operation completed.";
  }
}

function permissionRuleScope(rule: Record<string, unknown>): string {
  const parts = [
    rule.tool_pattern ? `tool ${displayValue(rule.tool_pattern, 200)}` : "",
    rule.path_pattern ? `path ${displayValue(rule.path_pattern, 240)}` : "",
    rule.has_command_pattern === true ? "command pattern (hidden)" : ""
  ].filter(Boolean);
  return parts.join(" · ") || "scoped rule";
}

function friendlyPolicyReason(reason: string): string {
  return reason ? reason.replace(/_/g, " ") : "";
}

function friendlyReviewScope(scope: string): string {
  return scope ? scope.replace(/_/g, " ") : "(unknown)";
}

function friendlyEventType(value: string): string {
  return value ? value.replace(/_/g, " ") : "event";
}

function reviewLocation(finding: Record<string, unknown>): string {
  const path = displayValue(finding.path, 500) || "(workspace)";
  const start = typeof finding.line_start === "number" ? finding.line_start : undefined;
  const end = typeof finding.line_end === "number" ? finding.line_end : undefined;
  if (start === undefined) {
    return path;
  }
  return end !== undefined && end !== start ? `${path}:${start}-${end}` : `${path}:${start}`;
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function displayValue(value: unknown, maxLength: number): string {
  if (value === undefined || value === null) {
    return "";
  }
  const text = typeof value === "string" ? value : JSON.stringify(value) ?? String(value);
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, Math.max(0, maxLength - 1))}…`;
}

function oauthPollDelay(cancellation: vscode.CancellationToken): Promise<void> {
  if (cancellation.isCancellationRequested) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    let disposable: vscode.Disposable | undefined;
    const timer = setTimeout(() => {
      disposable?.dispose();
      resolve();
    }, 750);
    disposable = cancellation.onCancellationRequested(() => {
      clearTimeout(timer);
      disposable?.dispose();
      resolve();
    });
  });
}

async function skillValidateSelector(raw: string): Promise<Record<string, boolean | string>> {
  if (raw) {
    return { name: raw };
  }
  const name = await optionalInput("Skill name (leave blank to validate all)");
  if (name) {
    return { name };
  }
  return { all: true };
}

async function pickStream(): Promise<boolean> {
  const picked = await vscode.window.showQuickPick(
    [
      { label: "on", value: true },
      { label: "off", value: false }
    ],
    { title: "Set Alysis Code streaming" }
  );
  if (!picked) {
    throw new ProtocolClientError("input_cancelled", "Stream selection was cancelled.");
  }
  return picked.value;
}

function streamValue(value: string): boolean | undefined {
  const normalized = value.trim().toLowerCase();
  if (["on", "true", "yes", "1"].includes(normalized)) {
    return true;
  }
  if (["off", "false", "no", "0"].includes(normalized)) {
    return false;
  }
  return undefined;
}

function sessionModeParam(value: unknown): SessionMode {
  const normalized = String(value).trim().toLowerCase();
  if (normalized === "readonly" || normalized === "review" || normalized === "auto") {
    return normalized;
  }
  throw new ProtocolClientError(
    "invalid_mode",
    "Session mode must be readonly, review, or auto."
  );
}

function assertSessionModeTrust(mode: SessionMode, trusted: boolean): void {
  if (mode !== "readonly" && !trusted) {
    throw new ProtocolClientError(
      "workspace_trust_required",
      `Workspace Trust is required before switching Alysis Code to ${mode} mode. Use readonly mode in untrusted workspaces.`
    );
  }
}

/**
 * Keep a declared `workspace-relative-path` inside the negotiated workspace root before it is
 * forwarded. The backend validates too, but the IDE must never hand it `../../..` verbatim.
 */
function workspaceScopedPath(value: string, workspace: string | undefined, label: string): string {
  const candidate = String(value ?? "").trim();
  if (!candidate || candidate.includes("\0")) {
    throw new ProtocolClientError("invalid_path", `${label} must be a path inside the open workspace folder.`);
  }
  if (!workspace) {
    throw new ProtocolClientError("workspace_required", workspaceScopeRequiredMessage(`setting ${label}`));
  }
  const root = path.resolve(workspace);
  const resolved = path.isAbsolute(candidate) ? path.resolve(candidate) : path.resolve(root, candidate);
  const relative = path.relative(root, resolved);
  if (relative !== "" && (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative))) {
    throw new ProtocolClientError(
      "path_outside_workspace",
      `${label} must stay inside the open workspace folder.`
    );
  }
  return candidate;
}

function requireWorkspaceTrustForMutation(trusted: boolean, action: string): void {
  if (!trusted) {
    throw new ProtocolClientError(
      "workspace_trust_required",
      `${action} requires Workspace Trust.`
    );
  }
}

function subagentParams(
  raw: string,
  sessionId: string,
  trusted: boolean,
  supportsMethod: (method: string) => boolean
): Record<string, unknown> {
  const normalized = raw.trim().toLowerCase();
  if (!normalized || normalized === "status") {
    return { session_id: sessionId };
  }
  const enabled = subagentToggleValue(normalized);
  if (!supportsMethod("session.subagents.setEnabled")) {
    throw new ProtocolClientError(
      "backend_action_unsupported",
      "Subagent toggling is unavailable because the current Alysis Code bridge does not advertise session.subagents.setEnabled."
    );
  }
  requireWorkspaceTrustForMutation(trusted, "Toggling subagents");
  return { session_id: sessionId, enabled, workspace_trusted: trusted };
}

function subagentToggleValue(value: string): boolean {
  const normalized = value.trim().toLowerCase();
  if (["on", "enable", "enabled", "true", "1"].includes(normalized)) {
    return true;
  }
  if (["off", "disable", "disabled", "false", "0"].includes(normalized)) {
    return false;
  }
  throw new ProtocolClientError(
    "invalid_subagent_command",
    "Usage: /subagents status, /subagents on, or /subagents off. Explicit subagent execution is unavailable in IDE v1."
  );
}

async function traceParams(
  raw: string,
  sessionId: string,
  defaultEvents: boolean
): Promise<Record<string, unknown>> {
  const normalized = raw.trim().toLowerCase();
  if (!normalized || normalized === "status") {
    return defaultEvents ? { session_id: sessionId, operation: "events" } : { session_id: sessionId };
  }
  if (normalized === "events" || normalized === "list") {
    return { session_id: sessionId, operation: "events" };
  }
  if (normalized === "clear") {
    return { session_id: sessionId, operation: "clear" };
  }
  const level = traceLevelParam(normalized);
  if (level === "full") {
    await confirmFullTrace();
    return { session_id: sessionId, level, confirm: true };
  }
  return { session_id: sessionId, level };
}

function traceLevelParam(value: unknown): "off" | "compact" | "full" {
  const normalized = String(value).trim().toLowerCase();
  if (normalized === "off" || normalized === "compact" || normalized === "full") {
    return normalized;
  }
  throw new ProtocolClientError(
    "invalid_trace_command",
    "Usage: /trace, /trace off, /trace compact, /trace full, /trace events, or /trace clear."
  );
}

async function confirmFullTrace(): Promise<void> {
  const selection = await vscode.window.showWarningMessage(
    "Full trace can expose more detailed reasoning and tool lifecycle metadata. Alysis Code will still redact and bound backend output. Continue?",
    { modal: true },
    "Enable Full Trace"
  );
  if (selection !== "Enable Full Trace") {
    throw new ProtocolClientError("input_cancelled", "Full trace was cancelled.");
  }
}

async function terminalParams(
  raw: string,
  sessionId: string,
  trusted: boolean
): Promise<Record<string, unknown>> {
  const trimmed = raw.trim();
  if (!trimmed || trimmed.toLowerCase() === "list") {
    return { session_id: sessionId, operation: "list" };
  }
  const [command, ...rest] = trimmed.split(/\s+/);
  const subcommand = command.toLowerCase();
  if (subcommand === "help") {
    throw new ProtocolClientError(
      "unsupported_terminal_command",
      "Usage: /terminals, /terminals list, /terminals show <id>, /terminals kill <id>, or /terminals clear <id>. IDE v1 does not start shells or stream interactive PTYs."
    );
  }
  if (subcommand === "show") {
    const processId = terminalId(rest.join(" "));
    return { session_id: sessionId, operation: "show", process_id: processId };
  }
  if (subcommand === "kill") {
    const processId = terminalId(rest.join(" "));
    requireWorkspaceTrustForMutation(trusted, "Killing a background terminal");
    await confirmTerminalMutation("Kill Terminal");
    return {
      session_id: sessionId,
      operation: "kill",
      process_id: processId,
      workspace_trusted: trusted,
      confirm: true
    };
  }
  if (subcommand === "clear") {
    const processId = terminalId(rest.join(" "));
    requireWorkspaceTrustForMutation(trusted, "Clearing terminal output");
    await confirmTerminalMutation("Clear Terminal Output");
    return {
      session_id: sessionId,
      operation: "clear",
      process_id: processId,
      workspace_trusted: trusted,
      confirm: true
    };
  }
  throw new ProtocolClientError(
    "unsupported_terminal_command",
    "Usage: /terminals, /terminals list, /terminals show <id>, /terminals kill <id>, or /terminals clear <id>. IDE v1 does not provide arbitrary shell execution."
  );
}

function terminalId(value: string): string {
  const processId = value.trim();
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(processId) || processId.includes("..")) {
    throw new ProtocolClientError(
      "invalid_terminal_id",
      "Terminal process id is required and must be an opaque managed terminal id."
    );
  }
  return processId;
}

async function confirmTerminalMutation(title: string): Promise<void> {
  const selection = await vscode.window.showWarningMessage(
    `${title} affects an existing managed background process. Continue?`,
    { modal: true },
    "Continue"
  );
  if (selection !== "Continue") {
    throw new ProtocolClientError("input_cancelled", `${title} was cancelled.`);
  }
}

function requireActiveSession(context: BackendActionContext): string {
  if (!context.sessionId) {
    throw new ProtocolClientError("active_session_required", "An active Alysis Code session is required.");
  }
  return context.sessionId;
}

function requireCodeReviewWorkspace(value: string | undefined): string {
  const workspace = value?.trim();
  if (!workspace) {
    throw new ProtocolClientError(
      "workspace_required",
      workspaceScopeRequiredMessage("starting code review")
    );
  }
  return workspace;
}

function workspaceRoot(): string | undefined {
  return activeWorkspaceRoot();
}

function backendActionCanTouchWorkspace(action: BackendActionMetadata): boolean {
  return action.workspaceRequired;
}

function stringOrUndefined(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function looksLikeRemoteSource(source: string): boolean {
  const trimmed = source.trim().toLowerCase();
  return (
    trimmed.startsWith("https://") ||
    trimmed.startsWith("git+https://") ||
    trimmed.startsWith("ssh://") ||
    trimmed.startsWith("git://") ||
    trimmed.startsWith("git@")
  );
}

function parseConfigSetArg(raw: string): { key: string; value?: string } | undefined {
  const trimmed = raw.trim();
  if (!trimmed) {
    return undefined;
  }
  const separator = trimmed.indexOf("=");
  if (separator < 0) {
    const match = /^(\S+)(?:\s+(.+))?$/.exec(trimmed);
    return validateConfigArgKey(match?.[1] ?? "", match?.[2]?.trim());
  }
  return validateConfigArgKey(trimmed.slice(0, separator).trim(), trimmed.slice(separator + 1).trim());
}

function validateConfigArgKey(key: string, value?: string): { key: string; value?: string } {
  if (!key) {
    throw new ProtocolClientError("invalid_config_key", "Config key is required.");
  }
  if (!/^[A-Za-z0-9_.-]+$/.test(key)) {
    throw new ProtocolClientError("invalid_config_key", "Config key must contain only letters, numbers, dots, underscores, or hyphens.");
  }
  return value === undefined ? { key } : { key, value };
}

function configSchemaEditableKeys(schemaResult: unknown): string[] {
  if (!isRecord(schemaResult) || !isRecord(schemaResult.schema)) {
    return [];
  }
  const properties = isRecord(schemaResult.schema.properties) ? schemaResult.schema.properties : undefined;
  if (!properties) {
    return [];
  }
  return Object.keys(properties)
    .filter((key) => !isSecretLikeConfigKey(key))
    .sort((left, right) => left.localeCompare(right));
}

function isSecretLikeConfigKey(key: string): boolean {
  return /(^|[_.-])(api[_-]?key|apikey|access[_-]?token|token|secret|password|credential|authorization)($|[_.-])/i.test(key.trim());
}

function isSecretLikeConfigValue(value: string): boolean {
  const trimmed = value.trim();
  return (
    /(?:git\+)?https?:\/\/[^/@\s]+@/i.test(trimmed) ||
    /\bauthorization\s*:\s*bearer\s+\S+/i.test(trimmed) ||
    /\bbearer\s+[A-Za-z0-9._~+/-]{16,}/i.test(trimmed) ||
    /\b[A-Z0-9_.-]*(?:api[_-]?key|token|secret|password|credential)[A-Z0-9_.-]*\s*[:=]\s*\S+/i.test(trimmed) ||
    /\b(?:sk|sk-proj)-[A-Za-z0-9_-]{12,}\b/i.test(trimmed)
  );
}

function parseConfigValue(value: string): unknown {
  const trimmed = value.trim();
  if (!trimmed) {
    return "";
  }
  if (/^(true|false|null)$/i.test(trimmed) || /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(trimmed) || /^[{\[]/.test(trimmed)) {
    try {
      return JSON.parse(trimmed);
    } catch {
      return value;
    }
  }
  return value;
}

function sandboxPullParams(raw: string, trusted: boolean): Record<string, unknown> {
  const images = raw
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    workspace_trusted: trusted,
    images: images.length > 0 ? images : undefined,
    include_server: false
  };
}

async function updateCheckParams(raw: string): Promise<Record<string, unknown>> {
  const normalized = raw.trim().toLowerCase();
  if (normalized) {
    if (/\b(force|online|network|remote|latest)\b/.test(normalized)) {
      const params: Record<string, unknown> = { cached: false, allow_network: true };
      if (/\bforce\b/.test(normalized)) {
        params.force = true;
      }
      return params;
    }
    return { cached: true };
  }
  const picked = await vscode.window.showQuickPick(
    [
      {
        label: "Cached/local status",
        detail: "Do not perform a network update check.",
        value: { cached: true }
      },
      {
        label: "Check online now",
        detail: "Perform a user-triggered network update check.",
        value: { cached: false, allow_network: true }
      }
    ],
    { title: "Alysis Code update check", ignoreFocusOut: true }
  );
  if (!picked) {
    throw new ProtocolClientError("input_cancelled", "Update check selection was cancelled.");
  }
  return picked.value;
}
