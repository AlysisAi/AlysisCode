import {
  ArtifactSummary,
  JobStatusResult,
  ProtocolEventEnvelope,
  SessionCreateResult,
  asBoolean,
  isRecord,
  asNumber,
  asString
} from "../client/AlysisProtocol";
import { ChatErrorKind, ClassifiedChatError, classifyChatError } from "./ChatErrorTaxonomy";
import type { CockpitAction } from "./CockpitReadiness";

// Cap chat history so a long session does not grow memory (or the posted state) unbounded —
// mirrors the 100-event runtime-events cap. Oldest items are trimmed; the active streaming item is
// always the newest, so it is never trimmed.
const MAX_CHAT_ITEMS = 200;
const MAX_ACTIVITY_TITLE_CHARS = 120;
const MAX_ACTIVITY_SUMMARY_CHARS = 600;
const MAX_ACTIVITY_TARGET_CHARS = 240;
const MAX_ACTIVITY_TEXT_CHARS = 1_000;

const ACTIVITY_KINDS = new Set([
  "read",
  "search",
  "edit",
  "command",
  "test",
  "git",
  "network",
  "browser",
  "diagnostic",
  "subagent",
  "service",
  "plan",
  "other"
]);

export type ChatItemKind =
  | "user"
  | "assistant"
  | "tool"
  | "status"
  | "info"
  | "warning"
  | "error"
  | "approval"
  | "notice";

/**
 * The classified projection carried by every `error` item. `kind` lives here (not on the item) so the
 * transcript item kind stays the rendering discriminator it has always been. `title`/`detail`/
 * `actions`/`retryAfterSeconds` are mirrored onto the item root so a renderer can read either shape.
 */
export interface ChatItemError {
  kind: ChatErrorKind;
  title: string;
  detail: string;
  actions: CockpitAction[];
  retryAfterSeconds?: number;
}

export interface ChatItem {
  id: string;
  kind: ChatItemKind;
  title?: string;
  text: string;
  timestamp?: string;
  status?: string;
  metadata?: Record<string, unknown>;
  /** Error items only: the plain-language sentence shown under the title. Never a protocol code. */
  detail?: string;
  /** Error items only: concrete recovery offers, at most one primary. */
  actions?: CockpitAction[];
  /** Error items only: set when the backend told us how long to wait before retrying. */
  retryAfterSeconds?: number;
  /** Error items only: the full classification, with the taxonomy `kind`. */
  error?: ChatItemError;
}

export interface ApprovalPrompt {
  approvalId: string;
  promptId: string;
  kind: string;
  reason: string;
  preview: string;
  files: string[];
  command: string | null;
  allowForSessionSupported: boolean;
  allowForSessionScope: Record<string, unknown> | null;
  allowForSessionWarning: string | null;
  expiresAt: string | null;
  resolved: boolean;
  decision?: ApprovalDecisionState;
}

export type ApprovalDecisionState = "allow_once" | "allow_for_session" | "deny" | "expired";

export interface ChatState {
  sessionId: string | null;
  jobId: string | null;
  workspaceRoot: string | null;
  mode: string | null;
  jobStatus: string;
  lastSequence: number;
  replayTruncated: boolean;
  items: ChatItem[];
  itemsTrimmed: boolean;
  approvals: ApprovalPrompt[];
  artifacts: ArtifactSummary[];
  artifactsTruncated: boolean;
}

export class ChatTranscript {
  private readonly items: ChatItem[] = [];
  private readonly approvals = new Map<string, ApprovalPrompt>();
  private readonly tools = new Map<string, ChatItem>();
  private readonly activities = new Map<string, ChatItem>();
  private readonly subagents = new Map<string, ChatItem>();
  private semanticActivityEnabled = false;
  private activeAssistant: ChatItem | undefined;
  private artifacts: ArtifactSummary[] = [];
  private artifactsTruncated = false;
  private session: SessionCreateResult | undefined;
  private jobId: string | null = null;
  private jobStatus = "idle";
  private readonly lastSequenceBySession = new Map<string, number>();
  private replayTruncatedValue = false;
  private itemsTrimmedValue = false;

  public setSession(session: SessionCreateResult): void {
    this.session = session;
    this.jobStatus = "idle";
  }

  /** Update only the displayed execution mode, e.g. after a persona clamp changed it. */
  public setSessionMode(mode: SessionCreateResult["mode"]): void {
    if (this.session && this.session.mode !== mode) {
      this.session = { ...this.session, mode };
    }
  }

  public setSemanticActivityEnabled(enabled: boolean): void {
    this.semanticActivityEnabled = enabled;
  }

