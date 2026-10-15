import { CliDetectionResult, AlysisConfig, redactForDisplay } from "../client/CliDiscovery";
import {
  BridgeHealth,
  BridgeHealthError,
  ProtocolMismatchDirection,
  protocolMismatchDirection
} from "../client/AlysisProtocol";
import {
  BridgeCompatibilitySnapshot,
  emptyCompatibilitySnapshot,
  evaluateBridgeCompatibility
} from "../client/compatibility";
import type { AlysisStatusBar } from "../status/statusBar";

export type CliOriginStatus = "trusted" | "blocked" | "unknown";
// "unreachable": the CLI could not be found or launched, or the bridge transport never came up —
// the fix is to locate/point at a working CLI, not to upgrade. "incompatible": a real CLI responded
// but is too old (version/method gap) — the fix is to upgrade. "extension_outdated": the CLI speaks a
// NEWER protocol than this extension — the fix is to update the extension, and it must never be told
// to upgrade the CLI. "broken": it launched but the bridge returned bad/unhealthy data. ("missing" is
// retained for back-compat; it is treated as unreachable.)
export type CliHealthStatus =
  | "unknown"
  | "checking"
  | "ok"
  | "missing"
  | "unreachable"
  | "incompatible"
  | "extension_outdated"
  | "broken";
export type BridgeProcessStatus = "stopped" | "starting" | "ready" | "active" | "approval_needed" | "error";
export type BridgeProtocolHealthStatus = "unknown" | "ok" | "incompatible" | "missing_methods" | "error";
export type SandboxDoctorStatus = "unknown" | "running" | "ok" | "failed";
export type CockpitDiagnosticSeverity = "info" | "warning" | "error";

export interface CockpitRuntimeDiagnostic {
  id: string;
  timestamp: string;
  severity: CockpitDiagnosticSeverity;
  source: string;
  title: string;
  message: string;
  details: string | null;
  command: string | null;
  exitCode: number | null;
  stdout: string | null;
  stderr: string | null;
  rootCauseKey: string | null;
}

export interface CockpitRuntimeStatus<T extends string> {
  status: T;
  message: string | null;
}

export type CockpitRuntimeOrigin = NonNullable<AlysisConfig["runtimeSelection"]>["origin"] | "unknown";

export interface CockpitRuntimeSelection {
  origin: CockpitRuntimeOrigin;
  production: boolean;
  validating?: boolean;
  message: string | null;
}

export interface CockpitRuntimeSnapshot {
  runtimeSelection: CockpitRuntimeSelection;
  cliOrigin: CockpitRuntimeStatus<CliOriginStatus>;
  cliHealth: CockpitRuntimeStatus<CliHealthStatus>;
  bridgeProcess: CockpitRuntimeStatus<BridgeProcessStatus>;
  bridgeProtocol: CockpitRuntimeStatus<BridgeProtocolHealthStatus>;
  sandboxDoctor: CockpitRuntimeStatus<SandboxDoctorStatus>;
  compatibility: BridgeCompatibilitySnapshot;
  events: CockpitRuntimeDiagnostic[];
}

export class CockpitRuntimeState {
  private sequence = 0;
  private readonly listeners = new Set<() => void>();
  private state: CockpitRuntimeSnapshot = {
    runtimeSelection: { origin: "unknown", production: false, message: null },
    cliOrigin: { status: "unknown", message: null },
    cliHealth: { status: "unknown", message: null },
    bridgeProcess: { status: "stopped", message: "Bridge is stopped." },
    bridgeProtocol: { status: "unknown", message: null },
    sandboxDoctor: { status: "unknown", message: null },
    compatibility: emptyCompatibilitySnapshot(),
    events: []
  };

  public onDidChange(listener: () => void): { dispose(): void } {
    this.listeners.add(listener);
    return {
      dispose: () => {
        this.listeners.delete(listener);
      }
    };
  }

