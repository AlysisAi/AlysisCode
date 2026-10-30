import {
  BridgeHealth,
  BridgeHealthError,
  isRecord,
  PROTOCOL_VERSION,
  protocolMismatchDirection,
  protocolMismatchMessage
} from "./AlysisProtocol";

export const SUPPORTED_IDE_PROTOCOL_VERSIONS = [PROTOCOL_VERSION] as const;
/**
 * The CLI release this extension build is tested against — the `alysis-code` version in the
 * monorepo's `pyproject.toml` when the extension is cut. It is a soft gate: an older CLI gets an
 * upgrade hint, while hard feature availability is decided per method by capability negotiation.
 * Bump it together with the extension release, and keep `docs/architecture.md` in step.
 */
export const MIN_RECOMMENDED_ALYSIS_CLI_VERSION = "0.16.0";
export const CLI_INSTALL_COMMAND = "pipx install alysis-code";
export const CLI_UPGRADE_COMMAND = "pipx upgrade alysis-code";
export const SETUP_GUIDE_URL = "https://github.com/AlysisAi/AlysisCode#install";

export const BASELINE_COCKPIT_METHODS = ["initialize", "health", "getCapabilities"] as const;

export const CHAT_RUN_METHODS = [
  "session.create",
  "chat.send",
  "run.start",
  "session.cancel",
  "approval.respond",
  "job.status",
  "session.list",
  "session.getEvents",
  "artifact.list",
  "artifact.read"
] as const;

export const FORGE_PLAN_SYNC_METHODS = ["forge.plan"] as const;
export const FORGE_PLAN_ASYNC_METHODS = ["forge.plan.start", "forge.plan.result"] as const;
export const FORGE_PLAN_PERSISTENCE_METHODS = ["forge.list", "forge.open", "forge.status"] as const;
export const FORGE_EXECUTE_PREVIEW_METHODS = ["forge.executePreview"] as const;
export const FORGE_EXECUTE_REVIEW_METHODS = [
  "forge.execute",
  "approval.respond",
  "job.status",
  "session.getEvents",
  "artifact.list",
  "artifact.read",
  "diff.list",
  "diff.get"
] as const;
export const DIFF_METHODS = ["diff.list", "diff.get"] as const;
export const ARTIFACT_METHODS = ["artifact.list", "artifact.read"] as const;
export const MANAGED_BROWSER_METHODS = [
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
  "browser.close"
] as const;

/**
 * Security facts a bridge must explicitly advertise before the extension may
 * expose managed-browser controls. Method presence alone is insufficient: an
 * older bridge could implement the same RPC names without the validating
 * proxy, DNS pinning, or exact process cleanup those controls rely on.
 */
export const MANAGED_BROWSER_SECURITY_FLAGS = [
  "supported",
  "owned_chromium_processes",
  "loopback_cdp_only",
  "private_profiles_outside_workspaces",
  "guarded_navigation",
  "redirect_and_subresource_interception",
  "persistent_child_target_interception",
  "validating_egress_proxy",
  "dns_resolution_pinned_to_numeric_connect",
  "no_direct_network_fallback",
  "loopback_proxy_bypass_removed",
  "non_proxied_udp_disabled",
  "public_destinations_by_default",
  "local_destinations_require_confirmation",
  "bounded_snapshots",
  "chunked_screenshot_artifacts",
  "bounded_diagnostics",
  "session_cleanup"
] as const;

/** Additional attestations required only for the direct-user loopback scope. */
export const MANAGED_BROWSER_DIRECT_LOOPBACK_SECURITY_FLAGS = [
  "direct_loopback_scope",
  "direct_loopback_scope_agent_isolated",
  "direct_loopback_scope_denies_lan_and_link_local"
] as const;

/** A native browser surface needs an owner session and an exact cleanup path. */
export const MANAGED_BROWSER_OWNER_METHODS = ["session.create", "session.cancel"] as const;