  public setJob(jobId: string, status: string): void {
    this.jobId = jobId;
    this.jobStatus = status;
  }

  public clearSession(status = "closed"): void {
    this.session = undefined;
    this.jobId = null;
    this.jobStatus = status;
    this.activeAssistant = undefined;
    this.tools.clear();
    this.activities.clear();
    this.subagents.clear();
    // A bridge/session boundary makes any outstanding approval token unusable. Keep the
    // transcript for reconnect diagnostics, but render those prompts as expired instead of
    // leaving actionable buttons that would be sent to a later, unrelated session.
    for (const approval of this.pendingApprovals()) {
      this.resolveApprovalDecision(approval.approvalId, "expired");
    }
  }

  /** Start a genuinely blank conversation, including all session-scoped UI state. */
  public reset(status = "idle"): void {
    this.items.splice(0);
    this.approvals.clear();
    this.tools.clear();
    this.activities.clear();
    this.subagents.clear();
    this.activeAssistant = undefined;
    this.artifacts = [];
    this.artifactsTruncated = false;
    this.session = undefined;
    this.jobId = null;
    this.jobStatus = status;
    this.lastSequenceBySession.clear();
    this.replayTruncatedValue = false;
    this.itemsTrimmedValue = false;
  }

  public setJobStatus(status: JobStatusResult): void {
    const previous = this.jobStatus;
    this.jobId = status.job_id;
    this.jobStatus = status.status;
    if (status.status === "cancelled" && previous !== "cancelled") {
      // One calm line for the whole stop, in place of the bridge's per-step lifecycle warnings.
      this.activeAssistant = undefined;
      this.addNotice("Task stopped at your request.");
    } else if (status.status === "completed") {
      // A clean completion (exit 0 / unspecified) needs no card — the assistant's reply is the
      // signal that the turn finished. Only surface a notice when the exit code is non-zero, which
      // is a result the user should actually see.
      if (status.exit_code !== null && status.exit_code !== undefined && status.exit_code !== 0) {
        this.addNotice(`Job completed with exit code ${status.exit_code}.`);
      }
    } else if (status.status === "failed") {
      const reason = status.error ?? "Alysis Code job failed.";
      this.addJobError(reason, classifyChatError(reason), status.session_id, status.job_id);
    }
  }

  /**
   * Preserve the backend's terminal result as evidence while projecting the
   * editor-owned verification verdict honestly in the primary job state.
   */
  public markVerificationNeedsAttention(jobId: string, backendStatus: string, reason: string): void {
    this.jobId = jobId;
    this.jobStatus = "needs_attention";
    // Editor-owned verdict, not a backend failure: the reason is already host-authored plain language,
    // so it is carried through as the card detail instead of being re-classified from prose.
    const item = this.addError(reason, {
      kind: "unknown",
      title: "Verification needs attention",
      detail: reason,
      actions: []
    });
    item.status = "needs_attention";
    item.metadata = {
      job_id: jobId,
      backend_status: backendStatus,
      verification_status: "failed"
    };
  }

  public addUserMessage(text: string): void {
    this.activeAssistant = undefined;
    this.addItem("user", "You", text);
  }

  public addNotice(text: string): void {
    // An empty notice is a card with nothing to say; backend actions that already reported their
    // result elsewhere return "" on purpose.
    if (!text.trim()) {
      return;
    }
    this.addItem("notice", "Notice", text);
  }

  /**
   * Add one error card. The raw text is kept as the item body for diagnostics, while the card the
   * user reads is the classified projection: a plain-language title, one sentence, and concrete
   * actions — never a protocol code or an HTTP status.
   */
  public addError(text: string, classified: ClassifiedChatError = classifyChatError(text)): ChatItem {
    const item = this.addItem("error", classified.title, text);
    return applyErrorClassification(item, classified);
  }

  public markReplayTruncated(): void {
    this.replayTruncatedValue = true;
    this.addNotice("Event replay was truncated by the bridge; older events are no longer retained.");
  }

  public setArtifacts(artifacts: ArtifactSummary[], truncated: boolean): void {
    this.artifacts = artifacts;
    this.artifactsTruncated = truncated;
  }

