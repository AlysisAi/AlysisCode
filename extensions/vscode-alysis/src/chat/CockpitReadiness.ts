import type { BridgeCompatibilitySnapshot } from "../client/compatibility";
import { COMMANDS } from "../commands/registry";
import type { CockpitRuntimeSnapshot } from "./CockpitRuntimeState";

/** One offer a readiness blocker or an error card can make. `command` is always a contributed id. */
export interface CockpitAction {
  label: string;
  command: string;
  args?: unknown[];
  /** At most one per card. The renderer gives it the affirmative treatment. */
  primary?: boolean;
}

/**
 * One reason a task cannot start, expressed as a ROOT CAUSE rather than a symptom: a missing CLI
 * produces one blocker, not four. `title`/`detail` are plain language with no error codes.
 */
export interface CockpitBlocker {
  id: string;
  severity: "error" | "warning";
  title: string;
  detail: string;
  actions: CockpitAction[];
  /**
   * True for a transient "still checking" state. It still fails closed (the composer stays locked)
   * but renders as neutral progress rather than a warning, because nothing is actually wrong yet.
   */
  progress?: boolean;
}

/**
 * The single composer gate. `ok` is true only when a task can actually run: the provider selection is
 * complete AND nothing error-severity is blocking the engine. Warnings (an untrusted folder that only
 * limits modes, a failed sandbox smoke) are surfaced without locking the composer.
 */
export interface CockpitReadiness {
  ok: boolean;
  blockers: CockpitBlocker[];
}

/** Alias for the same shape; the sidebar renderer imports the state-suffixed name. */
export type CockpitReadinessState = CockpitReadiness;

/** Host-owned provider/model gate, shaped like ProviderCatalogController.providerSelectionReadiness. */
export interface CockpitProviderReadiness {
  status: "loading" | "ready" | "incomplete";
  reason: string;
}

export interface CockpitReadinessInput {
  runtime: CockpitRuntimeSnapshot;
  workspaceTrusted: boolean;
  /** False when the executable-origin / Workspace-Trust guard blocks CLI execution. */
  cliTrusted: boolean;
  provider: CockpitProviderReadiness;
}

/** VS Code's built-in Workspace Trust editor; the only trust surface an extension may open. */
export const TRUST_MANAGEMENT_COMMAND = "workbench.trust.manage";

const MAX_BLOCKERS = 3;
const MAX_DETAIL_CHARS = 200;

export function cockpitAction(
  label: string,
  command: string,
  options: { args?: unknown[]; primary?: boolean } = {}
): CockpitAction {
  return {
    label,
    command,
    ...(options.args ? { args: options.args } : {}),
    ...(options.primary ? { primary: true } : {})
  };
}

/**
 * Build the readiness gate from EVERY input that decides whether a task can run: managed-runtime
 * state, CLI health, bridge process state, Workspace Trust, and provider/model selection. Engine
 * causes collapse to a single blocker (the first matching root cause) so one missing CLI never
 * renders as four cards; trust, sandbox, and provider causes are independent and stack behind it.
 */
export function buildCockpitReadiness(input: CockpitReadinessInput): CockpitReadiness {
  const blockers: CockpitBlocker[] = [];
  const engine = engineBlocker(input);
  if (engine) {
    blockers.push(engine);
  }
  const workspace = workspaceBlocker(input);
  if (workspace) {
    blockers.push(workspace);
  }
  const provider = providerBlocker(input.provider);
  if (provider) {
    blockers.push(provider);
  }
  const sandbox = sandboxBlocker(input.runtime);
  if (sandbox) {
    blockers.push(sandbox);
  }
  const deduped = dedupeAndCapBlockers(blockers);
  return {
    // Fail closed: a still-loading provider check is not a green light, and any error-severity
    // blocker keeps the composer locked even when the provider itself is ready.
    ok: input.provider.status === "ready" && !blockers.some((blocker) => blocker.severity === "error"),
    blockers: deduped
  };
}

/**
 * Defensive dedupe by id, then most-severe-first, then cap so the surface stays calm. Explicit
 * severity branches (not a lookup with a `||` fallback) so "error" ranking 0 is never swallowed.
 */
export function dedupeAndCapBlockers(blockers: readonly CockpitBlocker[]): CockpitBlocker[] {
  const seen = new Set<string>();
  const unique: CockpitBlocker[] = [];
  for (const blocker of blockers) {
    if (blocker && blocker.id && !seen.has(blocker.id)) {
      seen.add(blocker.id);
      unique.push(blocker);
    }
  }
  return unique
    .sort((left, right) => severityRank(left.severity) - severityRank(right.severity))
    .slice(0, MAX_BLOCKERS);
}