// Per-domain management capability groups. These replace the old all-or-nothing `management`
// monolith (which required all 62 management methods): each gates only on the method(s) its primary
// affordance needs, so a missing domain (e.g. doctor) cannot disable an unrelated one (e.g. config).
export const MANAGE_CONFIG_METHODS = ["config.get", "config.set", "config.schema", "config.validate"] as const;
export const MANAGE_PROFILES_METHODS = ["profile.list", "profile.show", "profile.use"] as const;
export const MANAGE_SESSIONS_METHODS = ["session.show", "session.usage", "session.score"] as const;
export const DOCTOR_METHODS = ["doctor.summary", "doctor.providers", "doctor.bundle"] as const;
export const SANDBOX_METHODS = ["sandbox.doctor", "sandbox.setup", "sandbox.pull"] as const;
export const TOOLS_METHODS = ["tools.catalog", "tool.list", "tool.info"] as const;
export const SKILLS_METHODS = ["skill.list", "skill.info"] as const;
export const HOOKS_METHODS = ["hooks.list", "hooks.effective"] as const;
export const CONVENTIONS_METHODS = ["conventions.list", "conventions.render"] as const;
export const MCP_METHODS = ["mcp.status", "mcp.prompts.list"] as const;
export const EXT_METHODS = ["ext.list", "ext.info", "ext.search"] as const;
export const UPDATE_METHODS = ["update.check"] as const;
export const REPORT_METHODS = ["report.create"] as const;
// Personas need both halves: listing without setting (or vice versa) renders a control that cannot
// keep its promise, so the UI lights up only when the CLI advertises the complete pair.
export const PERSONAS_METHODS = ["session.personas.list", "session.persona.set"] as const;

// Reserved slots reconciled to the REAL bridge method names now that the backend ships them.
export const FORGE_REVIEW_GATE_METHODS = ["forge.review"] as const;
export const ASSETS_METHODS = ["forge.assets.list", "forge.assets.show"] as const;
export const ASSETS_MUTATE_METHODS = ["forge.assets.add"] as const; // representative mutation (also Workspace-Trust gated)
export const PROVIDERS_PROFILES_METHODS = ["config.get", "config.set", "profile.list", "profile.use"] as const;

// Forge swarm is now wired into the IDE bridge as Workspace-Trust-gated jobs
// with cooperative cancellation, a disjoint-write-scope scheduler invariant, a
// swarm-layer write guard, and review-only merge (apply/discard per task). The
// gate is method-driven: present when the CLI advertises the swarm job + review
// methods, dimmed with a structured upgrade reason otherwise.
export const FORGE_SWARM_METHODS = [
  "forge.swarm.start",
  "forge.swarm.resume",
  "forge.swarm.list",
  "forge.swarm.status",
  "forge.swarm.result",
  "forge.swarm.cancel",
  "forge.swarm.review",
  "forge.swarm.apply",
  "forge.swarm.discard"
] as const;
// Permanently gated — no backend support exists, so these stay disabled with a clear reason.
export const FORGE_PR_METHODS = ["forge.pr"] as const;
export const DIFF_APPLY_DISCARD_METHODS = ["diff.apply", "diff.discard"] as const;
export const FULLACCESS_MODE = "fullaccess";

export type CompatibilityFeatureId =
  | "baselineCockpit"
  | "chatRun"
  | "forgePlan"
  | "forgePlanPersistence"
  | "forgeExecutePreview"
  | "forgeExecuteReview"
  | "diffs"
  | "artifacts"
  | "managedBrowser"
  | "managedBrowserDirectLoopback"
  // Per-domain management
  | "manageConfig"
  | "manageProfiles"
  | "manageSessions"
  | "doctor"
  | "sandbox"
  | "tools"
  | "skills"
  | "hooks"
  | "conventions"
  | "mcp"
  | "ext"
  | "update"
  | "report"
  | "personas"
  // Reserved slots reconciled to real methods
  | "forgeReviewGate"
  | "assets"
  | "assetsMutate"
  | "providersProfiles"
  // Permanently gated (no backend)
  | "forgePr"
  | "forgeSwarm"
  | "modesFullaccess"
  | "diffApplyDiscard";

export type CompatibilityReasonCategory =
  | "version_gate"
  | "security_model"
  | "not_applicable"
  | "untrusted"
  | "unknown";

export interface CompatibilityReason {
  category: CompatibilityReasonCategory;
  message: string;
  detail?: string;
  missingMethods?: string[];
}

export interface FeatureCompatibility {
  id: CompatibilityFeatureId;
  label: string;
  supported: boolean;
  requiredMethods: string[];
  missingMethods: string[];
  reason: CompatibilityReason | null;
  disabledReason: string | null;
}