  /**
   * Apply one protocol event. Returns false only for an unsupported event type so the host can
   * retain that diagnostic in the Output channel without turning protocol internals into chat.
   */
  public applyEvent(event: ProtocolEventEnvelope): boolean {
    const previousSequence = this.lastSequenceBySession.get(event.session_id) ?? 0;
    if (event.sequence <= previousSequence) {
      return true;
    }
    this.lastSequenceBySession.set(event.session_id, event.sequence);
    switch (event.type) {
      case "message_delta":
        this.applyMessageDelta(event);
        return true;
      case "message_end":
        this.applyMessageEnd(event);
        return true;
      // Legacy tool events always produce a row. When the backend also sends a semantic
      // activity_update for the same call (activity_id === call_id) that activity adopts the row,
      // so nothing renders twice; when it does not — CLI 0.14 emits activities for patches and
      // prompts but not for verify_run/shell_run — the execution still shows up instead of
      // silently vanishing between its approval card and the final reply.
      case "tool_call_started":
        this.applyToolStarted(event);
        return true;
      case "tool_call_progress":
        this.applyToolProgress(event);
        return true;
      case "tool_call_completed":
        this.applyToolCompleted(event);
        return true;
      case "activity_update":
        this.applyActivityUpdate(event);
        return true;
      case "subagent_state_changed":
        this.applySubagentStateChanged(event);
        return true;
      case "status_update":
      case "mode_changed":
        // Mode/model changes are reflected by the composer's own controls (the Permissions picker and
        // the model chip), not by a "mode: auto, model: …" card in the conversation.
        this.applySessionStatus(event);
        return true;
      case "info_emitted":
        // Info events are transient runtime status — progress chatter ("Understanding your request",
        // "Drafting response") and job-lifecycle bookkeeping ("job_started …", "job_completed …").
        // None of it is conversation. Keeping it out of the timeline lets the user's message and the
        // assistant's reply read as a clean exchange; that something is happening is already conveyed
        // by the running state (aria-busy + the breathing brand mark).
        return true;
      case "warning_emitted": {
        // Job-lifecycle bookkeeping ("job_cancelled <id> reason=…", "cancellation_requested <id> …")
        // is machine chatter, not a warning for the user: the job status transition already renders
        // the outcome ("Task stopped"). Everything else is a real warning worth a card.
        const warning = payloadText(event.payload);
        if (LIFECYCLE_WARNING_PATTERN.test(warning)) {
          return true;
        }
        this.addItem("warning", "Warning", warning, event);
        return true;
      }
      case "error_raised":
        // The backend's error code drives classification but never reaches the card: the user reads a
        // plain-language title and one sentence, while the raw payload stays as the item body.
        this.addEventError(
          classifyChatError({ code: asString(event.payload.code, ""), message: payloadText(event.payload) }),
          payloadText(event.payload),
          event
        );
        return true;
      case "prompt_for_input":
        if (asString(event.payload.kind) === "approval_result") {
          this.applyApprovalResult(event);
        } else if (asString(event.payload.kind) === "approval" || event.payload.approval_id) {
          this.applyApproval(event);
        } else {
          this.addItem("notice", "Input requested", payloadText(event.payload), event);
        }
        return true;
      default:
        return false;
    }
  }

  public resolveApproval(
    approvalId: string,
    decision: ApprovalDecisionState
  ): void {
    this.resolveApprovalDecision(approvalId, decision);
  }

  private resolveApprovalDecision(
    approvalId: string,
    decision: ApprovalDecisionState
  ): void {
    const approval = this.approvals.get(approvalId);
    if (!approval) {
      return;
    }
    approval.resolved = true;
    approval.decision = decision;
    const item = this.items.find((candidate) => candidate.id === `approval:${approvalId}`);
    if (item) {
      item.status = decision;
      item.text = approvalDecisionText(approval, decision);
    }
  }

  public expireApproval(approvalId: string): void {
    this.resolveApprovalDecision(approvalId, "expired");
  }

  public pendingApprovals(): ApprovalPrompt[] {
    return [...this.approvals.values()].filter((approval) => !approval.resolved);
  }

  public lastSequence(sessionId = this.session?.session_id): number {
    return sessionId ? this.lastSequenceBySession.get(sessionId) ?? 0 : 0;
  }

  public state(): ChatState {
    return {
      sessionId: this.session?.session_id ?? null,
      jobId: this.jobId,
      workspaceRoot: this.session?.workspace_root ?? null,
      mode: this.session?.mode ?? null,
      jobStatus: this.jobStatus,
      lastSequence: this.lastSequence(),
      replayTruncated: this.replayTruncatedValue,
      itemsTrimmed: this.itemsTrimmedValue,
      items: this.items.map((item) => ({ ...item, metadata: item.metadata ? { ...item.metadata } : undefined })),
      approvals: this.pendingApprovals().map((approval) => ({
        ...approval,
        files: [...approval.files],
        allowForSessionScope: approval.allowForSessionScope
          ? { ...approval.allowForSessionScope }
          : null
      })),
      artifacts: this.artifacts.map((artifact) => ({ ...artifact })),
      artifactsTruncated: this.artifactsTruncated
    };
  }