  public snapshot(): CockpitRuntimeSnapshot {
    return {
      runtimeSelection: { ...this.state.runtimeSelection },
      cliOrigin: { ...this.state.cliOrigin },
      cliHealth: { ...this.state.cliHealth },
      bridgeProcess: { ...this.state.bridgeProcess },
      bridgeProtocol: { ...this.state.bridgeProtocol },
      sandboxDoctor: { ...this.state.sandboxDoctor },
      compatibility: cloneCompatibility(this.state.compatibility),
      events: this.state.events.map((event) => ({ ...event }))
    };
  }

  public applyConfig(config: AlysisConfig): void {
    const selection = config.runtimeSelection;
    this.setRuntimeSelection(
      selection
        ? {
            origin: selection.origin,
            production: selection.production,
            ...(selection.validating ? { validating: true } : {}),
            message: redactForDisplay(selection.message)
          }
        : { origin: "unknown", production: false, message: null }
    );
    const cliPath = config.security?.cliPath;
    if (!cliPath) {
      this.setCliOrigin("unknown", null);
      this.setCompatibility(emptyCompatibilitySnapshot());
      return;
    }
    if (cliPath.executionAllowed) {
      const selection = config.runtimeSelection;
      if (selection?.origin === "managed") {
        this.setCliOrigin("trusted", selection.message);
      } else if (selection && !selection.production) {
        this.setCliOrigin("trusted", selection.message);
      } else {
        this.setCliOrigin("trusted", cliPath.resolvedExecutablePath ?? cliPath.value);
      }
    } else {
      this.setCliOrigin("blocked", cliPath.reason ?? "CLI execution is blocked by executable-origin guards.");
      this.setCliHealth("unknown", "CLI health was not checked because origin is blocked.");
      this.setCompatibility(emptyCompatibilitySnapshot());
    }
  }

  public applyStatusBarSnapshot(
    snapshot: ReturnType<AlysisStatusBar["snapshot"]>,
    live: { running: boolean; health?: BridgeHealth } = { running: false }
  ): void {
    // FE-19: the LIVE bridge (a running process + a completed `initialize` handshake) is the single
    // source of truth for capabilities and CLI/protocol health. Whenever it is up, the runtime reflects
    // it regardless of the status-bar string — so the Manage tree, provider pill, and runtime drawer
    // never read "needs newer CLI" / "unknown" / "stopped" while the bridge is actually serving requests.
    // The status bar (a resting "idle" by default, even before any bridge starts) only overlays the
    // activity state (active run / approval / error / missing CLI).
    // A CLI whose execution is blocked by the executable-origin / Workspace-Trust guard must NEVER be
    // un-gated from a (possibly stale, origin-trusted-at-spawn) running process — honor the block.
    const blocked = this.state.cliOrigin.status === "blocked";
    const bridgeUp = live.running && live.health !== undefined && !blocked;
    if (bridgeUp) {
      this.applyBridgeHealth(live.health as BridgeHealth);
      this.setCliHealth("ok", "Alysis Code IDE bridge is connected.");
    }
    switch (snapshot.state) {
      case "missingCli":
        // The status bar reflects a separate out-of-band detection probe. If the LIVE bridge is up and
        // handshaken, it is authoritative: a flaky "CLI missing" probe must not blank the capability
        // snapshot and re-gate the Manage tree for a CLI that is demonstrably serving requests.
        if (!bridgeUp) {
          this.setCliHealth("unreachable", snapshot.tooltip);
          this.setBridgeProcess("stopped", snapshot.tooltip);
          this.setBridgeProtocol("unknown", null);
          this.setCompatibility(emptyCompatibilitySnapshot());
        }
        break;
      case "error":
        this.setBridgeProcess("error", snapshot.tooltip);
        break;
      case "activeRun":
        this.setBridgeProcess("active", snapshot.tooltip);
        break;
      case "approvalNeeded":
        this.setBridgeProcess("approval_needed", snapshot.tooltip);
        break;
      case "bridgeOk":
        if (bridgeUp || this.hasCheckedBridgeHealth()) {
          this.setBridgeProcess("ready", snapshot.tooltip);
        } else {
          this.setBridgeProcess("stopped", snapshot.tooltip);
          this.setBridgeProtocol("unknown", null);
        }
        break;
      case "idle":
      case "untrustedWorkspace":
        // A resting status bar does NOT mean the bridge is stopped: it is "ready" while the process is
        // live (serving requests), and only "stopped" when the bridge genuinely is not running.
        this.setBridgeProcess(bridgeUp ? "ready" : "stopped", snapshot.tooltip);
        break;
    }
  }

