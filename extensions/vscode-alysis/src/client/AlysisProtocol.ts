export const PROTOCOL_VERSION = "1";

export const REQUIRED_BRIDGE_METHODS = [
  "initialize",
  "health",
  "getCapabilities"
] as const;

export const MANAGEMENT_BRIDGE_METHODS = [
  "config.get",
  "config.set",
  "config.schema",
  "config.validate",
  "profile.list",
  "profile.show",
  "profile.add",
  "profile.remove",
  "profile.use",
  "profile.rename",
  "profile.presets",
  "profile.preset",
  "profile.convert",
  "session.show",
  "session.usage",
  "session.score",
  "tools.catalog",
  "tool.list",
  "tool.info",
  "tool.trust",
  "tool.untrust",
  "commands.list",
  "agents.list",
  "skill.list",
  "skill.info",
  "skill.init",
  "skill.validate",
  "skill.install",
  "skill.enable",
  "skill.disable",
  "skill.remove",
  "doctor.summary",
  "doctor.providers",
  "doctor.providers.live",
  "doctor.bundle",
  "sandbox.doctor",
  "sandbox.setup",
  "sandbox.pull",
  "update.check",
  "report.create",
  "mcp.status",
  "mcp.prompts.list",
  "mcp.prompts.get",
  "mcp.auth.status",
  "mcp.auth.login.start",
  "mcp.auth.login.status",
  "mcp.auth.login.cancel",
  "mcp.auth.logout",
  "hooks.list",
  "hooks.doctor",
  "hooks.trace",
  "hooks.test",
  "hooks.trust",
  "hooks.untrust",
  "hooks.init",
  "hooks.effective",
  "hooks.enable",
  "hooks.disable",
  "conventions.list",
  "conventions.render",
  "ext.search",
  "ext.list",
  "ext.info",
  "ext.install",
  "ext.uninstall",
  "ext.enable",
  "ext.disable"
] as const;

export const OPTIONAL_BRIDGE_METHODS = [
  "bridge.shutdown",
  "session.create",
  "chat.send",
  "chat.queue.list",
  "chat.queue.get",
  "chat.queue.delete",
  "chat.steer",
  "chat.steer.take",
  "feedback.prepare",
  "feedback.send",
  "checkpoint.list",
  "checkpoint.diff",
  "checkpoint.revert",
  "checkpoint.redo",
  "checkpoint.branch",
  "session.tasks.get",
  "session.tasks.replace",
  "session.questions.create",
  "session.questions.get",
  "session.questions.list",
  "session.questions.answer",
  "session.questions.cancel",
  "permission.rules.list",
  "permission.rules.grant",
  "permission.rules.revoke",
  "permission.evaluate",
  "permission.session.list",
  "permission.session.revoke",
  "code.review.start",
  "code.review.result",
  "run.start",
  "session.status",
  "session.usage",
  "session.history",
  "session.search",
  "session.context",
  "session.compact",
  "session.resume",
  "session.fork",
  "session.images.list",
  "session.images.add",
  "session.images.clear",
  "session.setMode",
  "session.setModel",
  "session.setProfile",
  "session.setStream",
  "session.setActiveWorkdir",
  "session.modelInfo",
  "session.personas.list",
  "session.persona.set",
  "session.subagents.status",
  "session.subagents.setEnabled",
  "session.trace.status",
  "session.trace.setLevel",
  "session.trace.listEvents",
  "session.trace.readArtifact",
  "session.trace.clear",
  "session.terminals.list",
  "session.terminals.show",
  "session.terminals.kill",
  "session.terminals.clear",
  "session.clear",
  "session.cancel",
  "approval.respond",
  "host.action.respond",
  "job.status",
  "session.list",
  "session.getEvents",
  "artifact.list",
  "artifact.read",
  "mcp.server.status",
  "mcp.server.enable",
  "mcp.server.disable",
  "mcp.server.restart",
  "browser.start",
  "browser.navigate",
  "browser.snapshot",
  "browser.screenshot",
  "browser.artifact.read",
  "browser.diagnostics",
  "browser.click",
  "browser.type",
  "browser.status",
  "browser.list",
  "browser.close",
  "forge.plan",
  "forge.plan.start",
  "forge.plan.result",
  "forge.list",
  "forge.open",
  "forge.resume",
  "forge.status",
  "forge.attach",
  "forge.show",
  "forge.plan.getState",
  "forge.plan.setAssistant",
  "forge.plan.setGoal",
  "forge.plan.updateTask",
  "forge.plan.validate",
  "forge.plan.regenerate",
  "forge.plan.regenerate.start",
  "forge.plan.regenerate.result",
  "forge.review",
  "forge.review.start",
  "forge.review.result",
  "forge.assets.list",
  "forge.assets.show",
  "forge.assets.add",
  "forge.assets.delete",
  "forge.assets.edit",
  "forge.assets.refresh",
  "forge.assets.cancelPending",
  "forge.assets.checkPlan",
  "forge.assets.pruneLegacy",
  "forge.executePreview",
  "forge.execute",
  "forge.cancel",
  "forge.swarm.start",
  "forge.swarm.resume",
  "forge.swarm.list",
  "forge.swarm.status",
  "forge.swarm.result",
  "forge.swarm.cancel",
  "forge.swarm.reconcile",
  "forge.swarm.review",
  "forge.swarm.apply",
  "forge.swarm.discard",
  "diff.list",
  "diff.get",
  ...MANAGEMENT_BRIDGE_METHODS
] as const;

export type RequiredBridgeMethod = (typeof REQUIRED_BRIDGE_METHODS)[number];
export type ManagementBridgeMethod = (typeof MANAGEMENT_BRIDGE_METHODS)[number];
export type OptionalBridgeMethod = (typeof OPTIONAL_BRIDGE_METHODS)[number];
export type BridgeMethod = RequiredBridgeMethod | OptionalBridgeMethod;
export type AlysisMode = "readonly" | "review" | "auto";
export type AlysisTransport = "stdio";

export interface BridgeCapabilities {
  protocol_version: string;
  methods: string[];
  events: string[];
  modes: string[];
  transport: string;
  features?: Record<string, unknown>;
}

export interface BridgeHealth {
  ok: boolean;
  name: string;
  alysis_version: string;
  protocol_version: string;
  capabilities: BridgeCapabilities;
}

export interface ProtocolRequest {
  protocol_version: typeof PROTOCOL_VERSION;
  id: string | number | null;
  method: string;
  params: Record<string, unknown>;
}