export interface CliVersionCompatibility {
  /** The version the connected CLI reported (`unknown` when it did not report one). */
  current: string;
  minimumRecommended: string;
  /** False only when the reported version is readable AND older than the minimum. */
  satisfied: boolean;
  message: string | null;
}

export interface BridgeCompatibilitySnapshot {
  protocol: {
    current: string;
    supported: readonly string[];
    compatible: boolean;
    message: string | null;
    reason: CompatibilityReason | null;
    /** Which side is behind — an extension-outdated mismatch must never advise a CLI upgrade. */
    direction: ReturnType<typeof protocolMismatchDirection>;
  };
  /** Real comparison of the connected CLI against MIN_RECOMMENDED_ALYSIS_CLI_VERSION. */
  cliVersion?: CliVersionCompatibility;
  minimumRecommendedCliVersion: string;
  installCommand: string;
  upgradeCommand: string;
  setupGuideUrl: string;
  features: Record<CompatibilityFeatureId, FeatureCompatibility>;
}

export class FeatureCompatibilityError extends Error {
  public constructor(
    public readonly feature: CompatibilityFeatureId,
    message: string,
    public readonly missingMethods: readonly string[] = []
  ) {
    super(message);
    this.name = "FeatureCompatibilityError";
  }
}

export function evaluateBridgeCompatibility(health: BridgeHealth): BridgeCompatibilitySnapshot {
  const methods = new Set(health.capabilities.methods);
  const protocolCompatible = SUPPORTED_IDE_PROTOCOL_VERSIONS.includes(
    health.protocol_version as (typeof SUPPORTED_IDE_PROTOCOL_VERSIONS)[number]
  );
  // Direction-aware: a CLI that speaks a NEWER protocol than this extension needs an extension
  // update, so it must never be told to upgrade the CLI.
  const protocolMessage = protocolCompatible ? null : protocolMismatchMessage(health.protocol_version);

  return {
    protocol: {
      current: health.protocol_version,
      supported: SUPPORTED_IDE_PROTOCOL_VERSIONS,
      compatible: protocolCompatible,
      message: protocolMessage,
      reason: protocolCompatible ? null : compatibilityReason("version_gate", { detail: protocolMessage ?? undefined }),
      direction: protocolCompatible ? "unknown" : protocolMismatchDirection(health.protocol_version)
    },
    cliVersion: evaluateCliVersion(health.alysis_version),
    minimumRecommendedCliVersion: MIN_RECOMMENDED_ALYSIS_CLI_VERSION,
    installCommand: CLI_INSTALL_COMMAND,
    upgradeCommand: CLI_UPGRADE_COMMAND,
    setupGuideUrl: SETUP_GUIDE_URL,
    features: buildFeatures(methods, health.capabilities.modes, health.capabilities.features)
  };
}

/**
 * Real comparison against MIN_RECOMMENDED_ALYSIS_CLI_VERSION. Unreadable or missing versions are
 * reported as satisfied: a capability gap is already surfaced per feature, and a version string this
 * function cannot parse is not evidence that the CLI is too old.
 */
export function evaluateCliVersion(
  reportedVersion: string,
  minimumRecommended: string = MIN_RECOMMENDED_ALYSIS_CLI_VERSION
): CliVersionCompatibility {
  const current = reportedVersion.trim() || "unknown";
  const comparison = compareReleaseVersions(current, minimumRecommended);
  const satisfied = comparison === undefined || comparison >= 0;
  return {
    current,
    minimumRecommended,
    satisfied,
    message: satisfied
      ? null
      : `Alysis Code CLI ${current} is older than the ${minimumRecommended} this extension is tested against. Upgrade with \`${CLI_UPGRADE_COMMAND}\`.`
  };
}

/** Numeric-release comparison; undefined when either side is not a plain dotted release. */
function compareReleaseVersions(left: string, right: string): number | undefined {
  const leftParts = releaseParts(left);
  const rightParts = releaseParts(right);
  if (!leftParts || !rightParts) {
    return undefined;
  }
  for (let index = 0; index < Math.max(leftParts.length, rightParts.length); index += 1) {
    const difference = (leftParts[index] ?? 0) - (rightParts[index] ?? 0);
    if (difference !== 0) {
      return difference < 0 ? -1 : 1;
    }
  }
  return 0;
}