function severityRank(severity: CockpitBlocker["severity"]): number {
  if (severity === "error") {
    return 0;
  }
  return 1;
}

function engineBlocker(input: CockpitReadinessInput): CockpitBlocker | undefined {
  const runtime = input.runtime;
  const managed = isManagedRuntime(runtime);

  if (runtime.runtimeSelection.validating) {
    return {
      id: "runtime_validating",
      severity: "error",
      title: "Preparing the Alysis Code engine…",
      detail: "Validating and installing the signed runtime. Your connection will be available when this finishes.",
      actions: [],
      progress: true
    };
  }

  // Production installs own their signed runtime. Never direct those users to a separate pipx
  // executable: that would silently change the extension's trust and lifecycle boundary.
  if (runtime.runtimeSelection.origin === "unavailable") {
    return {
      id: "runtime_missing",
      severity: "error",
      title: "The Alysis Code engine is unavailable",
      detail: "The verified engine that ships with this extension could not be validated or installed.",
      actions: [
        cockpitAction("Check connection", COMMANDS.showBridgeHealth, { primary: true }),
        cockpitAction("Open setup guide", COMMANDS.openSetupGuide)
      ]
    };
  }

  // Execution is blocked before health can mean anything. Trust is the usual cause and has its own
  // one-click fix; anything else is an executable-origin block that Locate CLI resolves.
  if (!input.cliTrusted || runtime.cliOrigin.status === "blocked") {
    if (!input.workspaceTrusted) {
      return untrustedWorkspaceBlocker("error", "Alysis Code cannot start its local engine until you trust this folder.");
    }
    return {
      id: "cli_blocked",
      severity: "error",
      title: "Alysis Code cannot run this engine",
      detail: "The selected Alysis Code executable is blocked by the extension's origin checks.",
      actions: [
        cockpitAction("Locate the CLI", COMMANDS.locateCli, { primary: true }),
        cockpitAction("Open setup guide", COMMANDS.openSetupGuide)
      ]
    };
  }

  // Root cause: the engine cannot be found or launched. The fix is to LOCATE it, not to upgrade, and
  // the bridge-process error it implies is folded in here rather than carded twice.
  if (runtime.cliHealth.status === "unreachable" || runtime.cliHealth.status === "missing") {
    return {
      id: "runtime_missing",
      severity: "error",
      title: "Locate the CLI on this computer",
      detail: "Alysis Code could not find or start its local engine, so no task can run yet.",
      actions: managed
        ? [
            cockpitAction("Check connection", COMMANDS.showBridgeHealth, { primary: true }),
            cockpitAction("Open setup guide", COMMANDS.openSetupGuide)
          ]
        : [
            cockpitAction("Locate the CLI", COMMANDS.locateCli, { primary: true }),
            cockpitAction("Copy install command", COMMANDS.copyCliInstallCommand),
            cockpitAction("Open setup guide", COMMANDS.openSetupGuide)
          ]
    };
  }

  // Root cause: this extension is behind the engine. Never advise a CLI upgrade for this direction.
  if (runtime.cliHealth.status === "extension_outdated" || runtime.compatibility.protocol.direction === "extension_outdated") {
    return {
      id: "extension_outdated",
      severity: "error",
      title: "An update is needed",
      detail: "The local Alysis Code engine is newer than this extension, so the extension needs updating.",
      actions: [
        cockpitAction("Update extension", COMMANDS.checkForUpdates, { primary: true }),
        cockpitAction("Open setup guide", COMMANDS.openSetupGuide)
      ]
    };
  }

  // Root cause: the engine is present but is not responding correctly.
  if (runtime.cliHealth.status === "broken") {
    return {
      id: "cli_broken",
      severity: "error",
      title: "Setup needs attention",
      detail: "The Alysis Code engine started but is not responding correctly.",
      actions: [
        cockpitAction("Check connection", COMMANDS.showBridgeHealth, { primary: true }),
        ...(doctorSupported(runtime.compatibility) ? [cockpitAction("Check setup", COMMANDS.runDoctor)] : []),
        ...(managed ? [] : [cockpitAction("Locate the CLI", COMMANDS.locateCli)])
      ]
    };
  }

  // Root cause: the engine is OLDER than this extension. Collapse the version gap, the missing-method
  // signal, and every individually-disabled capability into ONE blocker.
  const missing = missingFeatureLabels(runtime.compatibility);
  const outdated =
    runtime.cliHealth.status === "incompatible" ||
    runtime.bridgeProtocol.status === "missing_methods" ||
    (runtime.compatibility.protocol.current !== "unknown" && missing.length > 0);
  if (outdated) {
    const blocking = !runtime.compatibility.protocol.compatible || runtime.cliHealth.status === "incompatible";
    return {
      id: "cli_outdated",
      severity: blocking ? "error" : "warning",
      title: "An update is needed",
      detail: missing.length > 0
        ? `${missing.length} ${missing.length === 1 ? "capability is" : "capabilities are"} unavailable until the local engine is updated.`
        : "The local Alysis Code engine is older than this extension.",
      actions: [
        ...(managed
          ? [cockpitAction("Check connection", COMMANDS.showBridgeHealth, { primary: true })]
          : [cockpitAction("Copy upgrade command", COMMANDS.copyCliUpgradeCommand, { primary: true })]),
        cockpitAction("Open setup guide", COMMANDS.openSetupGuide),
        ...(doctorSupported(runtime.compatibility) ? [cockpitAction("Check setup", COMMANDS.runDoctor)] : [])
      ]
    };
  }

  // Root cause: the bridge PROCESS fell over after the engine itself verified healthy. Evaluated last
  // so an engine-level cause never double-cards as a bridge failure.
  if (runtime.bridgeProcess.status === "error") {
    return {
      id: "bridge_unreachable",
      severity: "error",
      title: "Reconnect the Alysis Code engine",
      detail: "The local Alysis Code engine stopped responding.",
      actions: [cockpitAction("Check connection", COMMANDS.showBridgeHealth, { primary: true })]
    };
  }

  return undefined;
}