export interface ProtocolResponse {
  protocol_version: string;
  id: string | number | null;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

export interface ProtocolEventEnvelope {
  protocol_version: string;
  session_id: string;
  run_id?: string | null;
  job_id?: string | null;
  sequence: number;
  timestamp: string;
  type: string;
  payload: Record<string, unknown>;
}

export const HOST_ACTION_PROTOCOL_VERSION = "1" as const;

export const HOST_ACTION_NAMES = [
  "tasks.list",
  "tasks.run",
  "tasks.terminate",
  "tasks.status",
  "debug.list",
  "debug.start",
  "debug.stop",
  "debug.status"
] as const;

export type HostActionName = (typeof HOST_ACTION_NAMES)[number];

export interface HostCapabilitiesAdvertisement {
  protocol_version: typeof HOST_ACTION_PROTOCOL_VERSION;
  actions: HostActionName[];
}

export interface HostCapabilitySessionParams {
  workspace_trusted?: boolean;
  host_capabilities?: HostCapabilitiesAdvertisement;
}

export interface HostActionsNegotiation {
  protocol_version: typeof HOST_ACTION_PROTOCOL_VERSION;
  actions: HostActionName[];
  workspace_fence: string;
  capability_fingerprint: string;
  request_event: "host_action_requested";
  cancellation_event: "host_action_cancelled";
  session_closed_event: "session_closed";
  response_method: "host.action.respond";
  request_timeout_seconds: number;
  max_argument_bytes: number;
  max_result_bytes: number;
}

export interface HostActionRequestedPayload {
  host_action_id: string;
  action: HostActionName;
  arguments: Record<string, unknown>;
  workspace_root: string;
  workspace_fence: string;
  capability_fingerprint: string;
  expires_at: string;
  protocol_version: typeof HOST_ACTION_PROTOCOL_VERSION;
  max_result_bytes: number;
}

export interface HostActionCancelledPayload {
  host_action_id: string;
  action: HostActionName;
  workspace_fence: string;
  capability_fingerprint: string;
  reason: string;
  protocol_version: typeof HOST_ACTION_PROTOCOL_VERSION;
}

export interface HostActionResponseError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface HostActionRespondParams {
  session_id: string;
  host_action_id: string;
  workspace_fence: string;
  capability_fingerprint: string;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: HostActionResponseError;
}

export interface HostActionRespondResult {
  status: "applied";
  session_id: string;
  host_action_id: string;
  action: HostActionName;
  outcome: "result" | "error";
}

export interface RunChatOptions extends HostCapabilitySessionParams {
  model?: string;
  base_url?: string;
  temperature?: number;
  stream?: boolean;
  verify_cmd?: string | string[];
  verify_commands?: string[];
  subagents_enabled?: boolean;
  no_log?: boolean;
  yes?: boolean;
  max_steps?: number;
  active_workdir?: string;
  active_workdir_relpath?: string;
  images?: string[];
  image_paths?: string[];
}

export interface SessionCreateParams extends RunChatOptions {
  sandbox_profile?: "default" | "strict" | "warn" | "off";
  workspace: string;
  mode?: AlysisMode;
  session_id?: string;
}

export interface SessionCreateResult {
  sandbox_mode?: "strict" | "warn" | "off";
  session_id: string;
  workspace_root: string;
  mode: AlysisMode;
  host_actions?: HostActionsNegotiation;
}

export interface ChatSendParams {
  skill?: { name: string; arguments: string };
  session_id: string;
  message?: string;
  instruction?: string;
  images?: string[];
  image_paths?: string[];
  idempotency_key?: string;
  context_blocks?: Record<string, unknown>[];
}

export interface PromptQueueItem {
  session_id: string;
  prompt_id: string;
  sequence: number;
  state: "pending" | "running" | "completed" | "cancelled" | "failed";
  created_at: string;
  updated_at: string;
  attempts: number;
  terminal_at?: string | null;
  error_code?: string | null;
  message_preview?: string;
  message_truncated?: boolean;
}

export interface PromptQueueListResult {
  session_id: string;
  items: PromptQueueItem[];
  next_sequence: number;
}

/** chat.steer: a message added to the running turn, read at its next step. */
export interface ChatSteerResult {
  session_id: string;
  job_id: string;
  accepted: boolean;
  pending: number;
  /** Older waiting messages this call pushed out. */
  dropped: number;
  /** The engine shortened the message (over 4000 characters). */
  truncated: boolean;
}

/** chat.steer.take: messages finished turns did not read, oldest first. */
export interface ChatSteerTakeResult {
  session_id: string;
  messages: string[];
}

/** feedback.prepare: a private report to review; nothing has been sent. */
export interface FeedbackPrepareResult {
  report_id: string;
  /** The payload feedback.send will deliver, for the user to review. */
  report: Record<string, unknown>;
  include_diagnostics: boolean;
  /** True when the report goes out tied to the user's Alysis account. */
  signed_in: boolean;
}

/** feedback.send: the receipt for a delivered report. */
export interface FeedbackSendResult {
  report_id: string;
  created_at: string;
  anonymous: boolean;
}

export interface CheckpointChange {
  path: string;
  kind: "created" | "modified" | "deleted" | string;
}

export interface CheckpointRecord {
  checkpoint_id: string;
  session_id: string;
  turn_id: string;
  step_id?: string | null;
  parent_id?: string | null;
  kind: string;
  created_at: string;
  message: string;
  changes: CheckpointChange[];
  omitted_paths: string[];
  reverts_id?: string | null;
  redoes_id?: string | null;
}

export type StructuredTaskStatus = "pending" | "in_progress" | "completed" | "blocked";

export interface StructuredTaskItem {
  task_id: string;
  title: string;
  status: StructuredTaskStatus;
}

export interface StructuredTaskLedger {
  session_id: string;
  target_session_id?: string;
  revision?: number;
  tasks?: StructuredTaskItem[];
  updated_at?: number | null;
  updated?: boolean;
  conflict?: boolean;
  current_revision?: number;
}

export interface StructuredQuestionOption {
  option_id: string;
  label: string;
  description: string;
}

export interface StructuredQuestion {
  question_id: string;
  prompt: string;
  options: StructuredQuestionOption[];
}

export interface StructuredQuestionSet {
  session_id: string;
  question_set_id: string;
  status: "pending" | "resolving" | "answered" | "cancelled" | "expired";
  revision: number;
  questions: StructuredQuestion[];
  answers: Array<{ question_id: string; option_id: string }>;
  created_at: number;
  updated_at: number;
  expires_at: number;
  terminal_at: number | null;
  resolution_attempts: number;
  created?: boolean;
}

export interface PermissionRule {
  id: string;
  effect: "allow" | "ask" | "deny";
  order: number;
  source: string;
  tool_pattern?: string | null;
  path_pattern?: string | null;
  has_command_pattern: boolean;
}

export interface PermissionEvaluation {
  decision: "allow" | "ask" | "deny";
  reason: string;
  matched_rule_id?: string | null;
  matched_rule_source?: string | null;
  specificity: number;
}

export interface SessionPermissionGrant {
  id: string;
  kind: string;
  scope_type: string;
  source: string;
}

export type CodeReviewScope = "working_tree" | "branch" | "commit" | "range";

export interface CodeReviewStartParams {
  session_id: string;
  scope?: CodeReviewScope;
  base?: string;
  head?: string;
  revision?: string;
  workspace_trusted: boolean;
}

export interface CodeReviewStartResult extends JobStartResult {
  scope: CodeReviewScope;
}

export interface CodeReviewFinding {
  severity: "critical" | "high" | "medium" | "low";
  title: string;
  explanation: string;
  path: string;
  line_start: number | null;
  line_end: number | null;
  evidence: string;
  suggested_fix: string;
  confidence: "high" | "medium" | "low";
}

export interface CodeReviewResult {
  session_id: string;
  job_id: string;
  status: string;
  scope?: CodeReviewScope;
  complete?: boolean;
  cancelled?: boolean;
  findings?: CodeReviewFinding[];
  summary?: {
    verdict: "approve" | "comment" | "request_changes";
    overview: string;
    finding_counts: Record<string, number>;
    changed_file_count: number;
    reviewed_file_count: number;
    omitted_file_count: number;
    truncated: boolean;
    warnings: string[];
  };
  diff?: {
    scope: CodeReviewScope;
    changed_files: string[];
    included_files: string[];
    omitted_files: Array<{ path: string; reason: string }>;
    truncated: boolean;
    warnings: string[];
    metadata: Record<string, string>;
    byte_count: number;
  };
  [key: string]: unknown;
}

export interface RunStartParams extends RunChatOptions {
  session_id?: string;
  workspace?: string;
  mode?: AlysisMode;
  message?: string;
  instruction?: string;
}

export interface JobStartResult {
  session_id: string;
  job_id: string;
  status: string;
}

export interface JobStatusResult {
  job_id: string;
  session_id: string;
  kind?: string;
  status: string;
  state?: string;
  created_at?: string | null;
  started_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  cancellable?: boolean;
  cancellation_reason?: string | null;
  cancellation_requested_at?: string | null;
  last_error?: string | null;
  exit_code?: number | null;
  error?: string | null;
  plan_id?: string | null;
  event_count?: number;
  dropped_event_count?: number;
}

export interface SessionSummary {
  session_id: string;
  workspace_root: string;
  mode: AlysisMode;
  closed: boolean;
  active_job?: JobStatusResult | null;
  last_job?: JobStatusResult | null;
}

export interface SessionListResult {
  sessions: SessionSummary[];
}

export interface SessionStatusResult extends SessionSummary {
  model: string;
  base_url: string;
  temperature?: number | null;
  stream: boolean;
  max_steps: number;
  no_log: boolean;
  yes: boolean;
  subagents_enabled: boolean;
  active_workdir: string | null;
  active_workdir_relpath: string | null;
  effective_verification_commands: string[];
  message_count: number;
  pending_images?: number;
  pending_approvals: number;
}

export interface SessionUsageResult {
  session_id: string;
  by_model: Array<Record<string, unknown>>;
  totals: Record<string, unknown>;
  call_count: number;
}

export interface ManagementResult extends Record<string, unknown> {
  secret_values_included?: false;
}

export interface WorkspaceTrustParams {
  workspace_trusted?: boolean;
}

export interface TrustedWorkspaceParams extends WorkspaceTrustParams {
  workspace_trusted: boolean;
}

export interface WorkspaceScopedParams extends WorkspaceTrustParams {
  workspace?: string;
  path?: string;
}

export interface RequiredWorkspaceParams extends WorkspaceTrustParams {
  workspace: string;
  path?: string;
}

export interface ConfigGetResult extends ManagementResult {
  config: Record<string, unknown>;
  active_profile: string;
  api_key: Record<string, unknown>;
}

export interface ConfigSetParams extends TrustedWorkspaceParams {
  key: string;
  value: unknown;
}

export interface ConfigSetResult extends ManagementResult {
  key: string;
  changed: boolean;
  config_path: string;
  /** Returned when selecting a subscription-backed model also selects its safe default effort. */
  reasoning_effort?: string;
  /** Authoritative backend check for the active subscription model/effort pair. */
  subscription_selection_ready?: boolean;
}

export interface ConfigSchemaResult extends ManagementResult {
  schema: Record<string, unknown>;
}

export interface ConfigValidateParams {
  values?: Record<string, unknown>;
}

export interface ConfigValidateResult extends ManagementResult {
  valid: boolean;
  errors: unknown[];
  config: Record<string, unknown>;
}

export interface ProfileNameParams extends WorkspaceTrustParams {
  name: string;
}

export interface TrustedProfileNameParams extends ProfileNameParams {
  workspace_trusted: boolean;
}

export interface ProfileListResult extends ManagementResult {
  active_profile: string;
  profiles: Array<Record<string, unknown>>;
}

export interface ProfileShowResult extends ManagementResult {
  profile: Record<string, unknown>;
}

export interface ProfileAddParams extends TrustedProfileNameParams {
  protocol?: string;
  base_url: string;
  api_key_env?: string;
  default_model?: string;
  web_search_adapter?: string;
  web_search_model?: string;
  notes?: string;
}

export interface ProfileMutationResult extends ManagementResult {
  changed?: boolean;
  profile?: Record<string, unknown> | null;
  action?: Record<string, unknown>;
}

export interface ProfileRemoveParams extends TrustedProfileNameParams {
  yes?: boolean;
  force?: boolean;
}

export interface ProfileUseResult extends ProfileMutationResult {
  active_profile: string;
}

export interface ProfileRenameParams extends TrustedWorkspaceParams {
  old: string;
  new: string;
}

export interface ProfilePresetsResult extends ManagementResult {
  presets: Array<Record<string, unknown>>;
}

export interface ProfilePresetParams extends TrustedWorkspaceParams {
  preset_key?: string;
  preset?: string;
  name?: string;
  base_url?: string;
  yes?: boolean;
  force?: boolean;
}

export interface ProfileConvertParams extends TrustedWorkspaceParams {
  name?: string;
  target: string;
  yes?: boolean;
  force?: boolean;
}

export interface RetainedSessionParams {
  session_id: string;
  max_events?: number;
  max_total_bytes?: number;
}

export interface SessionShowResult extends ManagementResult {
  session_id: string;
  path: string;
  events: unknown[];
  event_count: number;
  returned_event_count?: number;
  truncated: boolean;
  truncated_by_events?: boolean;
  truncated_by_bytes?: boolean;
  max_events: number;
  max_total_bytes?: number;
  response_bytes?: number;
  redacted?: boolean;
}

export interface SessionScoreParams {
  session_id?: string;
  latest?: number;
}

export interface SessionScoreResult extends ManagementResult {
  sessions_dir: string;
  scores: unknown[];
  score: unknown | null;
}

export interface ToolsCatalogResult extends ManagementResult {
  tools: Array<Record<string, unknown>>;
  count: number;
}

export interface ToolListResult extends ManagementResult {
  workspace_root: string;
  tools: Array<Record<string, unknown>>;
}

export interface ToolInfoParams extends WorkspaceScopedParams {
  name: string;
}

export interface ToolInfoResult extends ManagementResult {
  workspace_root: string;
  tools: Array<Record<string, unknown>>;
}

export interface ToolTrustParams extends ToolInfoParams {
  workspace_trusted: boolean;
}

export interface ToolTrustResult extends ManagementResult {
  workspace_root: string;
  name: string;
  trusted: boolean;
  changed: boolean;
}

export interface SkillListResult extends ManagementResult {
  skills_enabled?: boolean;
  workspace_root: string;
  skills: Array<Record<string, unknown>>;
  issues: Record<string, unknown>;
}

export interface SkillInfoParams extends WorkspaceScopedParams {
  name: string;
}

export interface SkillInfoResult extends ManagementResult {
  workspace_root: string;
  skill: Record<string, unknown>;
  info_text: string;
  issues: Record<string, unknown>;
}

export interface SkillInitParams extends WorkspaceScopedParams {
  workspace_trusted: boolean;
  name: string;
  description?: string;
  project?: boolean;
  family?: string;
  force?: boolean;
}

export interface SkillValidateParams extends WorkspaceScopedParams {
  bundle?: string;
  name?: string;
  all?: boolean;
}

export interface SkillValidateResult extends ManagementResult {
  workspace_root: string;
  results: Array<Record<string, unknown>>;
  valid: boolean;
}

export interface SkillInstallParams extends WorkspaceScopedParams {
  workspace_trusted: boolean;
  source: string;
  project?: boolean;
  subdir?: string;
  force?: boolean;
  allow_remote?: boolean;
  yes?: boolean;
  confirm?: boolean;
}

export interface SkillToggleParams extends WorkspaceScopedParams {
  workspace_trusted: boolean;
  name: string;
  project?: boolean;
}

export interface SkillToggleResult extends ManagementResult {
  workspace_root: string;
  name: string;
  enabled: boolean;
  scope: string;
  changed: boolean;
}

export interface SkillRemoveParams extends WorkspaceScopedParams {
  workspace_trusted: boolean;
  name: string;
  project?: boolean;
}

export interface DoctorSummaryResult extends ManagementResult {
  python: string;
  platform: string;
  model_set: boolean;
  api_key: Record<string, unknown>;
}

export interface DoctorProvidersResult extends ManagementResult {
  diagnostics: Record<string, unknown>;
  last_provider_call: Record<string, unknown>;
  last_web_search: Record<string, unknown>;
}

export interface DoctorBundleResult extends ManagementResult {
  bundle: Record<string, unknown>;
}

/**
 * Params for one explicit-intent live provider check. `allow_live` is required by the bridge so the
 * call can never happen passively; the extension sends it only from a direct user action
 * (Connect, Replace key, Test connection).
 */
export interface DoctorProvidersLiveParams {
  allow_live: true;
  timeout_s?: number;
}

/** Redacted outcome of the live check. `message` is already classified and secret-free. */
export interface DoctorProvidersLiveValidation {
  profile: string;
  provider_key: string;
  protocol: string;
  model: string;
  status: string;
  message: string;
}

export interface DoctorProvidersLiveResult extends ManagementResult {
  validation: DoctorProvidersLiveValidation;
  ok: boolean;
  network_used: boolean;
}

export interface SandboxDoctorParams {
  smoke?: boolean;
  include_server?: boolean;
  env?: boolean;
}

export interface SandboxDoctorResult extends ManagementResult {
  diagnostic: Record<string, unknown>;
  message: string;
}

export interface SandboxPullParams extends WorkspaceTrustParams {
  workspace_trusted: boolean;
  images?: string[];
  include_server?: boolean;
  timeout_s?: number;
}

export interface SandboxSetupParams extends WorkspaceTrustParams {
  workspace_trusted: boolean;
  pull?: boolean;
}

export interface SandboxMutationResult extends ManagementResult {
  changed?: boolean;
  result?: Record<string, unknown>;
  diagnostic?: Record<string, unknown>;
  pull_result?: Record<string, unknown> | null;
  message?: string;
}

export interface UpdateCheckParams {
  cached?: boolean;
  allow_network?: boolean;
  force?: boolean;
}

export interface UpdateCheckResult extends ManagementResult {
  status: Record<string, unknown>;
  network_used?: boolean;
  cached?: boolean;
  allow_network?: boolean;
  force?: boolean;
}

export interface ReportCreateParams extends WorkspaceScopedParams {
  workspace_trusted: boolean;
  feedback?: string;
  session_id?: string;
  run_id?: string;
  latest?: boolean;
  github?: boolean;
  local_only?: boolean;
}

export interface ReportCreateResult extends ManagementResult {
  bundle: Record<string, unknown>;
  github_issue: Record<string, unknown> | null;
  github_status_lines: string[];
  changed: boolean;
}

export interface McpWorkspaceParams extends RequiredWorkspaceParams {
  runtime?: string;
}

export interface McpStatusResult extends ManagementResult {
  servers?: Array<Record<string, unknown>>;
  tools?: Array<Record<string, unknown>>;
  prompts?: Record<string, unknown> | Array<Record<string, unknown>>;
  errors?: unknown[];
}

export interface McpServerStatusParams {
  session_id: string;
  server_id: string;
}

export interface McpServerMutationParams extends McpServerStatusParams {
  workspace_trusted: boolean;
}

export type McpServerConnectionState =
  | "disabled"
  | "connected"
  | "disconnected"
  | "not_materialized";

export interface McpServerStatusResult {
  session_id: string;
  server_id: string;
  transport: string;
  enabled: boolean;
  connection_state: McpServerConnectionState;
  connected: boolean;
  generation: number;
  catalog_initialized: boolean;
  exposed_tool_count: number;
  snapshotted_resource_count: number;
  prompt_snapshot_loaded: boolean;
  snapshotted_prompt_count: number;
  secret_values_included: false;
}

export type McpServerMutationAction = "enable" | "disable" | "restart";

export interface McpServerMutationResult extends McpServerStatusResult {
  changed: boolean;
  action: McpServerMutationAction;
}

export interface McpPromptsListParams extends McpWorkspaceParams {
  server?: string;
  query?: string;
  limit?: number;
  refresh?: boolean;
}

export interface McpPromptsListResult extends ManagementResult {
  prompts?: Array<Record<string, unknown>>;
  count?: number;
}

export interface McpPromptsGetParams extends McpWorkspaceParams {
  server_id: string;
  prompt_name?: string;
  name?: string;
  arguments?: Record<string, string>;
  refresh?: boolean;
}

export interface McpPromptsGetResult extends ManagementResult {
  prompt?: Record<string, unknown>;
  messages?: Array<Record<string, unknown>>;
  text?: string;
}

export interface McpAuthStatusParams extends RequiredWorkspaceParams {
  server?: string;
}

export interface McpAuthStatusResult extends ManagementResult {
  rows: Array<Record<string, unknown>>;
}

export interface McpAuthLoginStartParams extends RequiredWorkspaceParams {
  workspace_trusted: boolean;
  server_id: string;
}

export type McpOAuthFlowState = "pending" | "completing" | "completed" | "cancelled" | "failed" | "expired";

export interface McpAuthFlowResult extends ManagementResult {
  flow_id: string;
  server_id: string;
  kind: "authorization_code" | "device_code";
  state: McpOAuthFlowState;
  created_at: number;
  updated_at: number;
  expires_at: number;
  terminal_at: number | null;
  error_code: string | null;
  browser_url?: string;
  tokens_in_protocol_params?: false;
  authorization_code_in_protocol?: false;
}

export interface McpAuthLoginStartResult extends McpAuthFlowResult {
  supported: true;
  will_block: false;
  browser_opened_by_bridge: false;
}

export interface McpAuthLoginFlowParams extends RequiredWorkspaceParams {
  server_id: string;
  flow_id: string;
}

export interface McpAuthLoginCancelParams extends McpAuthLoginFlowParams {
  workspace_trusted: boolean;
}

export interface McpAuthLogoutParams extends RequiredWorkspaceParams {
  workspace_trusted: boolean;
  server_id: string;
  yes?: boolean;
  confirm?: boolean;
}

export interface McpAuthLogoutResult extends ManagementResult {
  server_id?: string;
  removed?: boolean;
  changed: boolean;
  action?: Record<string, unknown>;
}

export interface HooksWorkspaceParams extends RequiredWorkspaceParams {
  runtime?: string;
}

export interface HooksListResult extends ManagementResult {
  workspace_root: string;
  sources: Array<Record<string, unknown>>;
  hooks: Array<Record<string, unknown>>;
  count: number;
}

export interface HooksDoctorResult extends ManagementResult {
  workspace_root: string;
  sources: Array<Record<string, unknown>>;
  effective: Record<string, unknown>;
  matcher_errors: Array<Record<string, unknown>>;
  untrusted_project_paths: string[];
}

export interface HooksTraceParams {
  session_id?: string;
  limit?: number;
}

export interface HooksTraceResult extends ManagementResult {
  session_id: string | null;
  artifact_path?: string;
  events: Array<Record<string, unknown>>;
  count: number;
  total_count?: number;
}

export interface HooksEventParams extends HooksWorkspaceParams {
  event?: string;
  event_name?: string;
  runtime_kind?: string;
  session_source?: string;
  tool?: string;
}

export interface HooksTestResult extends ManagementResult {
  workspace_root: string;
  event: string;
  matches: Array<Record<string, unknown>>;
  ignored_untrusted_project_paths: string[];
}

export interface HooksTrustParams extends RequiredWorkspaceParams {
  workspace_trusted: boolean;
  target: "project_config";
}

export interface HooksTrustResult extends ManagementResult {
  target: string;
  workspace_root: string;
  config_path: string;
  trusted: boolean;
  changed: boolean;
}

export interface HooksInitParams extends RequiredWorkspaceParams {
  workspace_trusted: boolean;
  force?: boolean;
}

export interface HooksInitResult extends ManagementResult {
  workspace_root?: string;
  config_path?: string;
  changed: boolean;
  gitignore_changed?: boolean;
  action?: Record<string, unknown>;
}

export interface HooksEffectiveParams extends HooksWorkspaceParams {
  event?: string;
  event_name?: string;
  tool?: string;
  session_source?: string;
}

export interface HooksEffectiveResult extends ManagementResult {
  workspace_root: string;
  event: string;
  hooks: Array<Record<string, unknown>>;
  count: number;
}

export interface HooksToggleParams extends RequiredWorkspaceParams {
  workspace_trusted: boolean;
  hook_id: string;
  layer?: string;
}

export interface HooksToggleResult extends ManagementResult {
  workspace_root: string;
  config_path: string;
  hook_id: string;
  layer: string;
  enabled: boolean;
  previous_enabled: boolean;
  changed: boolean;
}

export interface ConventionsListParams {
  workspace?: string;
  path?: string;
  focus_path?: string;
}

export interface ConventionsListResult extends ManagementResult {
  workspace_root: string;
  focus_path: string;
  documents: Array<Record<string, unknown>>;
  count: number;
}

export interface ConventionsRenderParams extends ConventionsListParams {
  max_chars?: number;
  max_bytes?: number;
}

export interface ConventionsRenderResult extends ManagementResult {
  workspace_root: string;
  focus_path: string;
  rendered: string | null;
  document_count: number;
  truncated_or_limited: boolean;
  truncated?: boolean;
  truncated_by_chars?: boolean;
  truncated_by_bytes?: boolean;
  max_chars?: number;
  max_bytes?: number;
  rendered_chars?: number;
  rendered_bytes?: number;
  redacted?: boolean;
}

export interface ExtSearchParams {
  query: string;
}

export interface ExtSearchResult extends ManagementResult {
  extensions: Array<Record<string, unknown>>;
  count: number;
}

export interface ExtWorkspaceParams extends WorkspaceScopedParams {
  workspace?: string;
}

export interface ExtListResult extends ManagementResult {
  workspace_root: string;
  extensions: Array<Record<string, unknown>>;
  count: number;
  project_overrides: Record<string, unknown>;
}

export interface ExtInfoParams extends ExtWorkspaceParams {
  ext_id?: string;
  id?: string;
  plugin_id?: string;
}

export interface ExtInfoResult extends ManagementResult {
  workspace_root: string;
  extension: Record<string, unknown>;
  installed: Record<string, unknown> | null;
  installed_scopes: string[];
  enabled_effective: boolean;
  project_override_state: string;
  workspace_trust: Record<string, unknown>;
}

export interface ExtInstallParams extends ExtWorkspaceParams {
  workspace_trusted: boolean;
  source: string;
  project?: boolean;
  yes?: boolean;
  confirm?: boolean;
  trust_approval?: ExtInstallTrustApproval;
}

export interface ExtInstallTrustApproval {
  approved: true;
  plugin_id: string;
  commit: string;
  manifest_sha256: string;
  approval_fingerprint: string;
  source?: string;
  source_url?: string;
  project?: boolean;
}

export interface ExtMutationParams extends ExtWorkspaceParams {
  workspace_trusted: boolean;
  plugin_id?: string;
  ext_id?: string;
  id?: string;
  project?: boolean;
  yes?: boolean;
  confirm?: boolean;
}

export interface ExtMutationResult extends ManagementResult {
  changed: boolean;
  result?: Record<string, unknown>;
  action?: Record<string, unknown>;
}

export interface SessionHistoryMatch {
  kind: string;
  path: string;
  line: number;
  text: string;
}

export interface SessionHistoryResult {
  session_id: string;
  pattern: string;
  matches: SessionHistoryMatch[];
  truncated: boolean;
}

export interface SessionSearchMatch {
  result_id: string;
  session_id: string;
  event_type: string;
  timestamp?: string | null;
  snippet: string;
  snippet_truncated: boolean;
  context_block: Record<string, unknown>;
}

export interface SessionSearchResult {
  session_id: string;
  workspace_root: string;
  query: string;
  results: SessionSearchMatch[];
  scanned_sessions: number;
  scanned_events: number;
  scanned_bytes: number;
  truncated: boolean;
  redacted: true;
  secret_values_included: false;
}

export interface SessionContextResult {
  session_id: string;
  model_name: string;
  max_input_tokens: number | null;
  used_input_tokens: number | null;
  remaining_tokens: number | null;
  percent_left: number | null;
  source: "provider_reported" | "tokenizer_estimate" | "approximate" | "unavailable" | string;
  provider_metadata_source?: string | null;
  approximate?: boolean;
  token_usage_available?: boolean;
  token_breakdown?: Record<string, number>;
  effective_input_budget?: number | null;
  effective_remaining_tokens?: number | null;
  effective_percent_left?: number | null;
  message_count: number;
  pinned_prefix_len: number;
  [key: string]: unknown;
}

export interface SessionCompactResult {
  session_id: string;
  supported?: boolean;
  changed: boolean;
  focus: string | null;
  tokens_before: number;
  tokens_after: number;
  tokens_delta: number;
  message_count: number;
  source?: string;
  approximate?: boolean;
  reason?: string;
  chunks_before?: number;
  chunks_after?: number;
  pins_before?: number;
  pins_after?: number;
}

export interface SessionResumeResult {
  session_id: string;
  resumed_session_id: string;
  resumed: boolean;
  message: string;
  history_count: number;
  history_count_total?: number;
  bounded?: boolean;
  max_messages?: number;
  source?: string;
  model_context_replay_supported?: boolean;
  resume_context_loaded?: boolean;
  messages_before?: number;
  messages_after?: number;
  queued_prompts_rebound?: number;
  expired_prompts_recovered?: number;
  active_prompts_observed?: number;
}

export interface SessionImageEntry {
  path: string;
  relpath: string | null;
  mime_type: string | null;
  size_bytes: number | null;
}

export interface SessionImagesListResult {
  session_id: string;
  images: SessionImageEntry[];
  count: number;
  max_bytes: number;
  binary_jsonl: boolean;
}

export interface SessionImagesAddParams {
  session_id: string;
  images?: string[];
  image_paths?: string[];
  replace?: boolean;
}

export interface SessionImagesAddResult extends SessionImagesListResult {
  added_count: number;
  replaced: boolean;
}

export interface SessionImagesClearResult {
  session_id: string;
  cleared: boolean;
  count_before: number;
  count_after: number;
  images: SessionImageEntry[];
}

export interface SessionSetActiveWorkdirResult {
  session_id: string;
  source: string;
  workspace_root: string;
  previous_active_workdir: string;
  previous_active_workdir_relpath: string;
  active_workdir: string;
  active_workdir_relpath: string;
  changed: boolean;
  [key: string]: unknown;
}

export interface SessionModelInfoResult {
  session_id: string;
  model: string;
  provider: string;
  profile: string | null;
  base_url: string | null;
  base_url_redacted: boolean;
  context_window: number | null;
  vision_support: boolean | null;
  tool_support: boolean | null;
  streaming_support: boolean | null;
  source: string;
  source_metadata: Record<string, unknown>;
  secret_values_included: boolean;
}

export interface SessionSubagentsStatusResult {
  session_id: string;
  enabled: boolean;
  available: string[];
  available_count: number;
  explicit_execution_supported: boolean;
  explicit_execution_policy: string;
  lifecycle_event: string;
  execution_lifecycle: string;
  cancellation: string;
  independently_resumable: boolean;
  background_worker_surface: string;
  forge_execute_policy: string;
  secret_values_included: boolean;
  changed?: boolean;
  previous_enabled?: boolean;
  audit?: Record<string, unknown>;
}

export type SessionTraceLevel = "off" | "compact" | "full";

export interface SessionTraceStatusResult {
  session_id: string;
  supported: boolean;
  level: SessionTraceLevel;
  levels: SessionTraceLevel[];
  retained_events?: number;
  redacted: boolean;
  secret_values_included: boolean;
  max_events: number;
  max_bytes: number;
  full_trace_requires_confirmation: boolean;
  changed?: boolean;
  previous_level?: SessionTraceLevel;
  full_trace_confirmed?: boolean;
  audit?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface SessionTraceEventsResult {
  session_id: string;
  level: SessionTraceLevel;
  events: ProtocolEventEnvelope[];
  count: number;
  total_retained: number;
  bytes: number;
  redacted: boolean;
  secret_values_included: boolean;
  max_events: number;
  max_bytes: number;
  truncated: boolean;
  truncated_by_event_count?: boolean;
  truncated_by_bytes?: boolean;
  lowest_retained_sequence: number | null;
  highest_retained_sequence: number | null;
}

export interface SessionTraceArtifactResult extends ArtifactReadResult {
  session_id: string;
  redacted: boolean;
  secret_values_included: boolean;
  max_bytes: number;
}

export interface SessionTraceClearResult {
  session_id: string;
  cleared: boolean;
  events_before: number;
  events_after: number;
  redacted: boolean;
  secret_values_included: boolean;
}

export interface SessionTerminalSummary {
  process_id: string;
  cmd: string;
  cwd: string;
  cwd_relpath?: string | null;
  status: string;
  exit_code: number | null;
  runtime_s: number | null;
  started_at: string | null;
}

export interface SessionTerminalsListResult {
  session_id: string;
  supported: boolean;
  available: boolean;
  reason?: string;
  terminals: SessionTerminalSummary[];
  count: number;
  redacted: boolean;
  secret_values_included: boolean;
  arbitrary_shell_execution: boolean;
  interactive_pty_streaming: boolean;
}

export interface SessionTerminalLine {
  seq: number;
  stream: string;
  text: string;
  ts: string | null;
}

export interface SessionTerminalShowResult extends SessionTerminalsListResult {
  process_id: string;
  status?: string;
  exit_code?: number | null;
  failure_reason?: string | null;
  lines: SessionTerminalLine[];
  line_count?: number;
  next_seq?: number;
  dropped_lines?: number;
  runtime_s?: number | null;
  started_at?: string | null;
  total_bytes?: number;
  bytes?: number;
  max_lines?: number;
  max_bytes?: number;
  truncated?: boolean;
  truncated_by_line_count?: boolean;
  truncated_by_bytes?: boolean;
  killed?: boolean;
  cleared?: boolean;
}

export interface SessionClearResult {
  session_id: string;
  cleared: boolean;
  messages_before: number;
  messages_after: number;
}

export interface EventReplayResult {
  session_id: string;
  events: ProtocolEventEnvelope[];
  truncated: boolean;
  lowest_retained_sequence: number | null;
  highest_retained_sequence: number | null;
  max_events: number;
}

export interface ArtifactSummary {
  artifact_id: string;
  root: string;
  path: string;
  size_bytes: number;
}

export interface ArtifactListResult {
  session_id: string;
  artifacts: ArtifactSummary[];
  truncated: boolean;
  max_items: number;
  max_depth: number;
}

export interface ArtifactReadResult {
  session_id: string;
  artifact_id: string;
  path: string;
  size_bytes: number;
  truncated: boolean;
  max_bytes: number;
  encoding: string;
  content: string;
}

export type ManagedBrowserSnapshotKind = "semantic" | "accessibility" | "dom" | "text";
export type ManagedBrowserSessionState = "running" | "crashed";
/** `local_network` is retained only when decoding legacy sessions; the IDE never starts one. */
export type ManagedBrowserNetworkScope = "public" | "public_loopback" | "local_network";

export interface ManagedBrowserStartParams {
  session_id: string;
  workspace_trusted: boolean;
  executable_path?: string;
  network_scope?: Exclude<ManagedBrowserNetworkScope, "local_network">;
  /** @deprecated Compatibility-only. New IDE clients must use `network_scope`. */
  allow_local_destinations?: boolean;
  yes?: boolean;
  confirm?: boolean;
}

export interface ManagedBrowserSessionParams {
  session_id: string;
  browser_session_id: string;
}

export interface ManagedBrowserTrustedSessionParams extends ManagedBrowserSessionParams {
  workspace_trusted: boolean;
}

export interface ManagedBrowserTimeoutParams {
  timeout_seconds?: number;
}

export interface ManagedBrowserNavigateParams
  extends ManagedBrowserTrustedSessionParams,
    ManagedBrowserTimeoutParams {
  url: string;
}

export interface ManagedBrowserSnapshotParams
  extends ManagedBrowserSessionParams,
    ManagedBrowserTimeoutParams {
  kind?: ManagedBrowserSnapshotKind;
}

export interface ManagedBrowserScreenshotParams
  extends ManagedBrowserSessionParams,
    ManagedBrowserTimeoutParams {
  full_page?: boolean;
}

export interface ManagedBrowserArtifactReadParams extends ManagedBrowserSessionParams {
  artifact_id: string;
  offset?: number;
  max_bytes?: number;
}

export interface ManagedBrowserDiagnosticsParams
  extends ManagedBrowserSessionParams,
    ManagedBrowserTimeoutParams {
  max_events?: number;
}

export interface ManagedBrowserClickParams
  extends ManagedBrowserTrustedSessionParams,
    ManagedBrowserTimeoutParams {
  selector: string;
}

export interface ManagedBrowserTypeParams
  extends ManagedBrowserTrustedSessionParams,
    ManagedBrowserTimeoutParams {
  selector: string;
  text: string;
  replace?: boolean;
}

export interface ManagedBrowserCloseParams extends ManagedBrowserSessionParams {
  delete_artifacts?: boolean;
  yes?: boolean;
  confirm?: boolean;
}

export interface ManagedBrowserSessionStatus {
  browser_session_id: string;
  product: string;
  state: ManagedBrowserSessionState;
  created_at: number;
  network_scope: ManagedBrowserNetworkScope;
  /** @deprecated Compatibility-only mirror returned by older bridges. */
  allow_local_destinations?: boolean;
  active_url: string | null;
  artifact_count: number;
}

export interface ManagedBrowserStatusResult extends ManagedBrowserSessionStatus {
  session_id: string;
}

export interface ManagedBrowserListResult {
  session_id: string;
  browsers: ManagedBrowserSessionStatus[];
  count: number;
}

/** A secret-redacted CDP payload whose serialized size was bounded by the bridge. */
export interface ManagedBrowserBoundedPayload {
  data: unknown;
  truncated: boolean;
  size_bytes: number;
}

export interface ManagedBrowserNavigateResult extends ManagedBrowserSessionParams {
  url: string;
  result: ManagedBrowserBoundedPayload;
}

export interface ManagedBrowserDataSnapshotResult extends ManagedBrowserSessionParams {
  kind: Exclude<ManagedBrowserSnapshotKind, "text">;
  data: unknown;
  truncated: boolean;
  size_bytes: number;
}

export interface ManagedBrowserTextSnapshotResult extends ManagedBrowserSessionParams {
  kind: "text";
  text: string;
  truncated: boolean;
  size_bytes: number;
}

export type ManagedBrowserSnapshotResult =
  | ManagedBrowserDataSnapshotResult
  | ManagedBrowserTextSnapshotResult;

export interface ManagedBrowserScreenshotResult extends ManagedBrowserSessionParams {
  artifact_id: string;
  media_type: "image/png";
  size_bytes: number;
  sha256: string;
}

export interface ManagedBrowserArtifactReadResult extends ManagedBrowserSessionParams {
  artifact_id: string;
  media_type: "image/png";
  encoding: "base64";
  content: string;
  offset: number;
  next_offset: number;
  size_bytes: number;
  truncated: boolean;
}

export interface ManagedBrowserDiagnosticEvent {
  category: "console" | "network";
  method: string;
  params: ManagedBrowserBoundedPayload;
}

export interface ManagedBrowserDiagnosticsResult extends ManagedBrowserSessionParams {
  events: ManagedBrowserDiagnosticEvent[];
  truncated: boolean;
  max_events: number;
}

export interface ManagedBrowserClickResult extends ManagedBrowserSessionParams {
  clicked: boolean;
}

export interface ManagedBrowserTypeResult extends ManagedBrowserSessionParams {
  typed: boolean;
  character_count: number;
}

export interface ManagedBrowserCloseResult extends ManagedBrowserSessionParams {
  status: "closed" | "not_found";
}

export interface ApprovalRespondResult {
  session_id: string;
  approval_id: string;
  status: string;
  allow: boolean;
  allow_for_session: boolean;
  allow_for_session_supported: boolean;
  allow_for_session_scope: Record<string, unknown> | null;
  allow_for_session_warning: string | null;
}

export interface ForgePlanTask {
  task_id: string;
  title: string;
  objective: string;
  file_scope: {
    estimated_files: string[];
    write_scope: string[];
  };
  acceptance_criteria: string[];
  verification_commands: string[];
  risk_notes: string[];
  dependencies: string[];
  order: number | null;
  scope_unknown_reason: string;
  warnings: string[];
  status: string;
}

export interface ForgePlanArtifact {
  kind: string;
  artifact_id: string;
  path: string;
}

export interface ForgePlanResult {
  plan_id: string;
  session_id: string;
  job_id: string | null;
  status: string;
  source: string;
  created_session: boolean;
  project_goal: string;
  summary: string;
  warnings: string[];
  incomplete: boolean;
  tasks: ForgePlanTask[];
  artifacts: ForgePlanArtifact[];
  plan_artifact_id: string;
  plan_markdown_artifact_id: string;
  diff_count?: number;
}

export interface ForgePlanParams {
  session_id?: string;
  workspace?: string;
  instruction: string;
  idempotency_key?: string;
  mode?: AlysisMode;
  model?: string;
  base_url?: string;
  max_steps?: number;
}

export interface ForgePlanStartResult {
  session_id: string;
  job_id: string;
  status: string;
  durably_accepted?: boolean;
  duplicate?: boolean;
}

export interface ForgePlanSummary {
  plan_id: string;
  session_id: string | null;
  workspace_root: string;
  status: string;
  source: string;
  project_goal: string;
  summary: string;
  task_count: number;
  created_at: string;
  updated_at: string;
  plan_artifact_id: string;
  plan_markdown_artifact_id: string;
}

export interface ForgeListParams {
  session_id?: string;
  workspace?: string;
  max_items?: number;
}

export interface ForgeListResult {
  workspace_root: string;
  plans: ForgePlanSummary[];
  truncated: boolean;
  max_items: number;
}

export interface ForgeOpenParams {
  session_id?: string;
  workspace?: string;
  plan_id: string;
}

export interface ForgeScopedPlanParams {
  session_id: string;
  plan_id: string;
}

export interface ForgeTrustedPlanParams extends ForgeScopedPlanParams {
  workspace_trusted: boolean;
}

export interface ForgeShowResult extends ForgePlanResult {
  assets: ForgeAssetEntry[];
  legacy_assets: Record<string, unknown>[];
  artifact_count: number;
}

export interface ForgePlanStateResult extends ForgeShowResult {
  ide_revision: number;
  assistant: {
    instruction: string;
    updated_at: string | null;
    source: string;
  };
  goal: string;
  validation: Record<string, unknown>;
  changed?: boolean;
  audit?: Record<string, unknown>;
  task?: ForgePlanTask;
}

export interface ForgePlanSetAssistantParams extends ForgeTrustedPlanParams {
  instruction: string;
  expected_revision?: number;
}

export interface ForgePlanSetGoalParams extends ForgeTrustedPlanParams {
  goal: string;
  expected_revision?: number;
}

export interface ForgePlanUpdateTaskParams extends ForgeScopedPlanParams {
  task_id: string;
  title?: string;
  body?: string;
  status?: string;
  workspace_trusted?: boolean;
  expected_revision?: number;
}

export interface ForgePlanRegenerateParams extends ForgeTrustedPlanParams {
  expected_revision?: number;
  instruction?: string;
  focus?: string;
}

export interface ForgePlanRegenerateResult extends ForgePlanStateResult {
  old_revision: number;
  new_revision: number;
  redacted: boolean;
  secret_values_included: boolean;
}

export interface ForgePlanRegenerateStartResult extends JobStartResult {
  plan_id: string;
}

export interface ForgePlanRegenerateJobProgress {
  session_id: string;
  job_id: string;
  plan_id: string | null;
  status: string;
  state: string;
  complete: boolean;
  cancellable?: boolean;
  cancellation_requested?: boolean;
  cancelled?: boolean;
  cancellation_reason?: string | null;
}

export type ForgePlanRegenerateJobResult =
  | ForgePlanRegenerateResult
  | ForgePlanRegenerateJobProgress;

export interface ForgeJobProgress {
  session_id: string;
  job_id: string;
  plan_id: string | null;
  status: string;
  state: string;
  complete: boolean;
  cancellable?: boolean;
  cancellation_requested?: boolean;
  cancelled?: boolean;
  cancellation_reason?: string | null;
}

export interface ForgeSwarmApprovalScopeGrant {
  kind: string;
  scope: Record<string, unknown>;
}

export interface ForgeSwarmStartParams {
  session_id: string;
  plan_id: string;
  workspace_trusted: boolean;
  parallel?: number;
  /** Launch-time pre-grants recorded as allow-for-session scopes. */
  approval_scope_grants?: ForgeSwarmApprovalScopeGrant[];
}

export interface ForgeSwarmResumeParams {
  session_id: string;
  plan_id: string;
  job_id: string;
  workspace_trusted: boolean;
  /** Fresh grants, including the exact job/revision-scoped recovery grant; empty/stale authority fails closed. */
  approval_scope_grants: ForgeSwarmApprovalScopeGrant[];
  /** Accepted for compatibility; the persisted execution specification remains authoritative. */
  parallel?: number;
  expected_revision?: number;
}

export interface ForgeSwarmListParams {
  session_id: string;
  limit?: number;
}

export interface ForgeSwarmUsage {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  total_tokens: number;
}

export type ForgeSwarmDurableState =
  | "queued"
  | "running"
  | "interrupted"
  | "succeeded"
  | "failed"
  | "cancelled";

/** Public durable job state. Lease, worker, execution-spec, and permission secrets are excluded. */
export interface ForgeSwarmDurableStatus {
  job_id: string;
  state: ForgeSwarmDurableState;
  revision: number;
  attempts: number;
  resume_count: number;
  created_at: number;
  updated_at: number;
  started_at: number | null;
  terminal_at: number | null;
  lease_expires_at: number | null;
  result_available: boolean;
  error_code: string | null;
  error_summary: string | null;
  usage: ForgeSwarmUsage;
  resumable: boolean;
}

export interface ForgeSwarmResumeResult extends ForgeSwarmDurableStatus {
  session_id: string;
  plan_id: string;
  status: "resumed";
}

export interface ForgeSwarmListResult {
  session_id: string;
  jobs: ForgeSwarmDurableStatus[];
  count: number;
}

export interface ForgeSwarmReviewItem {
  task_id: string;
  title: string;
  status: string;
  state: string;
  reviewable: boolean;
  diff_available: boolean;
  diff_artifact_id: string | null;
  untracked_files: string[];
  untracked_files_note: string | null;
  worktree_present: boolean;
  applied: boolean;
  discarded: boolean;
  recovery: Record<string, unknown> | null;
}

export interface ForgeSwarmReviewResult {
  session_id: string;
  plan_id: string;
  items: ForgeSwarmReviewItem[];
  state_counts: Record<string, number>;
  pending_review_task_ids: string[];
  working_tree_untouched_until_apply: boolean;
}

export interface ForgeSwarmApplyResult {
  session_id: string;
  plan_id: string;
  applied: Array<Record<string, unknown>>;
  working_tree_committed: boolean;
}

export interface ForgeSwarmDiscardResult {
  session_id: string;
  plan_id: string;
  discarded: Array<Record<string, unknown>>;
}

export interface ForgeSwarmStartResult extends JobStartResult {
  plan_id: string;
  parallel: number;
}

export interface ForgeSwarmStatusResult {
  job_id: string;
  session_id: string;
  status: string;
  state: string;
  plan_id: string | null;
  cancellation_requested: boolean;
  task_status_counts?: Record<string, number>;
}

export interface ForgeSwarmRunResult {
  job_id: string;
  status: string;
  state?: string;
  complete: boolean;
  exit_code?: number;
  run_status?: string;
  clean?: boolean;
  interrupted?: boolean;
  interrupted_task_ids?: string[];
  verification_status?: string;
  reason_codes?: string[];
  task_status_counts?: Record<string, number>;
  cancelled?: boolean;
  cancellation_reason?: string | null;
}

export type ForgeSwarmJobResult = ForgeSwarmRunResult | ForgeJobProgress;

export interface ForgeSwarmReconcileParams {
  session_id: string;
  plan_id: string;
  action?: "report" | "harvest" | "discard";
  task_ids?: string[];
  base_branch?: string;
  workspace_trusted?: boolean;
  yes?: boolean;
  confirm?: boolean;
}

export interface ForgeSwarmReconcileTask {
  task_id: string;
  status: string;
  state: string;
  worktree_present: boolean;
  worktree_path: string | null;
  diff_available: boolean;
  patch_artifact_present: boolean;
  harvest_artifact_present: boolean;
  merge_commit_hash: string | null;
  branch: string | null;
}

export interface ForgeSwarmReconcileResult {
  session_id: string;
  plan_id: string;
  action: string;
  tasks: ForgeSwarmReconcileTask[];
  state_counts: Record<string, number>;
  actions: Array<Record<string, unknown>>;
  read_only: boolean;
  idempotent: boolean;
}

export interface ForgeReviewStartResult extends JobStartResult {
  plan_id: string;
  task_id: string;
}

export interface ForgeReviewParams extends ForgeTrustedPlanParams {
  task_id: string;
  model?: string;
  base_url?: string;
  temperature?: number;
}

export interface ForgeReviewResult {
  session_id: string;
  plan_id: string;
  task_id: string;
  approved: boolean;
  confidence: string;
  summary: string;
  blocking_issues_count: number;
  non_blocking_issues_count: number;
  review_json: Record<string, unknown> | null;
  review_markdown: string;
  json_artifact_id: string;
  markdown_artifact_id: string;
  requires_human_approval: boolean;
  action: Record<string, unknown> | null;
}

export interface ForgeAttachParams extends ForgeTrustedPlanParams {
  source_path?: string;
  source?: string;
  title?: string;
  description?: string;
}

export interface ForgeAssetRecord {
  id: string;
  title: string;
  description: string;
  kind: string;
  mime: string;
  original_filename: string;
  size_bytes: number;
  sha256: string;
  stored_path: string;
  extracted_text_path: string | null;
  thumbnail_path: string | null;
  pinned: boolean;
  added_at: string;
  added_by: Record<string, unknown>;
  deleted_at: string | null;
  comprehension_status: string;
  comprehension_current_version: number | null;
}

export interface ForgeAssetEntry {
  record: ForgeAssetRecord;
  comprehension_status: string;
  comprehension_source: string | null;
  comprehension_summary_preview: string;
  detected_language: string | null;
}

export interface ForgeAssetDetail {
  record: ForgeAssetRecord;
  comprehension_status: string;
  comprehension: Record<string, unknown> | null;
  versions: number[];
  extracted_text_preview: string;
}

export interface ForgeAssetsListParams extends ForgeScopedPlanParams {
  include_deleted?: boolean;
}

export interface ForgeAssetsListResult {
  session_id: string;
  plan_id: string;
  run_id: string;
  assets: ForgeAssetEntry[];
  count: number;
  include_deleted: boolean;
}

export interface ForgeAssetsShowParams extends ForgeScopedPlanParams {
  asset_id: string;
}

export interface ForgeAssetsShowResult {
  session_id: string;
  plan_id: string;
  asset: ForgeAssetDetail;
}

export interface ForgeAssetsAddParams extends ForgeTrustedPlanParams {
  source_path?: string;
  source?: string;
  title?: string;
  description?: string;
  pinned?: boolean;
  wait?: boolean;
  link?: boolean;
}

export interface ForgeAssetsMutationResult {
  session_id: string;
  plan_id: string;
  asset: ForgeAssetDetail | ForgeAssetRecord;
  status: string;
  bound_task_ids?: string[];
  comprehension_record?: Record<string, unknown> | null;
}

export interface ForgeAssetsDeleteParams extends ForgeTrustedPlanParams {
  asset_id: string;
  yes?: boolean;
  confirm?: boolean;
}

export interface ForgeAssetsEditParams extends ForgeTrustedPlanParams {
  asset_id: string;
  title?: string;
  description?: string;
  pinned?: boolean;
  refresh?: boolean;
}

export interface ForgeAssetsRefreshParams extends ForgeTrustedPlanParams {
  asset_id: string;
}

export interface ForgeAssetsCancelPendingResult {
  session_id: string;
  plan_id: string;
  cancelled_count: number;
  status: string;
  message: string;
}

export interface ForgeAssetsCheckPlanResult {
  session_id: string;
  plan_id: string;
  deleted_referenced: Record<string, string>[];
  missing_referenced: Record<string, string>[];
  pinned_added: string[];
  ok: boolean;
}

export interface ForgeAssetsPruneLegacyParams extends ForgeTrustedPlanParams {
  yes?: boolean;
}

export interface ForgeAssetsPruneLegacyResult {
  session_id: string;
  plan_id: string;
  verified: string[];
  unverified: string[];
  deleted: string[];
  requires_confirmation: boolean;
  blocked: boolean;
}

export interface ForgeExecuteParams {
  session_id: string;
  plan_id: string;
  task_ids?: string[];
  mode?: AlysisMode;
  dry_run?: boolean;
  workspace_trusted?: boolean;
  sandbox_profile?: string;
  max_steps?: number;
  no_log?: boolean;
}

export interface ForgeExecuteResult {
  session_id: string;
  plan_id: string;
  job_id: string | null;
  status: string;
}

export interface ForgeExecutePreviewParams {
  session_id: string;
  plan_id: string;
  task_ids?: string[];
  mode?: AlysisMode;
  workspace_trusted?: boolean;
  sandbox_profile?: string;
  max_steps?: number;
  no_log?: boolean;
}

export interface ForgeExecuteFileScopePreview {
  task_id: string;
  title: string;
  estimated_files: string[];
  write_scope: string[];
  scope_unknown_reason: string;
}

export interface ForgeExecuteVerificationPreview {
  task_id: string;
  commands: string[];
  source: string;
  missing_reason: string;
}

export interface ForgeExecuteApprovalPreview {
  kind: string;
  task_id: string;
  reason: string;
  scope: Record<string, unknown> | null;
  allow_for_session_scope: Record<string, unknown> | null;
  allow_for_session_supported: boolean;
}

export interface ForgeExecuteRuntimeApprovalRequirement {
  kind: string;
  reason: string;
  scope_requirement: Record<string, unknown> | null;
  allow_for_session_supported: boolean;
  warning: string | null;
}

export interface ForgeExecuteSandboxPreview {
  requested: string;
  supported: boolean;
  available: boolean;
  diagnostic: string | null;
}

export interface ForgeCancellationCapability {
  supported: boolean;
  kind: string;
  hard_interrupt: boolean;
}

export interface ForgeExecutePreviewResult {
  session_id: string;
  plan_id: string;
  selected_task_ids: string[];
  execution_mode_requested: AlysisMode;
  workspace_trust_required: boolean;
  workspace_trusted: boolean | null;
  estimated_file_scopes: ForgeExecuteFileScopePreview[];
  verification_commands: ForgeExecuteVerificationPreview[];
  required_approvals: ForgeExecuteApprovalPreview[];
  runtime_approval_requirements: ForgeExecuteRuntimeApprovalRequirement[];
  approval_scopes_safe: boolean;
  sandbox_profile: ForgeExecuteSandboxPreview;
  known_risks: string[];
  missing_prerequisites: string[];
  preview_ready: boolean;
  real_execution_supported: boolean;
  unsupported_reason: string;
  active_cancellation_supported: boolean;
  cancellation?: ForgeCancellationCapability;
  max_steps: number | null;
  no_log: boolean;
  subagents_supported: boolean;
  subagents_enabled: boolean;
  subagents_policy: string;
  next_recommended_action: string;
  status: string;
}

export interface DiffSummary {
  diff_id: string;
  session_id: string;
  plan_id: string;
  job_id: string | null;
  file_path: string;
  status: string;
  old_label: string;
  new_label: string;
  size_bytes: number;
}

export interface DiffListResult {
  diffs: DiffSummary[];
  empty_reason?: string | null;
}

export interface DiffGetResult {
  diff_id: string;
  session_id: string;
  plan_id: string;
  job_id: string | null;
  file_path: string;
  old_text: string | null;
  new_text: string | null;
  old_artifact_id: string | null;
  new_artifact_id: string | null;
  unified_diff: string;
  truncated: boolean;
  size_bytes: number;
  max_bytes: number;
  redaction?: string;
}

export class BridgeHealthError extends Error {
  public readonly code: string;
  public readonly details?: Record<string, unknown>;