function releaseParts(version: string): number[] | undefined {
  // Pre-release / build metadata (1.2.3-rc1, 1.2.3+build) compares on its release core only.
  const core = version.trim().split(/[-+]/, 1)[0];
  if (!/^\d+(\.\d+)*$/.test(core)) {
    return undefined;
  }
  return core.split(".").map((part) => Number.parseInt(part, 10));
}

export function assertFeatureCompatible(
  compatibility: BridgeCompatibilitySnapshot,
  feature: CompatibilityFeatureId
): void {
  const featureState = compatibility.features[feature];
  if (compatibility.protocol.compatible && featureState.supported) {
    return;
  }
  const message = featureDisabledErrorReason(compatibility, feature);
  throw new FeatureCompatibilityError(feature, message, featureState.missingMethods);
}

export function featureDisabledReason(
  compatibility: BridgeCompatibilitySnapshot,
  feature: CompatibilityFeatureId
): string {
  const featureState = compatibility.features[feature];
  if (!compatibility.protocol.compatible) {
    return formatCompatibilityReasonWithDetail(
      compatibility.protocol.reason,
      compatibility.protocol.message ?? "The Alysis Code IDE protocol is incompatible with this extension."
    );
  }
  if (featureState.supported) {
    return `${featureState.label} is available.`;
  }
  return formatCompatibilityReasonWithDetail(
    featureState.reason,
    featureState.disabledReason ?? `${featureState.label} is unavailable.`
  );
}

export function featureGateReason(featureState: FeatureCompatibility | undefined): string {
  if (!featureState) {
    return compatibilityReason("unknown").message;
  }
  const label = featureLabel(featureState);
  if (featureState.supported) {
    return `${label} is available.`;
  }
  return formatCompatibilityReason(
    featureState.reason,
    featureState.disabledReason ?? `${label} is unavailable.`
  );
}

function featureDisabledErrorReason(
  compatibility: BridgeCompatibilitySnapshot,
  feature: CompatibilityFeatureId
): string {
  const featureState = compatibility.features[feature];
  if (!compatibility.protocol.compatible) {
    return formatCompatibilityReasonWithDetail(
      compatibility.protocol.reason,
      compatibility.protocol.message ?? "The Alysis Code IDE protocol is incompatible with this extension."
    );
  }
  if (featureState.supported) {
    return `${featureLabel(featureState)} is available.`;
  }
  return formatCompatibilityReasonWithDetail(
    featureState.reason,
    featureState.disabledReason ?? `${featureLabel(featureState)} is unavailable.`
  );
}

function featureLabel(featureState: Partial<FeatureCompatibility> | undefined): string {
  const label = featureState?.label;
  if (typeof label === "string" && label.trim()) {
    return label;
  }
  const id = featureState?.id;
  if (typeof id === "string" && id.trim()) {
    return id;
  }
  return "Feature";
}

export function formatCompatibilityReason(reason: CompatibilityReason | null | undefined, fallback?: string): string {
  if (reason?.message) {
    return reason.message;
  }
  return fallback ?? compatibilityReason("unknown").message;
}

function formatCompatibilityReasonWithDetail(reason: CompatibilityReason | null | undefined, fallback?: string): string {
  const message = formatCompatibilityReason(reason, fallback);
  if (reason?.detail && reason.detail !== message) {
    return `${message}. ${reason.detail}`;
  }
  return message;
}

export function emptyCompatibilitySnapshot(): BridgeCompatibilitySnapshot {
  const message = "Alysis Code CLI compatibility has not been checked.";
  return {
    protocol: {
      current: "unknown",
      supported: SUPPORTED_IDE_PROTOCOL_VERSIONS,
      compatible: false,
      message,
      reason: compatibilityReason("unknown", { detail: message }),
      direction: "unknown"
    },
    minimumRecommendedCliVersion: MIN_RECOMMENDED_ALYSIS_CLI_VERSION,
    installCommand: CLI_INSTALL_COMMAND,
    upgradeCommand: CLI_UPGRADE_COMMAND,
    setupGuideUrl: SETUP_GUIDE_URL,
    features: markFeaturesUnknown(buildFeatures(new Set<string>(), [], undefined), message)
  };
}