  public applyDetectionResult(result: CliDetectionResult): void {
    if (result.ok) {
      this.applyBridgeHealth(result.health);
      this.setCliHealth("ok", result.versionOutput || "Alysis Code CLI detected.");
      this.setBridgeProcess("ready", "Alysis Code IDE bridge health is ok.");
      this.setBridgeProtocol("ok", `IDE protocol ${result.health.protocol_version}.`);
      return;
    }

    if (result.code === "cli_untrusted") {
      this.setCliOrigin("blocked", result.message);
      this.setCliHealth("unknown", "CLI health was not checked because origin is blocked.");
      this.setBridgeProcess("stopped", result.message);
      this.setBridgeProtocol("unknown", null);
      this.setCompatibility(emptyCompatibilitySnapshot());
      return;
    }
    if (result.code === "cli_missing") {
      this.setCliHealth("unreachable", result.message);
      this.setBridgeProcess("stopped", result.message);
      this.setBridgeProtocol("unknown", null);
      this.setCompatibility(emptyCompatibilitySnapshot());
      return;
    }

    const bridgeProtocol = protocolStatusFromCode(result.code);
    this.setCliHealth(
      cliHealthFromFailureCode(result.code, protocolDirectionFromDetails(result.details)),
      result.message
    );
    this.setBridgeProcess("error", result.message);
    this.setBridgeProtocol(bridgeProtocol, result.message);
    this.setCompatibility(emptyCompatibilitySnapshot());
  }

  public applyBridgeHealth(health: BridgeHealth): BridgeCompatibilitySnapshot {
    const compatibility = evaluateBridgeCompatibility(health);
    this.setCompatibility(compatibility);
    this.setBridgeProtocol(
      compatibility.protocol.compatible
        ? compatibility.features.baselineCockpit.supported
          ? "ok"
          : "missing_methods"
        : "incompatible",
      compatibility.protocol.message ??
        (compatibility.features.baselineCockpit.supported
          ? `IDE protocol ${health.protocol_version}.`
          : compatibility.features.baselineCockpit.disabledReason)
    );
    return compatibility;
  }

  public setCompatibility(compatibility: BridgeCompatibilitySnapshot): void {
    if (compatibilitySnapshotsEqual(this.state.compatibility, compatibility)) {
      return;
    }
    this.state = {
      ...this.state,
      compatibility: cloneCompatibility(compatibility)
    };
    this.emit();
  }

  private setRuntimeSelection(selection: CockpitRuntimeSelection): void {
    const current = this.state.runtimeSelection;
    if (
      current.origin === selection.origin &&
      current.production === selection.production &&
      current.validating === selection.validating &&
      current.message === selection.message
    ) {
      return;
    }
    this.state = {
      ...this.state,
      runtimeSelection: { ...selection }
    };
    this.emit();
  }

  public setCliOrigin(status: CliOriginStatus, message: string | null = null): void {
    this.setStatus("cliOrigin", status, message);
  }

  public setCliHealth(status: CliHealthStatus, message: string | null = null): void {
    this.setStatus("cliHealth", status, message);
  }