  /** Keep the session's mode current so the composer switch follows a backend mode change. */
  private applySessionStatus(event: ProtocolEventEnvelope): void {
    const payload = isRecord(event.payload.payload) ? event.payload.payload : event.payload;
    const mode = asString(payload.mode);
    if (this.session && (mode === "readonly" || mode === "review" || mode === "auto")) {
      this.session = { ...this.session, mode };
    }
  }

  private applyMessageDelta(event: ProtocolEventEnvelope): void {
    const text = asString(event.payload.text);
    if (!this.activeAssistant) {
      this.activeAssistant = this.addItem("assistant", "Alysis Code", "", event);
    }
    this.activeAssistant.text += text;
    this.activeAssistant.timestamp = event.timestamp;
  }

  private applyMessageEnd(event: ProtocolEventEnvelope): void {
    const text = asString(event.payload.text);
    // Retained history uses the same event as a completed live reply, with an explicit role.
    // A replayed user turn also separates identical assistant answers on adjacent turns.
    if (event.payload.role === "user") {
      this.activeAssistant = undefined;
      if (text.length > 0) {
        this.addItem("user", "You", text, event).status = "complete";
      }
      return;
    }
    if (text.length > 0) {
      if (!this.activeAssistant) {
        // Guard against the bridge double-emitting a finished assistant message (a second
        // message_end carrying the same full text after the first already completed). Without an
        // active streaming item, a fresh message_end whose text exactly matches the immediately
        // preceding completed assistant card is that duplicate — drop it instead of re-carding it.
        const last = this.items[this.items.length - 1];
        if (last && last.kind === "assistant" && last.status === "complete" && last.text === text) {
          return;
        }
        this.activeAssistant = this.addItem("assistant", "Alysis Code", "", event);
      }
      this.activeAssistant.text = text;
    }
    if (this.activeAssistant) {
      this.activeAssistant.status = "complete";
      this.activeAssistant.timestamp = event.timestamp;
    }
    this.activeAssistant = undefined;
  }

  private applyToolStarted(event: ProtocolEventEnvelope): void {
    const callId = asString(event.payload.call_id, `tool-${event.sequence}`);
    if (this.activities.has(callId)) {
      // The semantic activity for this call already owns a row; the legacy start is redundant.
      return;
    }
    const name = asString(event.payload.name, "tool");
    const preview = asString(event.payload.arguments_preview);
    const item = this.addItem("tool", name, preview, event);
    item.status = "running";
    // Keep the protocol name for diagnostics, but let the webview translate it into a
    // human activity label. Keeping the input separate also means a completion preview
    // can replace item.text without losing which file or query the tool was working on.
    item.metadata = {
      call_id: callId,
      tool_name: name,
      input_preview: preview,
      ...(asString(event.payload.worker_id)
        ? {
            worker_id: asString(event.payload.worker_id),
            worker_role: asString(event.payload.role)
          }
        : {})
    };
    this.tools.set(callId, item);
  }

  private applyToolProgress(event: ProtocolEventEnvelope): void {
    const callId = asString(event.payload.call_id);
    if (this.activities.has(callId)) {
      // Owned by a semantic activity row; its own updates carry the state.
      return;
    }
    const item = this.tools.get(callId);
    if (!item) {
      this.addItem("tool", "Tool progress", payloadText(event.payload), event);
      return;
    }
    const text = asString(event.payload.text);
    item.text = item.text.length > 0 ? `${item.text}\n${text}` : text;
    item.timestamp = event.timestamp;
  }

  private applySubagentStateChanged(event: ProtocolEventEnvelope): void {
    const runId = asString(event.payload.subagent_run_id, `event-${event.sequence}`);
    const name = asString(event.payload.name, "helper");
    const state = asString(event.payload.state, "running").toLowerCase();
    const existing = this.subagents.get(runId);
    const item = existing ?? this.addItem(
      "tool",
      `${friendlySubagentName(name)} helper`,
      subagentLifecycleText(event.payload, state),
      event
    );
    item.title = `${friendlySubagentName(name)} helper`;
    item.text = subagentLifecycleText(event.payload, state);
    item.status = subagentDisplayStatus(state);
    item.timestamp = event.timestamp;
    item.metadata = {
      subagent_run_id: runId,
      subagent_session_id: asString(event.payload.subagent_session_id) || null,
      worker_id: name,
      worker_role: asString(event.payload.mode),
      tool_name: "subagent",
      input_preview: asString(event.payload.description)
    };
    if (!existing) {
      this.subagents.set(runId, item);
    }
  }