  public constructor(code: string, message: string, details?: Record<string, unknown>) {
    super(message);
    this.name = "BridgeHealthError";
    this.code = code;
    this.details = details;
  }
}

export function protocolRequest(
  id: string | number | null,
  method: string,
  params: Record<string, unknown> = {}
): ProtocolRequest {
  return {
    protocol_version: PROTOCOL_VERSION,
    id,
    method,
    params
  };
}

export function parseHealthJson(stdout: string): BridgeHealth {
  let value: unknown;
  try {
    value = JSON.parse(stdout.trim());
  } catch (error) {
    throw new BridgeHealthError("invalid_json", "Alysis Code bridge health did not return JSON.", {
      cause: error instanceof Error ? error.message : String(error)
    });
  }

  return parseHealthValue(value);
}

export function parseHealthRecord(record: Record<string, unknown>): BridgeHealth {
  return parseHealthValue(record);
}

export type ProtocolMismatchDirection = "cli_outdated" | "extension_outdated" | "unknown";

/**
 * Which side of the handshake is actually behind. Telling a user to upgrade a CLI that already
 * speaks a NEWER protocol than the extension understands is exactly backwards and can never work.
 */
export function protocolMismatchDirection(
  actual: string,
  expected: string = PROTOCOL_VERSION
): ProtocolMismatchDirection {
  if (!/^\d+$/.test(actual) || !/^\d+$/.test(expected)) {
    return "unknown";
  }
  const actualVersion = Number.parseInt(actual, 10);
  const expectedVersion = Number.parseInt(expected, 10);
  if (actualVersion === expectedVersion) {
    return "unknown";
  }
  return actualVersion > expectedVersion ? "extension_outdated" : "cli_outdated";
}

export function protocolMismatchMessage(actual: string, expected: string = PROTOCOL_VERSION): string {
  const reported = actual.length > 0 ? actual : "(missing)";
  const direction = protocolMismatchDirection(actual, expected);
  if (direction === "extension_outdated") {
    return `Unsupported Alysis Code IDE protocol version: ${reported}. This CLI is newer than this extension supports (${expected}); update the Alysis Code VS Code extension — upgrading the CLI cannot resolve it.`;
  }
  if (direction === "cli_outdated") {
    return `Unsupported Alysis Code IDE protocol version: ${reported}. This extension requires ${expected}; upgrade the Alysis Code CLI.`;
  }
  return `Unsupported Alysis Code IDE protocol version: ${reported}; expected ${expected}.`;
}

function parseHealthValue(value: unknown): BridgeHealth {
  if (!isRecord(value)) {
    throw new BridgeHealthError("invalid_health", "Alysis Code bridge health must be a JSON object.");
  }

  const health = value as Partial<BridgeHealth>;
  if (health.protocol_version !== PROTOCOL_VERSION) {
    throw new BridgeHealthError(
      "unsupported_protocol_version",
      protocolMismatchMessage(String(health.protocol_version ?? "")),
      { expected: PROTOCOL_VERSION, actual: String(health.protocol_version ?? "") }
    );
  }
  if (health.ok !== true) {
    throw new BridgeHealthError("bridge_unhealthy", "Alysis Code bridge health reported not ok.");
  }
  if (!isRecord(health.capabilities)) {
    throw new BridgeHealthError("invalid_health", "Alysis Code bridge health is missing capabilities.");
  }

  const capabilities = health.capabilities as Partial<BridgeCapabilities>;
  const missing = missingRequiredMethods(capabilities.methods);
  if (missing.length > 0) {
    throw new BridgeHealthError(
      "incompatible_bridge",
      `Alysis Code bridge is missing required IDE methods: ${missing.join(", ")}.`,
      { missingMethods: missing }
    );
  }
  if (capabilities.protocol_version !== PROTOCOL_VERSION) {
    throw new BridgeHealthError(
      "unsupported_protocol_version",
      `Unsupported Alysis Code capability protocol version. ${protocolMismatchMessage(String(capabilities.protocol_version ?? ""))}`,
      { expected: PROTOCOL_VERSION, actual: String(capabilities.protocol_version ?? "") }
    );
  }
  if (capabilities.transport !== "stdio-jsonl") {
    throw new BridgeHealthError(
      "unsupported_transport",
      `Unsupported Alysis Code IDE transport: ${String(capabilities.transport ?? "(missing)")}.`
    );
  }

  return {
    ok: true,
    name: stringOrDefault(health.name, "alysis-ide-bridge"),
    alysis_version: stringOrDefault(health.alysis_version, "unknown"),
    protocol_version: PROTOCOL_VERSION,
    capabilities: {
      protocol_version: PROTOCOL_VERSION,
      methods: arrayOfStrings(capabilities.methods),
      events: arrayOfStrings(capabilities.events),
      modes: arrayOfStrings(capabilities.modes),
      transport: "stdio-jsonl",
      features: isRecord(capabilities.features) ? capabilities.features : undefined
    }
  };
}

export function missingRequiredMethods(methods: unknown): RequiredBridgeMethod[] {
  const methodSet = new Set(arrayOfStrings(methods));
  return REQUIRED_BRIDGE_METHODS.filter((method) => !methodSet.has(method));
}

export function hasBridgeMethod(capabilities: BridgeCapabilities, method: BridgeMethod): boolean {
  return capabilities.methods.includes(method);
}

export function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function arrayOfStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function stringOrDefault(value: unknown, fallback: string): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

// ---------------------------------------------------------------------------
// Session personas (session.personas.list / session.persona.set / persona_changed).
// The CLI clamp rule is authoritative: a persona may lower the session's execution
// mode, never raise it, so these payloads carry display data only.
// ---------------------------------------------------------------------------

/** CLI persona names are profile-like: bounded and shell/filename safe. */
export const PERSONA_NAME_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}$/i;
export const MAX_PERSONA_LIST_ENTRIES = 64;
const MAX_PERSONA_DESCRIPTION_LENGTH = 280;
const MAX_PERSONA_ROLE_LENGTH = 64;
const MAX_PERSONA_SOURCE_LENGTH = 32;
const MAX_PERSONA_WRITE_GLOBS = 64;
const MAX_PERSONA_WRITE_GLOB_LENGTH = 512;

export function isValidPersonaName(value: unknown): value is string {
  return typeof value === "string" && PERSONA_NAME_PATTERN.test(value);
}

/** A bridge result or event whose shape violates the persona wire contract. */
export class ProtocolPayloadError extends Error {
  public constructor(public readonly code: string, message: string) {
    super(message);
    this.name = "ProtocolPayloadError";
  }
}

export interface SessionPersonaInfo {
  name: string;
  description: string;
  /** "" means "inherit the session mode"; otherwise the persona's own ceiling before the clamp. */
  default_exec_mode: "" | AlysisMode;
  model_role: string;
  source_scope: string;
  allow_write_globs: string[];
}

export interface SessionPersonasListParams {
  session_id: string;
}

export interface SessionPersonasListResult {
  enabled: boolean;
  active: string;
  active_source: string;
  personas: SessionPersonaInfo[];
}

export interface SessionPersonaSetParams {
  session_id: string;
  persona: string;
}

export interface SessionPersonaSetResult {
  persona: string;
  effective_mode: AlysisMode;
  model_role: string;
  changed: boolean;
}

export interface PersonaChangedPayload {
  persona: string;
  effective_mode: AlysisMode;
  source: string;
}

export function parseSessionPersonasListResult(value: unknown): SessionPersonasListResult {
  if (!isRecord(value)) {
    throw personaPayloadError("session.personas.list result must be a JSON object.");
  }
  if (typeof value.enabled !== "boolean") {
    throw personaPayloadError("session.personas.list result field enabled must be a boolean.");
  }
  const active = optionalPersonaName(value.active, "active");
  const activeSource = boundedPersonaString(value.active_source, "active_source", MAX_PERSONA_SOURCE_LENGTH);
  const rawPersonas = value.personas ?? [];
  if (!Array.isArray(rawPersonas)) {
    throw personaPayloadError("session.personas.list result field personas must be an array.");
  }
  if (rawPersonas.length > MAX_PERSONA_LIST_ENTRIES) {
    throw personaPayloadError(
      `session.personas.list result exceeds ${MAX_PERSONA_LIST_ENTRIES} personas.`
    );
  }
  return {
    enabled: value.enabled,
    active,
    active_source: activeSource,
    personas: value.enabled ? rawPersonas.map((entry, index) => parsePersonaInfo(entry, index)) : []
  };
}

export function parseSessionPersonaSetResult(value: unknown): SessionPersonaSetResult {
  if (!isRecord(value)) {
    throw personaPayloadError("session.persona.set result must be a JSON object.");
  }
  if (!isValidPersonaName(value.persona)) {
    throw personaPayloadError("session.persona.set result field persona is not a valid persona name.");
  }
  if (typeof value.changed !== "boolean") {
    throw personaPayloadError("session.persona.set result field changed must be a boolean.");
  }
  return {
    persona: value.persona,
    effective_mode: requiredPersonaMode(value.effective_mode),
    model_role: boundedPersonaString(value.model_role, "model_role", MAX_PERSONA_ROLE_LENGTH),
    changed: value.changed
  };
}

export function parsePersonaChangedPayload(value: unknown): PersonaChangedPayload {
  if (!isRecord(value)) {
    throw personaPayloadError("persona_changed payload must be a JSON object.");
  }
  if (!isValidPersonaName(value.persona)) {
    throw personaPayloadError("persona_changed payload field persona is not a valid persona name.");
  }
  return {
    persona: value.persona,
    effective_mode: requiredPersonaMode(value.effective_mode),
    source: boundedPersonaString(value.source, "source", MAX_PERSONA_SOURCE_LENGTH)
  };
}

function parsePersonaInfo(value: unknown, index: number): SessionPersonaInfo {
  if (!isRecord(value)) {
    throw personaPayloadError(`personas[${index}] must be a JSON object.`);
  }
  if (!isValidPersonaName(value.name)) {
    throw personaPayloadError(`personas[${index}].name is not a valid persona name.`);
  }
  if (value.description !== undefined && typeof value.description !== "string") {
    throw personaPayloadError(`personas[${index}].description must be a string.`);
  }
  const rawGlobs = value.allow_write_globs ?? [];
  if (!Array.isArray(rawGlobs)) {
    throw personaPayloadError(`personas[${index}].allow_write_globs must be an array.`);
  }
  if (rawGlobs.length > MAX_PERSONA_WRITE_GLOBS) {
    throw personaPayloadError(`personas[${index}].allow_write_globs exceeds ${MAX_PERSONA_WRITE_GLOBS} entries.`);
  }
  const globs = rawGlobs.map((glob, globIndex) => {
    if (typeof glob !== "string" || glob.length === 0 || glob.length > MAX_PERSONA_WRITE_GLOB_LENGTH) {
      throw personaPayloadError(`personas[${index}].allow_write_globs[${globIndex}] is not a bounded string.`);
    }
    return glob;
  });
  return {
    name: value.name,
    description: (value.description ?? "").slice(0, MAX_PERSONA_DESCRIPTION_LENGTH),
    default_exec_mode: optionalPersonaMode(value.default_exec_mode, index),
    model_role: boundedPersonaString(value.model_role, `personas[${index}].model_role`, MAX_PERSONA_ROLE_LENGTH),
    source_scope: boundedPersonaString(value.source_scope, `personas[${index}].source_scope`, MAX_PERSONA_SOURCE_LENGTH),
    allow_write_globs: globs
  };
}

function optionalPersonaName(value: unknown, field: string): string {
  if (value === undefined || value === "") {
    return "";
  }
  if (!isValidPersonaName(value)) {
    throw personaPayloadError(`Persona payload field ${field} is not a valid persona name.`);
  }
  return value;
}

function requiredPersonaMode(value: unknown): AlysisMode {
  if (value === "readonly" || value === "review" || value === "auto") {
    return value;
  }
  throw personaPayloadError("Persona payload field effective_mode is not an Alysis Code mode.");
}

function optionalPersonaMode(value: unknown, index: number): "" | AlysisMode {
  if (value === undefined || value === "") {
    return "";
  }
  if (value === "readonly" || value === "review" || value === "auto") {
    return value;
  }
  throw personaPayloadError(`personas[${index}].default_exec_mode is not an Alysis Code mode.`);
}

function boundedPersonaString(value: unknown, field: string, maxLength: number): string {
  if (value === undefined) {
    return "";
  }
  if (typeof value !== "string" || value.length > maxLength) {
    throw personaPayloadError(`Persona payload field ${field} must be a string of at most ${maxLength} characters.`);
  }
  return value;
}

function personaPayloadError(message: string): ProtocolPayloadError {
  return new ProtocolPayloadError("invalid_persona_payload", message);
}