// Sibling of cliHealthFromFailureCode (CockpitRuntimeState) for the live-operation compatibility
// path. Keep the two in lockstep: "unreachable" = can't find/launch/reach the CLI (locate, not
// upgrade); "incompatible" = a real CLI responded but is too old (version/method gap); "broken" =
// it launched but returned bad/unhealthy data.
export function cliHealthStatusForCompatibilityError(error: unknown): "unreachable" | "incompatible" | "broken" {
  if (error instanceof FeatureCompatibilityError) {
    return "incompatible";
  }
  if (error instanceof BridgeHealthError) {
    if (
      error.code === "unsupported_protocol_version" ||
      error.code === "incompatible_bridge" ||
      error.code === "ide_bridge_missing"
    ) {
      return "incompatible";
    }
    // The bridge advertised a transport this extension can't speak — the connection never came up,
    // so locate a working CLI rather than offer Upgrade (matches cliHealthFromFailureCode).
    if (error.code === "unsupported_transport") {
      return "unreachable";
    }
    return "broken";
  }
  if (error instanceof Error && "code" in error && (error as NodeJS.ErrnoException).code === "ENOENT") {
    return "unreachable";
  }
  return "broken";
}

function buildFeatures(
  methods: Set<string>,
  modes: readonly string[] | undefined,
  features: unknown
): Record<CompatibilityFeatureId, FeatureCompatibility> {
  const planExecution = planExecutionCompatibility(methods);
  return {
    baselineCockpit: requiredFeature("baselineCockpit", "Baseline Cockpit", BASELINE_COCKPIT_METHODS, methods),
    chatRun: requiredFeature("chatRun", "Chat and Run", CHAT_RUN_METHODS, methods),
    forgePlan: {
      id: "forgePlan",
      label: "Forge Plan",
      supported: planExecution.supported,
      requiredMethods: planExecution.requiredMethods,
      missingMethods: planExecution.missingMethods,
      reason: planExecution.supported
        ? null
        : compatibilityReason("version_gate", {
            detail: "Forge Plan requires forge.plan or the async forge.plan.start and forge.plan.result methods.",
            missingMethods: planExecution.missingMethods
          }),
      disabledReason: planExecution.supported
        ? null
        : "Forge Plan requires forge.plan or the async forge.plan.start and forge.plan.result methods."
    },
    forgePlanPersistence: requiredFeature("forgePlanPersistence", "Forge Plan Persistence", FORGE_PLAN_PERSISTENCE_METHODS, methods),
    forgeExecutePreview: requiredFeature("forgeExecutePreview", "Forge Execute Preview", FORGE_EXECUTE_PREVIEW_METHODS, methods),
    forgeExecuteReview: requiredFeature("forgeExecuteReview", "Forge Execute Review", FORGE_EXECUTE_REVIEW_METHODS, methods),
    diffs: requiredFeature("diffs", "Diffs", DIFF_METHODS, methods),
    artifacts: requiredFeature("artifacts", "Artifacts", ARTIFACT_METHODS, methods),
    managedBrowser: managedBrowserCompatibility(features, methods),
    managedBrowserDirectLoopback: managedBrowserDirectLoopbackCompatibility(features, methods),
    // Per-domain management — each independent.
    manageConfig: requiredFeature("manageConfig", "Configuration", MANAGE_CONFIG_METHODS, methods),
    manageProfiles: requiredFeature("manageProfiles", "Profiles", MANAGE_PROFILES_METHODS, methods),
    manageSessions: requiredFeature("manageSessions", "Session Management", MANAGE_SESSIONS_METHODS, methods),
    doctor: requiredFeature("doctor", "Doctor", DOCTOR_METHODS, methods),
    sandbox: requiredFeature("sandbox", "Sandbox", SANDBOX_METHODS, methods),
    tools: requiredFeature("tools", "Tools", TOOLS_METHODS, methods),
    skills: requiredFeature("skills", "Skills", SKILLS_METHODS, methods),
    hooks: requiredFeature("hooks", "Hooks", HOOKS_METHODS, methods),
    conventions: requiredFeature("conventions", "Conventions", CONVENTIONS_METHODS, methods),
    mcp: requiredFeature("mcp", "MCP", MCP_METHODS, methods),
    ext: requiredFeature("ext", "Extensions", EXT_METHODS, methods),
    update: requiredFeature("update", "Update Check", UPDATE_METHODS, methods),
    report: requiredFeature("report", "Reports", REPORT_METHODS, methods),
    personas: requiredFeature("personas", "Personas", PERSONAS_METHODS, methods),
    // Reserved slots reconciled to real methods.
    forgeReviewGate: requiredFeature("forgeReviewGate", "Review Gate", FORGE_REVIEW_GATE_METHODS, methods),
    assets: forgeAssetsCompatibility(features, methods),
    assetsMutate: requiredFeature("assetsMutate", "Asset Editing", ASSETS_MUTATE_METHODS, methods),
    providersProfiles: requiredFeature("providersProfiles", "Providers & Profiles", PROVIDERS_PROFILES_METHODS, methods),
    // Permanently gated — no backend.
    forgePr: permanentlyGated(
      "forgePr",
      "Open as PR",
      FORGE_PR_METHODS,
      compatibilityReason("not_applicable", {
        detail: "Open-as-PR is not yet supported by the Alysis Code IDE bridge.",
        missingMethods: FORGE_PR_METHODS
      })
    ),
    forgeSwarm: requiredFeature("forgeSwarm", "Run Swarm", FORGE_SWARM_METHODS, methods),
    modesFullaccess: modesFullaccessFeature(modes, features),
    diffApplyDiscard: permanentlyGated(
      "diffApplyDiscard",
      "Keep / Discard",
      DIFF_APPLY_DISCARD_METHODS,
      compatibilityReason("not_applicable", {
        detail: "Keep/Discard needs a commit/revert bridge capability that does not exist yet; use VS Code Source Control.",
        missingMethods: DIFF_APPLY_DISCARD_METHODS
      })
    )
  };
}