  public setBridgeProcess(status: BridgeProcessStatus, message: string | null = null): void {
    this.setStatus("bridgeProcess", status, message);
  }

  public setBridgeProtocol(status: BridgeProtocolHealthStatus, message: string | null = null): void {
    this.setStatus("bridgeProtocol", status, message);
  }

  public setSandboxDoctor(status: SandboxDoctorStatus, message: string | null = null): void {
    this.setStatus("sandboxDoctor", status, message);
  }

  public recordEvent(event: {
    severity: CockpitDiagnosticSeverity;
    source: string;
    title: string;
    message: string;
    details?: string | null;
    command?: string | null;
    exitCode?: number | null;
    stdout?: string | null;
    stderr?: string | null;
    rootCauseKey?: string | null;
  }): CockpitRuntimeDiagnostic {
    const diagnostic: CockpitRuntimeDiagnostic = {
      id: `runtime-${++this.sequence}`,
      timestamp: new Date().toISOString(),
      severity: event.severity,
      source: event.source,
      title: event.title,
      message: redactForDisplay(event.message),
      details: event.details ? redactForDisplay(event.details) : null,
      command: event.command ? redactForDisplay(event.command) : null,
      exitCode: event.exitCode ?? null,
      stdout: event.stdout ? redactForDisplay(event.stdout) : null,
      stderr: event.stderr ? redactForDisplay(event.stderr) : null,
      rootCauseKey: event.rootCauseKey ? redactForDisplay(event.rootCauseKey) : null
    };
    this.state = {
      ...this.state,
      events: [diagnostic, ...this.state.events].slice(0, 100)
    };
    this.emit();
    return diagnostic;
  }

  public recordError(
    source: string,
    title: string,
    message: string,
    details?: string | null,
    rootCauseKey?: string | null
  ): void {
    this.recordEvent({ severity: "error", source, title, message, details, rootCauseKey });
  }

  private setStatus<K extends Exclude<keyof CockpitRuntimeSnapshot, "events" | "compatibility" | "runtimeSelection">>(
    key: K,
    status: CockpitRuntimeSnapshot[K]["status"],
    message: string | null
  ): void {
    const current = this.state[key];
    if (current.status === status && current.message === message) {
      return;
    }
    this.state = {
      ...this.state,
      [key]: { status, message }
    };
    this.emit();
  }

  private emit(): void {
    for (const listener of this.listeners) {
      listener();
    }
  }

  private hasCheckedBridgeHealth(): boolean {
    return !compatibilitySnapshotsEqual(this.state.compatibility, emptyCompatibilitySnapshot());
  }
}

export function cliHealthFromFailureCode(
  code: string,
  protocolDirection: ProtocolMismatchDirection = "unknown"
): CliHealthStatus {
  // Could not find or launch a working CLI, or never established the bridge transport → locate, not upgrade.
  if (
    code === "cli_missing" ||
    code === "cli_nonzero" ||
    code === "cli_error" ||
    code === "unsupported_transport"
  ) {
    return "unreachable";
  }
  // Direction-aware: a CLI speaking a NEWER protocol than this extension is fixed by updating the
  // extension, so it must never be classified as "the CLI is too old".
  if (code === "unsupported_protocol_version" && protocolDirection === "extension_outdated") {
    return "extension_outdated";
  }
  // A real CLI responded but is too old (wrong protocol / missing the ide-bridge surface) → upgrade.
  if (
    code === "unsupported_protocol_version" ||
    code === "incompatible_bridge" ||
    code === "ide_bridge_missing"
  ) {
    return "incompatible";
  }
  // It launched but the bridge returned bad/unhealthy data → repair/retry.
  return "broken";
}

/**
 * Read the protocol mismatch direction out of a detection failure's structured details
 * (`{ expected, actual }` on an unsupported_protocol_version failure). Unknown when absent.
 */