  private applyToolCompleted(event: ProtocolEventEnvelope): void {
    const callId = asString(event.payload.call_id);
    if (!asBoolean(event.payload.success)) {
      this.addRunnerSetupError(asString(event.payload.result_preview), event);
    }
    if (this.activities.has(callId)) {
      // Owned by a semantic activity row; its terminal status arrives as an activity_update.
      return;
    }
    const item = this.tools.get(callId);
    const text = asString(event.payload.result_preview);
    if (!item) {
      this.addItem("tool", "Tool completed", text, event).status = asBoolean(event.payload.success) ? "ok" : "failed";
      return;
    }
    item.text = text || item.text;
    item.status = asBoolean(event.payload.success) ? "ok" : "failed";
    item.timestamp = event.timestamp;
  }

  private applyActivityUpdate(event: ProtocolEventEnvelope): void {
    // Receiving the semantic event is itself authoritative even if an older/mock handshake omitted
    // it. Subsequent legacy tool events in this session must not create a second row.
    this.semanticActivityEnabled = true;
    const activityId = boundedActivityIdentifier(event.payload.activity_id, event);
    const legacyItem = this.tools.get(activityId);
    const existing = this.activities.get(activityId) ?? legacyItem;
    const kind = activityKind(event.payload.kind);
    const title = activityTitle(event.payload.display_title, kind);
    const target = boundedActivityText(event.payload.target, MAX_ACTIVITY_TARGET_CHARS);
    const summary = boundedActivityText(event.payload.summary, MAX_ACTIVITY_SUMMARY_CHARS);
    const diff = activityDiff(event.payload.diff);
    const durationMs = activityDuration(event.payload.duration_ms);
    const item = existing ?? this.addItem("tool", title, "", event);

    item.title = title;
    item.text = activityText(summary, target, diff, durationMs);
    item.status = activityStatus(event.payload.status);
    if (item.status === "failed" && ["command", "test", "service"].includes(kind)) {
      this.addRunnerSetupError(summary, event);
    }
    item.timestamp = event.timestamp;
    item.metadata = {
      activityId,
      activityKind: kind,
      // The bridge marks the activity that spans the whole turn ("Working on request") with
      // operation=execute_prompt; the sidebar renders that as the running indicator, not as a step.
      // Only the boolean is projected — internal operation names never reach the transcript.
      turnLevel: asString(event.payload.operation) === "execute_prompt",
      toolName: kind,
      toolInput: target
    };
    this.activities.set(activityId, item);
    this.tools.delete(activityId);
  }

  private addRunnerSetupError(text: string, event: ProtocolEventEnvelope): void {
    const classified = classifyChatError(text);
    if (classified.kind === "sandbox_unavailable" || classified.kind === "checkpoint_unavailable") {
      // One recovery card per job, even when several tools hit the same unavailable runner.
      this.addEventError(classified, classified.detail, event);
    }
  }

  private applyApproval(event: ProtocolEventEnvelope): void {
    const approvalId = asString(event.payload.approval_id || event.payload.prompt_id);
    if (!approvalId || this.approvals.has(approvalId)) {
      return;
    }
    const files = Array.isArray(event.payload.files)
      ? event.payload.files.filter((file): file is string => typeof file === "string")
      : [];
    const approval: ApprovalPrompt = {
      approvalId,
      promptId: asString(event.payload.prompt_id, approvalId),
      // payload.kind is the envelope discriminator ("approval"); the tool/operation that asked is
      // approval_kind (e.g. "write_file", "shell", "verify_run").
      kind: asString(event.payload.approval_kind) || asString(event.payload.kind, "approval"),
      reason: asString(event.payload.reason || event.payload.prompt_text),
      preview: asString(event.payload.preview),
      files,
      command: typeof event.payload.command === "string" ? event.payload.command : null,
      allowForSessionSupported: asBoolean(event.payload.allow_for_session_supported),
      allowForSessionScope: isRecord(event.payload.allow_for_session_scope)
        ? event.payload.allow_for_session_scope
        : null,
      allowForSessionWarning:
        typeof event.payload.allow_for_session_warning === "string"
          ? event.payload.allow_for_session_warning
          : null,
      expiresAt: typeof event.payload.expires_at === "string" ? event.payload.expires_at : null,
      resolved: false
    };
    this.approvals.set(approvalId, approval);
    this.items.push({
      id: `approval:${approvalId}`,
      kind: "approval",
      title: "Approval needed",
      text: approvalText(approval),
      timestamp: event.timestamp,
      status: "pending",
      metadata: {
        approval_id: approvalId,
        allow_for_session_supported: approval.allowForSessionSupported,
        allow_for_session_scope: approval.allowForSessionScope,
        allow_for_session_warning: approval.allowForSessionWarning,
        // Structured copy for the sidebar card; `text` above stays the plain projection.
        approval_kind: approval.kind,
        approval_reason: approval.reason,
        approval_preview: approval.preview,
        approval_command: approval.command,
        approval_files: [...approval.files],
        approval_expires_at: approval.expiresAt,
        approval_scope: approvalScopeText(approval)
      }
    });
    this.trimItems();
  }