function compatibilityReason(
  category: CompatibilityReasonCategory,
  options: { detail?: string; missingMethods?: readonly string[] } = {}
): CompatibilityReason {
  const missingMethods = [...(options.missingMethods ?? [])];
  const methodSuffix = missingMethods.length > 0 ? ` — ${missingMethods.join(", ")}` : "";
  const base = (() => {
    switch (category) {
      case "version_gate":
        return "Needs a newer Alysis Code CLI";
      case "security_model":
        return "Blocked pending the security model";
      case "not_applicable":
        return "Not available in the IDE";
      case "untrusted":
        return "Needs Workspace Trust";
      case "unknown":
        return "Alysis Code CLI compatibility has not been checked.";
    }
  })();
  return {
    category,
    message: category === "version_gate" ? `${base}${methodSuffix}` : base,
    ...(options.detail ? { detail: options.detail } : {}),
    ...(missingMethods.length > 0 ? { missingMethods } : {})
  };
}

function markFeaturesUnknown(
  features: Record<CompatibilityFeatureId, FeatureCompatibility>,
  detail: string
): Record<CompatibilityFeatureId, FeatureCompatibility> {
  return Object.fromEntries(
    Object.entries(features).map(([id, feature]) => [
      id,
      {
        ...feature,
        supported: false,
        reason: compatibilityReason("unknown", { detail }),
        disabledReason: detail
      }
    ])
  ) as Record<CompatibilityFeatureId, FeatureCompatibility>;
}

function requiredFeature(
  id: CompatibilityFeatureId,
  label: string,
  requiredMethods: readonly string[],
  methods: Set<string>
): FeatureCompatibility {
  const missingMethods = requiredMethods.filter((method) => !methods.has(method));
  const supported = missingMethods.length === 0;
  const disabledReason = `${label} is disabled because the installed Alysis Code CLI does not expose all required IDE bridge methods.`;
  return {
    id,
    label,
    supported,
    requiredMethods: [...requiredMethods],
    missingMethods,
    reason: supported ? null : compatibilityReason("version_gate", { detail: disabledReason, missingMethods }),
    disabledReason: supported ? null : disabledReason
  };
}

function permanentlyGated(
  id: CompatibilityFeatureId,
  label: string,
  requiredMethods: readonly string[],
  reason: CompatibilityReason
): FeatureCompatibility {
  // Truly permanent: no backend exists, so this stays unsupported regardless of the advertised
  // method set, with a clear reason. (Re-wire to a method-driven gate if the bridge ever ships it.)
  return {
    id,
    label,
    supported: false,
    requiredMethods: [...requiredMethods],
    missingMethods: [...requiredMethods],
    reason,
    disabledReason: reason.detail ?? reason.message
  };
}