function workspaceBlocker(input: CockpitReadinessInput): CockpitBlocker | undefined {
  if (input.workspaceTrusted) {
    return undefined;
  }
  // Read-only work still runs in an untrusted folder, so this limits rather than blocks. When trust is
  // what blocks execution outright, engineBlocker already emitted the same id at error severity and
  // the dedupe keeps that one.
  return untrustedWorkspaceBlocker(
    "warning",
    "Alysis Code stays read-only until you trust this folder."
  );
}

function untrustedWorkspaceBlocker(severity: CockpitBlocker["severity"], detail: string): CockpitBlocker {
  return {
    id: "workspace_untrusted",
    severity,
    title: "Limited until you trust this folder",
    detail,
    actions: [cockpitAction("Trust this folder", TRUST_MANAGEMENT_COMMAND, { primary: true })]
  };
}

function providerBlocker(provider: CockpitProviderReadiness): CockpitBlocker | undefined {
  if (provider.status === "ready") {
    return undefined;
  }
  if (provider.status === "loading") {
    return {
      id: "provider_loading",
      severity: "warning",
      title: "Checking your AI connection…",
      detail: "Alysis Code is confirming the selected provider and model.",
      actions: [],
      progress: true
    };
  }
  return {
    id: "provider_incomplete",
    severity: "error",
    title: "Choose a provider and model",
    detail: boundedDetail(provider.reason) || "Connect a provider and choose a model before starting a task.",
    actions: [
      cockpitAction("Connect a provider", COMMANDS.configureProvider, { primary: true }),
      cockpitAction("Choose a model", COMMANDS.showModels)
    ]
  };
}

function sandboxBlocker(runtime: CockpitRuntimeSnapshot): CockpitBlocker | undefined {
  if (runtime.sandboxDoctor.status !== "failed") {
    return undefined;
  }
  return {
    id: "sandbox_doctor",
    severity: "warning",
    title: "The sandbox check did not pass",
    detail: "Alysis Code could not verify its sandbox, so some protected work may be unavailable.",
    actions: [
      ...(doctorSupported(runtime.compatibility) ? [cockpitAction("Check setup", COMMANDS.runDoctor, { primary: true })] : []),
      cockpitAction("Check connection", COMMANDS.showBridgeHealth)
    ]
  };
}

function isManagedRuntime(runtime: CockpitRuntimeSnapshot): boolean {
  return runtime.runtimeSelection.origin === "managed" || runtime.runtimeSelection.origin === "unavailable";
}

function doctorSupported(compatibility: BridgeCompatibilitySnapshot): boolean {
  return compatibility.features.doctor?.supported === true;
}

/**
 * Only capabilities a CLI upgrade would actually restore count as "outdated". Permanently gated
 * features (`not_applicable`: no backend exists yet) and security-model gates are not upgrade
 * problems, so they must never turn an up-to-date engine into "An update is needed".
 */
function missingFeatureLabels(compatibility: BridgeCompatibilitySnapshot): string[] {
  return Object.values(compatibility.features)
    .filter((feature) => !feature.supported && feature.reason?.category === "version_gate")
    .map((feature) => feature.label);
}

function boundedDetail(text: string): string {
  return text.trim().replace(/\s+/g, " ").slice(0, MAX_DETAIL_CHARS);
}