export function protocolDirectionFromDetails(details: Record<string, unknown> | undefined): ProtocolMismatchDirection {
  const actual = typeof details?.actual === "string" ? details.actual : "";
  const expected = typeof details?.expected === "string" ? details.expected : undefined;
  if (!actual) {
    return "unknown";
  }
  return expected ? protocolMismatchDirection(actual, expected) : protocolMismatchDirection(actual);
}

export function protocolStatusFromCode(code: string): BridgeProtocolHealthStatus {
  if (code === "incompatible_bridge" || code === "ide_bridge_missing") {
    return "missing_methods";
  }
  // unsupported_transport drives cliHealth "unreachable" (the recovery card is the single source of
  // truth and early-returns); the Protocol summary row stays "incompatible" here because the bridge
  // did respond with a transport — it just isn't one this extension speaks. Informational only.
  if (code === "unsupported_protocol_version" || code === "unsupported_transport") {
    return "incompatible";
  }
  if (code === "invalid_json" || code === "invalid_health" || code === "bridge_unhealthy") {
    return "error";
  }
  return "error";
}

export function protocolStatusFromError(error: unknown): BridgeProtocolHealthStatus {
  if (error instanceof BridgeHealthError) {
    return protocolStatusFromCode(error.code);
  }
  return "error";
}

function cloneCompatibility(compatibility: BridgeCompatibilitySnapshot): BridgeCompatibilitySnapshot {
  return {
    ...compatibility,
    protocol: {
      ...compatibility.protocol,
      supported: [...compatibility.protocol.supported]
    },
    features: Object.fromEntries(
      Object.entries(compatibility.features).map(([key, feature]) => [
        key,
        {
          ...feature,
          requiredMethods: [...feature.requiredMethods],
          missingMethods: [...feature.missingMethods]
        }
      ])
    ) as BridgeCompatibilitySnapshot["features"]
  };
}

function compatibilitySnapshotsEqual(a: BridgeCompatibilitySnapshot, b: BridgeCompatibilitySnapshot): boolean {
  if (
    a.minimumRecommendedCliVersion !== b.minimumRecommendedCliVersion ||
    a.installCommand !== b.installCommand ||
    a.upgradeCommand !== b.upgradeCommand ||
    a.setupGuideUrl !== b.setupGuideUrl ||
    a.protocol.current !== b.protocol.current ||
    a.protocol.compatible !== b.protocol.compatible ||
    a.protocol.message !== b.protocol.message ||
    !stringArraysEqual(a.protocol.supported, b.protocol.supported) ||
    !compatibilityReasonsEqual(a.protocol.reason, b.protocol.reason)
  ) {
    return false;
  }
  const aEntries = Object.entries(a.features);
  const bKeys = new Set(Object.keys(b.features));
  if (aEntries.length !== bKeys.size) {
    return false;
  }
  return aEntries.every(([key, aFeature]) => {
    const bFeature = b.features[key as keyof BridgeCompatibilitySnapshot["features"]];
    return (
      bFeature !== undefined &&
      aFeature.id === bFeature.id &&
      aFeature.label === bFeature.label &&
      aFeature.supported === bFeature.supported &&
      aFeature.disabledReason === bFeature.disabledReason &&
      stringArraysEqual(aFeature.requiredMethods, bFeature.requiredMethods) &&
      stringArraysEqual(aFeature.missingMethods, bFeature.missingMethods) &&
      compatibilityReasonsEqual(aFeature.reason, bFeature.reason)
    );
  });
}

function compatibilityReasonsEqual(
  a: BridgeCompatibilitySnapshot["protocol"]["reason"],
  b: BridgeCompatibilitySnapshot["protocol"]["reason"]
): boolean {
  return (
    a?.category === b?.category &&
    a?.message === b?.message &&
    a?.detail === b?.detail &&
    stringArraysEqual(a?.missingMethods ?? [], b?.missingMethods ?? [])
  );
}

function stringArraysEqual(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}