function modesFullaccessFeature(modes: readonly string[] | undefined, features: unknown): FeatureCompatibility {
  const list = modes ?? [];
  const blockedReason = unsafeModeSecurityReason(features, FULLACCESS_MODE);
  const supported = blockedReason ? false : list.includes(FULLACCESS_MODE);
  const disabledReason = "Fullaccess mode is not advertised by this Alysis Code CLI.";
  return {
    id: "modesFullaccess",
    label: "Fullaccess Mode",
    supported,
    requiredMethods: [`capabilities.modes includes "${FULLACCESS_MODE}"`],
    missingMethods: supported ? [] : [`capabilities.modes "${FULLACCESS_MODE}"`],
    reason: supported
      ? null
      : blockedReason ?? compatibilityReason("version_gate", { detail: disabledReason, missingMethods: [`capabilities.modes "${FULLACCESS_MODE}"`] }),
    disabledReason: supported ? null : blockedReason?.detail ?? disabledReason
  };
}

function forgeAssetsCompatibility(features: unknown, methods: Set<string>): FeatureCompatibility {
  // Prefer the bridge's explicit capability flag (features.forge.assets.supported) when present;
  // fall back to method-presence (forge.assets.list + show) for CLIs that don't advertise the flag.
  const explicitSupported = readBooleanFeature(features, ["forge", "assets", "supported"]);
  if (explicitSupported !== undefined) {
    return {
      id: "assets",
      label: "Assets",
      supported: explicitSupported,
      requiredMethods: ["features.forge.assets.supported"],
      missingMethods: explicitSupported ? [] : ["features.forge.assets.supported"],
      reason: explicitSupported
        ? null
        : compatibilityReason("version_gate", {
            detail: "Assets are disabled because the Alysis Code CLI explicitly reports Forge assets as unsupported.",
            missingMethods: ["features.forge.assets.supported"]
          }),
      disabledReason: explicitSupported
        ? null
        : "Assets are disabled because the Alysis Code CLI explicitly reports Forge assets as unsupported."
    };
  }
  const compatibility = requiredFeature("assets", "Assets", ASSETS_METHODS, methods);
  return {
    ...compatibility,
    reason: compatibility.supported
      ? null
      : compatibilityReason("version_gate", {
          detail: "Assets are disabled because the installed Alysis Code CLI does not expose the Forge asset IDE bridge methods.",
          missingMethods: compatibility.missingMethods
        }),
    disabledReason: compatibility.supported
      ? null
      : "Assets are disabled because the installed Alysis Code CLI does not expose the Forge asset IDE bridge methods."
  };
}

function managedBrowserCompatibility(
  features: unknown,
  methods: Set<string>
): FeatureCompatibility {
  const requiredMethods = [...MANAGED_BROWSER_METHODS, ...MANAGED_BROWSER_OWNER_METHODS];
  const missingMethods = requiredMethods.filter((method) => !methods.has(method));
  const missingSecurityFacts = MANAGED_BROWSER_SECURITY_FLAGS
    .filter((flag) => readBooleanFeature(features, ["managed_browser", flag]) !== true)
    .map((flag) => `features.managed_browser.${flag}`);
  const supported = missingMethods.length === 0 && missingSecurityFacts.length === 0;
  const missing = [...missingMethods, ...missingSecurityFacts];
  const detail = missingMethods.length > 0
    ? "Managed Browser needs the complete owner-scoped browser lifecycle from a newer Alysis Code CLI."
    : "Managed Browser is blocked because the bridge did not attest every required browser security guarantee.";
  return {
    id: "managedBrowser",
    label: "Managed Browser",
    supported,
    requiredMethods: [
      ...requiredMethods,
      ...MANAGED_BROWSER_SECURITY_FLAGS.map((flag) => `features.managed_browser.${flag}`)
    ],
    missingMethods: missing,
    reason: supported
      ? null
      : compatibilityReason(missingMethods.length > 0 ? "version_gate" : "security_model", {
          detail,
          missingMethods: missing
        }),
    disabledReason: supported ? null : detail
  };
}