  private applyApprovalResult(event: ProtocolEventEnvelope): void {
    const approvalId = asString(event.payload.approval_id || event.payload.prompt_id);
    if (!approvalId) {
      return;
    }
    const metadata = isRecord(event.payload.metadata) ? event.payload.metadata : {};
    const status = asString(metadata.status);
    const allow = asBoolean(metadata.allow);
    const allowForSession = asBoolean(metadata.allow_for_session);
    if (status === "expired") {
      this.resolveApprovalDecision(approvalId, "expired");
      return;
    }
    this.resolveApprovalDecision(
      approvalId,
      allow ? (allowForSession ? "allow_for_session" : "allow_once") : "deny"
    );
  }

  private addEventError(
    classified: ClassifiedChatError,
    text: string,
    event: ProtocolEventEnvelope
  ): ChatItem {
    return this.addJobError(text, classified, event.session_id, event.job_id, event);
  }

  /** The stream and terminal status can report the same failure in either order. */
  private addJobError(
    text: string,
    classified: ClassifiedChatError,
    sessionId: string,
    jobId?: string | null,
    event?: ProtocolEventEnvelope
  ): ChatItem {
    // Match only retained backend errors from this job. New turns, unrelated errors, and failures
    // without a job identity must remain visible; reset/trim naturally discard old matches.
    const existing = jobId && sessionId ? this.items.find((item) =>
      item.kind === "error" && item.metadata?.backend_error === true &&
      item.metadata.session_id === sessionId && item.metadata.job_id === jobId &&
      item.text.trim() === text.trim()
    ) : undefined;
    const item = existing ?? this.addItem("error", classified.title, text, event);
    item.metadata = { ...item.metadata, backend_error: true, session_id: sessionId, job_id: jobId };
    if (event) {
      item.timestamp = event.timestamp;
      item.metadata = { ...item.metadata, event_type: event.type, sequence: event.sequence };
    }
    // A status poll has only prose; it must not erase a classification supplied by an event code.
    if (!existing || (event && classified.kind !== "unknown")) {
      applyErrorClassification(item, classified);
    }
    return item;
  }

  private addItem(
    kind: ChatItemKind,
    title: string,
    text: string,
    event?: ProtocolEventEnvelope
  ): ChatItem {
    const item: ChatItem = {
      id: `${kind}:${event?.sequence ?? this.items.length + 1}:${asNumber(Date.now())}`,
      kind,
      title,
      text,
      timestamp: event?.timestamp,
      metadata: event ? { event_type: event.type, sequence: event.sequence } : undefined
    };
    this.items.push(item);
    this.trimItems();
    return item;
  }

  private trimItems(): void {
    const excess = this.items.length - MAX_CHAT_ITEMS;
    if (excess <= 0) {
      return;
    }
    const removed = this.items.splice(0, excess);
    this.itemsTrimmedValue = true;
    // Drop Map entries that backed trimmed items so a late tool/approval event does not mutate a
    // detached object (and so the Maps do not grow unbounded over a long session).
    const removedItems = new Set(removed);
    for (const [callId, item] of this.tools) {
      if (removedItems.has(item)) {
        this.tools.delete(callId);
      }
    }
    for (const [activityId, item] of this.activities) {
      if (removedItems.has(item)) {
        this.activities.delete(activityId);
      }
    }
    for (const [runId, item] of this.subagents) {
      if (removedItems.has(item)) {
        this.subagents.delete(runId);
      }
    }
    for (const [approvalId] of this.approvals) {
      if (removed.some((item) => item.id === `approval:${approvalId}`)) {
        this.approvals.delete(approvalId);
      }
    }
  }
}

/**
 * Attach one classification to an error item. The taxonomy `kind` lives on `item.error` (the item's
 * own `kind` stays the rendering discriminator), and the readable fields are mirrored on the item
 * root so a renderer can consume either shape without reaching into metadata.
 */
