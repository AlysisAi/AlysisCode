import { AlysisConfig } from "../client/CliDiscovery";
import {
  BrowserCockpitSummaryState,
  browserCockpitSummary,
  emptyBrowserCockpitState
} from "../browser/BrowserCockpitController";
import { emptyCompatibilitySnapshot } from "../client/compatibility";
import { AlysisStatusBar } from "../status/statusBar";
import { CockpitReadiness } from "./CockpitReadiness";
import { CockpitRuntimeSnapshot } from "./CockpitRuntimeState";
import { ChatState } from "./ChatTranscript";

export type { CockpitAction, CockpitBlocker, CockpitReadiness, CockpitReadinessState } from "./CockpitReadiness";

// One management-action run, surfaced in the cockpit (running -> ok/error). The payload is the
// redacted structured bridge result (rendered as kv/table/raw text); error is a redacted message.
// Backend actions are not cancellable, so cancellable is false (honors cancel_not_supported).
export interface ActionResultEntry {
  id: string;
  actionId: string;
  title: string;
  status: "running" | "ok" | "error";
  mutates: boolean;
  cancellable: boolean;
  payload: unknown;
  error: string | null;
}

// One selectable provider profile (a redacted projection of a profile.list entry). base_url/model
// are safe to show; secrets never enter this shape (profiles flow through redactDeep first).
export interface ProfileOption {
  name: string;
  baseUrl: string;
  model: string;
  active: boolean;
}

// The header provider/profile switcher's data, cached from profile.list. supported=false when the
// bridge cannot list profiles (the control renders informational/disabled with a reason, not faked).
export interface ProviderProfileState {
  supported: boolean;
  activeProfile: string;
  profiles: ProfileOption[];
}

// One selectable persona, projected for the cockpit. writeScoped is derived from the CLI's
// allow_write_globs (non-empty means the persona writes only inside a narrow scope, e.g. the
// architect writing markdown only); the raw globs never leave the host — the description carries
// the human-readable hint.
export interface PersonaOption {
  name: string;
  description: string;
  /** "" inherits the session mode; otherwise the persona's own ceiling before the clamp. */
  defaultExecMode: "" | AlysisConfig["defaultMode"];
  modelRole: string;
  sourceScope: string;
  writeScoped: boolean;
}

// The persona surface for the cockpit. Sourced from session.personas.list when the bridge
// advertises the complete pair of persona methods; `reason` carries the standard capability
// affordance ("Needs a newer Alysis Code CLI...") when it does not. The CLI clamp rule means a
// persona can only narrow the session's execution mode, so no trust gate lives here.
export interface PersonasSurfaceState {
  supported: boolean;
  reason: string | null;
  enabled: boolean;
  active: string;
  activeSource: string;
  available: PersonaOption[];
}

// Redacted live model/provider metadata for the active IDE session. This is sourced from
// session.modelInfo when the bridge advertises it; it deliberately excludes secret-bearing fields.
export interface SessionModelState {
  supported: boolean;
  switchSupported: boolean;
  sessionId: string | null;
  model: string;
  provider: string;
  profile: string | null;
  source: string;
  error: string | null;
}

export interface CockpitStatusState {
  mode: AlysisConfig["defaultMode"];
  model: string;
  provider: string;
  sandbox: AlysisConfig["sandboxProfile"];
  workspaceTrusted: boolean;
  bridgeStatus: ReturnType<AlysisStatusBar["snapshot"]>;
  cliTrusted: boolean;
  cliReason: string | null;
  runtime: CockpitRuntimeSnapshot;
  providerProfile: ProviderProfileState;
  sessionModel: SessionModelState;
}

export interface CockpitState extends ChatState {
  /** Bounded correlated-submit acknowledgement; never contains draft or message text. */
  lastAcceptedTaskRequestId: string | null;
  /**
   * The single composer gate. `ok` is true only when a task can actually run — engine, trust, and
   * provider selection all included — and `blockers` is the deduped, severity-sorted root-cause list.
   */
  readiness: CockpitReadiness;
  /** Session persona surface; hidden by the UI when unsupported or disabled CLI-side. */
  personas: PersonasSurfaceState;
  cockpit: {
    status: CockpitStatusState;
    browser: BrowserCockpitSummaryState;
    actionResults: ActionResultEntry[];
  };
}

export function emptyCockpitState(chat: ChatState): CockpitState {
  return {
    ...chat,
    lastAcceptedTaskRequestId: null,
    // Fail closed: nothing has been checked yet, so the composer stays locked until a real
    // readiness projection replaces this.
    readiness: { ok: false, blockers: [] },
    personas: emptyPersonasState(),
    cockpit: {
      status: {
        mode: "readonly",
        model: "",
        provider: "",
        sandbox: "default",
        workspaceTrusted: false,
        bridgeStatus: {
          state: "idle",
          text: "Alysis Code: idle",
          tooltip: "Alysis Code is ready",
          visible: true
        },
        cliTrusted: false,
        cliReason: null,
        providerProfile: { supported: false, activeProfile: "", profiles: [] },
        sessionModel: emptySessionModelState(),
        runtime: {
          runtimeSelection: { origin: "unknown", production: false, message: null },
          cliOrigin: { status: "unknown", message: null },
          cliHealth: { status: "unknown", message: null },
          bridgeProcess: { status: "stopped", message: "Bridge is stopped." },
          bridgeProtocol: { status: "unknown", message: null },
          sandboxDoctor: { status: "unknown", message: null },
          compatibility: emptyCompatibilitySnapshot(),
          events: []
        }
      },
      browser: browserCockpitSummary(emptyBrowserCockpitState()),
      actionResults: []
    }
  };
}

export function emptySessionModelState(): SessionModelState {
  return {
    supported: false,
    switchSupported: false,
    sessionId: null,
    model: "",
    provider: "",
    profile: null,
    source: "",
    error: null
  };
}

export function emptyPersonasState(): PersonasSurfaceState {
  return {
    supported: false,
    reason: null,
    enabled: false,
    active: "",
    activeSource: "",
    available: []
  };
}

const PERSONA_MODE_RANK: Record<AlysisConfig["defaultMode"], number> = {
  readonly: 0,
  review: 1,
  auto: 2
};

/**
 * THE CLAMP RULE, mirrored from the CLI: a persona may LOWER the session's execution mode, never
 * raise it. An architect persona (review) in a readonly session stays readonly; in an auto session
 * it clamps down to review. "" means the persona inherits the session mode unchanged. The host
 * precomputes this for every picker row so the webview renders, never decides.
 */
export function clampPersonaMode(
  defaultExecMode: "" | AlysisConfig["defaultMode"],
  sessionMode: AlysisConfig["defaultMode"]
): AlysisConfig["defaultMode"] {
  if (!defaultExecMode) {
    return sessionMode;
  }
  return PERSONA_MODE_RANK[defaultExecMode] < PERSONA_MODE_RANK[sessionMode] ? defaultExecMode : sessionMode;
}