function managedBrowserDirectLoopbackCompatibility(
  features: unknown,
  methods: Set<string>
): FeatureCompatibility {
  const base = managedBrowserCompatibility(features, methods);
  const missingSecurityFacts = MANAGED_BROWSER_DIRECT_LOOPBACK_SECURITY_FLAGS
    .filter((flag) => readBooleanFeature(features, ["managed_browser", flag]) !== true)
    .map((flag) => `features.managed_browser.${flag}`);
  const missing = [...base.missingMethods, ...missingSecurityFacts];
  const supported = base.supported && missingSecurityFacts.length === 0;
  const detail = base.supported
    ? "Direct IDE loopback browsing is blocked because the bridge did not attest actor isolation and LAN/link-local denial."
    : base.disabledReason || "Managed Browser is not supported by this Alysis Code CLI.";
  return {
    id: "managedBrowserDirectLoopback",
    label: "Direct IDE Loopback Browser",
    supported,
    requiredMethods: [
      ...base.requiredMethods,
      ...MANAGED_BROWSER_DIRECT_LOOPBACK_SECURITY_FLAGS.map(
        (flag) => `features.managed_browser.${flag}`
      )
    ],
    missingMethods: missing,
    reason: supported
      ? null
      : compatibilityReason(base.supported ? "security_model" : (base.reason?.category ?? "version_gate"), {
          detail,
          missingMethods: missing
        }),
    disabledReason: supported ? null : detail
  };
}

function unsafeModeSecurityReason(features: unknown, mode: string): CompatibilityReason | undefined {
  for (const path of [
    ["forge", "unsafe_modes"],
    ["unsafe_modes"]
  ]) {
    const entry = readRecordFeature(features, path);
    if (!entry) {
      continue;
    }
    const modeEntry = isRecord(entry[mode]) ? entry[mode] : undefined;
    if (modeEntry && isBlockedUntilSecurityModel(modeEntry.status)) {
      return compatibilityReason("security_model", { detail: statusDetail(modeEntry) });
    }
    if (isBlockedUntilSecurityModel(entry.status)) {
      return compatibilityReason("security_model", { detail: statusDetail(entry) });
    }
    const blockedModes = entry.blocked_until_security_model;
    if (Array.isArray(blockedModes) && blockedModes.includes(mode)) {
      return compatibilityReason("security_model", { detail: statusDetail(entry) });
    }
  }
  for (const path of [
    ["unsafe_modes", mode],
    ["modes", mode],
    ["forge", "modes", mode]
  ]) {
    const entry = readRecordFeature(features, path);
    if (entry && isBlockedUntilSecurityModel(entry.status)) {
      return compatibilityReason("security_model", { detail: statusDetail(entry) });
    }
  }
  return undefined;
}

function readBooleanFeature(features: unknown, path: readonly string[]): boolean | undefined {
  let current: unknown = features;
  for (const segment of path) {
    if (!isRecord(current)) {
      return undefined;
    }
    current = current[segment];
  }
  return typeof current === "boolean" ? current : undefined;
}

function readRecordFeature(features: unknown, path: readonly string[]): Record<string, unknown> | undefined {
  let current: unknown = features;
  for (const segment of path) {
    if (!isRecord(current)) {
      return undefined;
    }
    current = current[segment];
  }
  return isRecord(current) ? current : undefined;
}

function isBlockedUntilSecurityModel(value: unknown): boolean {
  return value === "blocked_until_security_model";
}

function statusDetail(entry: Record<string, unknown>): string | undefined {
  const detail = entry.rationale ?? entry.reason ?? entry.message;
  return typeof detail === "string" && detail.trim().length > 0 ? detail : undefined;
}

function planExecutionCompatibility(methods: Set<string>): Pick<FeatureCompatibility, "supported" | "requiredMethods" | "missingMethods"> {
  if (methods.has("forge.plan") || (methods.has("forge.plan.start") && methods.has("forge.plan.result"))) {
    return {
      supported: true,
      requiredMethods: ["forge.plan", "forge.plan.start", "forge.plan.result"],
      missingMethods: []
    };
  }
  const missingMethods = ["forge.plan"];
  if (!methods.has("forge.plan.start")) {
    missingMethods.push("forge.plan.start");
  }
  if (!methods.has("forge.plan.result")) {
    missingMethods.push("forge.plan.result");
  }
  return {
    supported: false,
    requiredMethods: ["forge.plan", "forge.plan.start", "forge.plan.result"],
    missingMethods
  };
}