function applyErrorClassification(item: ChatItem, classified: ClassifiedChatError): ChatItem {
  const actions = classified.actions.map((action) => ({ ...action }));
  item.title = classified.title;
  item.detail = classified.detail;
  item.actions = actions;
  item.error = {
    kind: classified.kind,
    title: classified.title,
    detail: classified.detail,
    actions: actions.map((action) => ({ ...action })),
    ...(classified.retryAfterSeconds === undefined ? {} : { retryAfterSeconds: classified.retryAfterSeconds })
  };
  if (classified.retryAfterSeconds !== undefined) {
    item.retryAfterSeconds = classified.retryAfterSeconds;
  } else {
    delete item.retryAfterSeconds;
  }
  return item;
}

interface ActivityDiffSummary {
  files: number;
  additions: number;
  deletions: number;
}

function boundedActivityIdentifier(value: unknown, event: ProtocolEventEnvelope): string {
  const candidate = boundedActivityText(value, 128);
  return candidate || `${event.session_id}:${event.sequence}`;
}

function activityKind(value: unknown): string {
  const candidate = typeof value === "string" ? value.trim().toLowerCase() : "";
  return ACTIVITY_KINDS.has(candidate) ? candidate : "other";
}

function activityTitle(value: unknown, kind: string): string {
  const candidate = boundedActivityText(value, MAX_ACTIVITY_TITLE_CHARS);
  if (candidate && !looksLikeProtocolIdentifier(candidate)) {
    return candidate;
  }
  switch (kind) {
    case "read":
      return "Reading files";
    case "search":
      return "Searching the codebase";
    case "edit":
      return "Making changes";
    case "command":
      return "Running a command";
    case "test":
      return "Checking the work";
    case "git":
      return "Inspecting Git changes";
    case "network":
      return "Researching online";
    case "browser":
      return "Using the browser";
    case "diagnostic":
      return "Inspecting diagnostics";
    case "subagent":
      return "Delegating focused work";
    case "service":
      return "Managing a development service";
    case "plan":
      return "Planning next steps";
    default:
      return "Working on the task";
  }
}

function looksLikeProtocolIdentifier(value: string): boolean {
  const normalized = value.trim().toLowerCase();
  if (/^(?:rs|internal)[._:/-]/.test(normalized)) {
    return true;
  }
  return !/\s/.test(normalized) && /[._:/]/.test(normalized);
}

function boundedActivityText(value: unknown, maxChars: number): string {
  if (typeof value !== "string") {
    return "";
  }
  const normalized = value
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (normalized.length <= maxChars) {
    return normalized;
  }
  return `${normalized.slice(0, Math.max(0, maxChars - 1))}…`;
}

function activityStatus(value: unknown): string {
  const status = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (status === "succeeded") {
    return "ok";
  }
  if (status === "completed_unverified") {
    return "completed_unverified";
  }
  if (status === "failed" || status === "blocked") {
    return "failed";
  }
  if (status === "cancelled" || status === "canceled") {
    return "cancelled";
  }
  if (status === "queued") {
    return "queued";
  }
  return "running";
}

function activityDuration(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.min(value, Number.MAX_SAFE_INTEGER)
    : undefined;
}

function activityDiff(value: unknown): ActivityDiffSummary | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  const files = nonNegativeActivityCount(value.files);
  const additions = nonNegativeActivityCount(value.additions);
  const deletions = nonNegativeActivityCount(value.deletions);
  if (files === 0 && additions === 0 && deletions === 0) {
    return undefined;
  }
  return { files, additions, deletions };
}

function nonNegativeActivityCount(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.min(Math.max(0, Math.floor(value)), 1_000_000_000)
    : 0;
}

function activityText(
  summary: string,
  target: string,
  diff: ActivityDiffSummary | undefined,
  durationMs: number | undefined
): string {
  const details: string[] = [];
  if (summary) {
    details.push(summary);
  } else if (target) {
    details.push(target);
  }
  if (diff) {
    const parts = [
      diff.files > 0 ? `${diff.files} file${diff.files === 1 ? "" : "s"}` : "",
      diff.additions > 0 ? `+${diff.additions}` : "",
      diff.deletions > 0 ? `−${diff.deletions}` : ""
    ].filter(Boolean);
    details.push(parts.join(" · "));
  }
  if (durationMs !== undefined) {
    details.push(formatDuration(durationMs));
  }
  return boundedActivityText(details.join(" · "), MAX_ACTIVITY_TEXT_CHARS);
}

function friendlySubagentName(value: string): string {
  const normalized = value.trim().replace(/[_-]+/g, " ");
  return normalized ? normalized[0].toUpperCase() + normalized.slice(1) : "Specialist";
}

function subagentDisplayStatus(state: string): string {
  if (["success", "succeeded", "complete", "completed"].includes(state)) {
    return "complete";
  }
  if (["failed", "degraded", "incomplete", "cancelled", "canceled"].includes(state)) {
    return "failed";
  }
  return "running";
}

function subagentLifecycleText(payload: Record<string, unknown>, state: string): string {
  const description = asString(payload.description);
  if (state === "running") {
    return description || "A focused specialist is working on part of this task.";
  }
  const elapsedMs = asNumber(payload.elapsed_ms, -1);
  const steps = asNumber(payload.steps_completed, -1);
  const details = [
    steps >= 0 ? `${steps} step${steps === 1 ? "" : "s"}` : "",
    elapsedMs >= 0 ? formatDuration(elapsedMs) : ""
  ].filter(Boolean).join(" · ");
  const error = asString(payload.error);
  if (["failed", "degraded", "incomplete", "cancelled", "canceled"].includes(state)) {
    return [error || `The helper ${state === "canceled" ? "cancelled" : state}.`, details]
      .filter(Boolean)
      .join("\n");
  }
  return ["Focused work completed.", details].filter(Boolean).join("\n");
}

function formatDuration(milliseconds: number): string {
  if (milliseconds < 1_000) {
    return `${Math.max(0, Math.round(milliseconds))} ms`;
  }
  const seconds = milliseconds / 1_000;
  return seconds < 60 ? `${seconds.toFixed(seconds < 10 ? 1 : 0)} s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

/** Bridge job-lifecycle warnings that the job state machine already renders as an outcome. */
const LIFECYCLE_WARNING_PATTERN =
  /^(?:job_(?:started|completed|cancelled)|cancellation_requested|prompt_recovery_stopped)\b/i;

function approvalDecisionText(
  approval: ApprovalPrompt,
  decision: ApprovalDecisionState
): string {
  const scope = approvalScopeText(approval);
  return [
    approval.reason || approval.preview || "Approval request",
    scope ? `Scope: ${scope}` : "",
    `Decision: ${decisionLabel(decision)}.`
  ]
    .filter(Boolean)
    .join("\n");
}

function payloadText(payload: Record<string, unknown>): string {
  return (
    asString(payload.message) ||
    asString(payload.text) ||
    asString(payload.error) ||
    asString(payload.reason) ||
    summarizePayload(payload)
  );
}

function summarizePayload(payload: Record<string, unknown>): string {
  const parts = Object.entries(payload)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key}: ${String(value)}`);
  return parts.join(", ");
}

function decisionLabel(decision: ApprovalDecisionState): string {
  if (decision === "allow_once") {
    return "allowed once";
  }
  if (decision === "allow_for_session") {
    return "allowed for session";
  }
  if (decision === "expired") {
    return "expired and denied";
  }
  return "denied";
}

function approvalText(approval: ApprovalPrompt): string {
  const parts = [
    approval.reason ? `Reason: ${approval.reason}` : "",
    approval.preview ? `Preview: ${approval.preview}` : "",
    approval.command ? `Command: ${approval.command}` : "",
    approval.files.length > 0 ? `Files: ${approval.files.join(", ")}` : "",
    approvalScopeText(approval) ? `Scope: ${approvalScopeText(approval)}` : "",
    approval.allowForSessionWarning ? `Session approval: ${approval.allowForSessionWarning}` : "",
    approval.expiresAt ? `Expires: ${approval.expiresAt}` : ""
  ].filter(Boolean);
  return parts.join("\n") || "Alysis Code is waiting for approval.";
}

function approvalScopeText(approval: ApprovalPrompt): string {
  const scope = approval.allowForSessionScope;
  if (!scope) {
    return approval.allowForSessionSupported ? "backend scoped" : "";
  }
  const type = asString(scope.type);
  if (type === "exact_command_hash") {
    return `exact command hash ${hashPrefix(scope.command_hash)}`;
  }
  if (type === "exact_file_set") {
    const fileCount = typeof scope.file_count === "number" ? scope.file_count : approval.files.length;
    return `exact file set (${fileCount} file${fileCount === 1 ? "" : "s"})`;
  }
  if (type === "exact_verify_command_set") {
    const commandCount = typeof scope.command_count === "number" ? scope.command_count : 0;
    return `exact verification command set (${commandCount} command${commandCount === 1 ? "" : "s"})`;
  }
  if (type === "explicit_backend_safe_kind") {
    return `backend safe kind ${asString(scope.safe_kind, approval.kind)}`;
  }
  return type || "backend scoped";
}

function hashPrefix(value: unknown): string {
  return typeof value === "string" && value.length > 0 ? `(sha256:${value.slice(0, 12)})` : "";
}
