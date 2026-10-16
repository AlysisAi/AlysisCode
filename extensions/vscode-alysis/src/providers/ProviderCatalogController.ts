import * as vscode from "vscode";

import { redactDeep, redactForDisplay, type AlysisConfig } from "../client/CliDiscovery";
import { ProtocolClientError, type AlysisBridgeClient } from "../client/AlysisBridgeClient";
import { CLI_UPGRADE_COMMAND } from "../client/compatibility";
import { isStorableProfileName, type AlysisSecretStore } from "../secrets/secretStorage";

/** Bridge methods the Models surface needs before it can offer any control. */
export const PROVIDER_CATALOG_METHODS = [
  "profile.presets",
  "profile.preset",
  "profile.list",
  "profile.use"
] as const;

/** One provider the user can connect to, projected from a profile.presets entry. */
export interface ProviderCatalogEntry {
  key: string;
  label: string;
  host: string;
  /** Raw wire protocol id from the preset (e.g. `openai_compat`, `openai_responses`). */
  protocol: string;
  protocolKind: string;
  protocolLabel: string;
  models: string[];
  /** Model slug -> one-line purpose, straight from the preset catalog. May be empty. */
  modelDescriptions: Record<string, string>;
  keyEnvVar: string;
  notes: string;
  warning: string;
  needsBaseUrl: boolean;
  local: boolean;
  recommended: boolean;
  profileName: string;
  connected: boolean;
}

/** One configured connection, projected from a profile.list entry. Never carries a key value. */
export interface ProviderConnection {
  profile: string;
  host: string;
  baseUrl: string;
  /** Raw wire protocol id reported by profile.list. */
  protocol: string;
  model: string;
  /**
   * Models offered for this connection: the configured model first, then the matching catalog
   * preset's suggestions. `profile.list` carries no model catalog of its own, so without the merge
   * every existing connection offered exactly one model plus "type a name".
   */
  models: string[];
  /** Model slug -> one-line purpose from the matching preset. Empty when no preset matched. */
  modelDescriptions: Record<string, string>;
  /** Catalog preset key the models were borrowed from; empty when nothing matched. */
  presetKey: string;
  protocolKind: string;
  active: boolean;
  hasKey: boolean;
  keySource: string;
  keyEnvVar: string;
  storedInVsCode: boolean;
  /** Non-empty only for account/subscription-backed profiles. */
  authProvider: string;
  /** Safe configured effort label; never credential-bearing. */
  reasoningEffort: string;
  /** Backend-owned completeness check. Missing/false fails closed. */
  selectionReady: boolean;
}

/**
 * What the busy target is currently doing, so the webview can explain the native prompts and
 * pauses instead of leaving the user staring at a disabled button.
 */
export type ModelsBusyPhase = "" | "waiting_for_key" | "connecting" | "checking_key";

export interface ModelsSurfaceState {
  supported: boolean;
  reason: string;
  loaded: boolean;
  busy: string;
  busyPhase: ModelsBusyPhase;
  error: string;
  activeProfile: string;
  activeModel: string;
  selectionStatus: "loading" | "ready" | "incomplete";
  selectionReason: string;
  providers: ProviderCatalogEntry[];
  connections: ProviderConnection[];
}

export interface ProviderConnectRequest {
  presetKey: string;
  model?: string;
  baseUrl?: string;
}

type CatalogBridge = Pick<
  AlysisBridgeClient,
  | "ensureStarted"
  | "supportsMethod"
  | "profilePresets"
  | "profilePreset"
  | "profileList"
  | "profileUse"
  | "configSet"
> &
  Partial<Pick<AlysisBridgeClient, "currentHealth" | "doctorProvidersLive" | "isRunning">>;

/** Outcome of the native key prompt: a fresh key, keep what is stored, or a clean cancel. */
type KeyPromptOutcome =
  | { kind: "entered"; value: string }
  | { kind: "keep_existing" }
  | { kind: "not_required" }
  | { kind: "cancelled" };

/** Outcome of the optional live key check after a connect or key change. */
interface KeyCheckOutcome {
  status: "passed" | "failed" | "unavailable";
  message: string;
}

export function emptyModelsSurfaceState(): ModelsSurfaceState {
  return {
    supported: false,
    reason: "Alysis Code has not checked the local CLI yet.",
    loaded: false,
    busy: "",
    busyPhase: "",
    error: "",
    activeProfile: "",
    activeModel: "",
    selectionStatus: "loading",
    selectionReason: "Checking the selected provider and model...",
    providers: [],
    connections: []
  };
}

// Owns the Models surface: the provider catalog (profile.presets), the configured connections
// (profile.list), and the connect/switch/forget/set-model operations behind them.
//
// Credentials never travel through this controller's bridge calls. The CLI rejects secret-bearing
// protocol params outright (inline_secret_rejected), so a key is collected with a native VS Code
// password prompt, stored in SecretStorage against the profile name, and reaches the agent only as
// the profile's declared api_key_env in the spawned bridge environment. The webview is told whether
// a key exists, never what it is.
export class ProviderCatalogController {
  private state: ModelsSurfaceState = emptyModelsSurfaceState();
  private inFlight: Promise<void> | undefined;
  private loadGeneration = 0;
  private mutationInFlight: Promise<void> | undefined;
  private mutationDescription = "";

  public constructor(
    private readonly bridge: CatalogBridge,
    private readonly secrets: AlysisSecretStore,
    private readonly getConfig: () => AlysisConfig,
    private readonly onChange: () => void,
    private readonly isWorkspaceTrusted: () => boolean = () => vscode.workspace.isTrusted,
    /**
     * Restarts the bridge with the freshly stored credentials (an idle-only, fingerprint-guarded
     * restart) so a live key check sees the key the user just typed. Optional: without it the
     * check is skipped rather than run against a bridge that predates the key.
     */
    private readonly ensureCredentials: (() => Promise<void>) | undefined = undefined,
    /** Commits the saved selection and any open conversation together. */
    private readonly activateProfile?: (profile: string, model?: string) => Promise<void>
  ) {}

  public snapshot(): ModelsSurfaceState {
    return {
      ...this.state,
      providers: this.state.providers.map((entry) => ({
        ...entry,
        models: [...entry.models],
        modelDescriptions: { ...entry.modelDescriptions }
      })),
      connections: this.state.connections.map((entry) => ({
        ...entry,
        models: [...entry.models],
        modelDescriptions: { ...entry.modelDescriptions }
      }))
    };
  }

  /** Reload the catalog. Concurrent calls share one in-flight refresh. */
  public refresh(): Promise<void> {
    if (this.inFlight) {
      return this.inFlight;
    }
    this.inFlight = this.load().finally(() => {
      this.inFlight = undefined;
    });
    return this.inFlight;
  }

  public clear(): void {
    // Invalidate any older provider reads so a delayed bridge response cannot repopulate state
    // after a disconnect/reset cleared the surface.
    this.loadGeneration += 1;
    this.set(emptyModelsSurfaceState());
  }

  /**
   * Connect a provider: collect the key first, then create the profile, make it active, store the
   * key, apply the chosen model, and finally verify the key with one live check when the CLI
   * supports it.
   *
   * The order is deliberate. Nothing is created or switched until the user has actually provided a
   * key (or chosen to keep a stored one), so Escape at any prompt is a clean cancel that leaves the
   * previous working connection untouched — the old flow switched first and stranded a cancel on a
   * keyless provider while still announcing success.
   */
  public connect(request: ProviderConnectRequest): Promise<void> {
    return this.runMutation(
      `connecting ${redactForDisplay(request.presetKey) || "a provider"}`,
      () => this.connectImpl(request)
    );
  }

  private async connectImpl(request: ProviderConnectRequest): Promise<void> {
    const entry = this.state.providers.find((candidate) => candidate.key === request.presetKey);
    if (!entry) {
      await this.fail(`Unknown provider: ${redactForDisplay(request.presetKey)}`);
      return;
    }
    if (!this.requireTrust("connect a provider")) {
      return;
    }
    const requestedModel = (request.model ?? "").trim();
    if (requestedModel.length === 0) {
      await this.fail("Choose a model before connecting this provider.");
      return;
    }
    const baseUrl = (request.baseUrl ?? "").trim();
    if (entry.needsBaseUrl && baseUrl.length === 0) {
      await this.fail(`${entry.label} needs a base URL before it can be connected.`);
      return;
    }
    // An already-configured provider is confirmed BEFORE the user types anything, so declining
    // costs nothing. The CLI-side requires_confirmation answer is still honored as a fallback in
    // createProfile for the edge where the catalog did not know the profile existed.
    let overwriteConfirmed = false;
    if (entry.connected) {
      const choice = await vscode.window.showWarningMessage(
        `${entry.label} is already configured. Reconnect it?`,
        {
          modal: true,
          detail: `This overwrites the ${entry.profileName} profile's endpoint and model defaults. Your saved key is kept.`
        },
        "Reconnect"
      );
      if (choice !== "Reconnect") {
        return;
      }
      overwriteConfirmed = true;
    }
    this.setBusy(entry.key, "waiting_for_key");
    try {
      const local = entry.local || isLoopbackHost(hostOf(baseUrl));
      const key = await this.promptForKey(entry.profileName, entry.keyEnvVar, entry.label, local);
      if (key.kind === "cancelled") {
        this.setBusy("");
        void vscode.window.showInformationMessage(
          `Canceled — nothing was changed. Connect ${entry.label} again when you have its API key.`
        );
        return;
      }
      this.setBusy(entry.key, "connecting");
      const created = await this.createProfile(entry, baseUrl, { confirmed: overwriteConfirmed });
      if (!created) {
        return;
      }
      if (key.kind === "entered") {
        await this.secrets.setProfileApiKey(entry.profileName, key.value, entry.keyEnvVar);
      }
      const model = requestedModel;
      await this.selectProfile(entry.profileName, model);
      this.setBusy(entry.key, "checking_key");
      const check = await this.verifyConnection();
      await this.load();
      // Fire-and-forget: the outcome toast offers "Replace key" and can sit unanswered in the
      // notification bell indefinitely. Awaiting it here held the mutation lock, so every later
      // Connect click was silently ignored. The work is done; reporting must not block the queue.
      void this.reportConnectOutcome(entry.label, entry.profileName, model, check);
    } catch (error) {
      await this.fail(this.describe("connect", error));
    } finally {
      this.setBusy("");
    }
  }

  /** Switch the active connection to an already-configured profile. */
  public useConnection(profile: string): Promise<void> {
    return this.runMutation(
      `switching to ${redactForDisplay(profile) || "another provider"}`,
      () => this.useConnectionImpl(profile)
    );
  }

  private async useConnectionImpl(profile: string): Promise<void> {
    if (!isStorableProfileName(profile)) {
      await this.fail("That provider profile name is not valid.");
      return;
    }
    if (!this.requireTrust("switch providers")) {
      return;
    }
    const target = this.state.connections.find((entry) => entry.profile === profile);
    let enteredKey: string | undefined;
    try {
      // Hosted targets need a key before switching. Local profiles may intentionally have none;
      // their optional credentials can still be added through Update Key.
      if (target && !target.hasKey && !isLoopbackHost(target.host)) {
        this.setBusy(profile, "waiting_for_key");
        const key = await this.promptForKey(profile, target.keyEnvVar, profile);
        if (key.kind === "cancelled") {
          this.setBusy("");
          void vscode.window.showInformationMessage(
            `Canceled — still using your current provider. ${profile} needs an API key before it can be switched to.`
          );
          return;
        }
        if (key.kind === "entered") {
          enteredKey = key.value;
        }
      }
      this.setBusy(profile, "connecting");
      if (enteredKey !== undefined) {
        await this.secrets.setProfileApiKey(profile, enteredKey, target?.keyEnvVar ?? "");
      }
      await this.selectProfile(profile);
      if (enteredKey !== undefined) {
        this.setBusy(profile, "checking_key");
        const check = await this.verifyConnection();
        await this.load();
        // Fire-and-forget for the same reason as connectImpl: an unanswered toast must not pin
        // the mutation lock.
        void this.reportConnectOutcome(profile, profile, target?.model ?? "", check);
        return;
      }
      await this.load();
    } catch (error) {
      await this.fail(this.describe("switch", error));
    } finally {
      this.setBusy("");
    }
  }

  /** Replace the stored key for a configured profile. */
  public updateKey(profile: string): Promise<void> {
    return this.runMutation(
      `updating the key for ${redactForDisplay(profile) || "a provider"}`,
      () => this.updateKeyImpl(profile)
    );
  }

  private async updateKeyImpl(profile: string): Promise<void> {
    const connection = this.state.connections.find((entry) => entry.profile === profile);
    if (!connection) {
      await this.fail("That provider is not configured.");
      return;
    }
    this.setBusy(profile, "waiting_for_key");
    try {
      const key = await this.promptForKey(profile, connection.keyEnvVar, profile);
      if (key.kind === "entered") {
        await this.secrets.setProfileApiKey(profile, key.value, connection.keyEnvVar);
        // Only the active profile can be live-checked (the CLI validates the active connection),
        // so a key saved for an inactive profile is reported as saved, not verified.
        if (connection.active) {
          this.setBusy(profile, "checking_key");
          const check = await this.verifyConnection();
          await this.load();
          // Fire-and-forget for the same reason as connectImpl: an unanswered toast must not pin
          // the mutation lock.
          void this.reportKeyOutcome(profile, check);
          return;
        }
        await this.load();
        void vscode.window.showInformationMessage(
          `Key for ${profile} saved securely. It will be checked when ${profile} becomes the active provider.`
        );
        return;
      }
      await this.load();
    } catch (error) {
      await this.fail(this.describe("update the key for", error));
    } finally {
      this.setBusy("");
    }
  }

  /** Forget the key this extension stored for a profile. Leaves the profile itself in place. */
  public forgetKey(profile: string): Promise<void> {
    return this.runMutation(
      `removing the key for ${redactForDisplay(profile) || "a provider"}`,
      () => this.forgetKeyImpl(profile)
    );
  }

  private async forgetKeyImpl(profile: string): Promise<void> {
    if (!isStorableProfileName(profile)) {
      return;
    }
    const confirmed = await vscode.window.showWarningMessage(
      `Remove the saved API key for ${profile}?`,
      { modal: true, detail: "The provider stays configured. Alysis Code will need a key again before it can run." },
      "Remove key"
    );
    if (confirmed !== "Remove key") {
      return;
    }
    this.setBusy(profile);
    try {
      await this.secrets.deleteProfileApiKey(profile);
      if (this.bridge.isRunning?.() && this.ensureCredentials) {
        try {
          await this.ensureCredentials();
        } catch {
          await vscode.window.showWarningMessage(
            `Saved API key for ${profile} removed. The current connection could not be refreshed. Stop any active task and restart the connection to stop using its previous credentials.`
          );
        }
      }
      await this.load();
    } catch (error) {
      await this.fail(this.describe("remove the key for", error));
    } finally {
      this.setBusy("");
    }
  }

  /** Set the model used by the active connection. */
  public setModel(model: string): Promise<void> {
    return this.runMutation("changing the active model", () => this.setModelImpl(model));
  }

  private async setModelImpl(model: string): Promise<void> {
    const normalized = model.trim();
    if (normalized.length === 0) {
      return;
    }
    if (redactForDisplay(normalized) !== normalized) {
      await this.fail("That looks like a credential, not a model name.");
      return;
    }
    if (!this.requireTrust("change the model")) {
      return;
    }
    this.setBusy(normalized);
    try {
      await this.applyModel(normalized);
      await this.load();
    } catch (error) {
      await this.fail(this.describe("set the model for", error));
    } finally {
      this.setBusy("");
    }
  }

  private async createProfile(
    entry: ProviderCatalogEntry,
    baseUrl: string,
    options: { confirmed: boolean } = { confirmed: false }
  ): Promise<boolean> {
    const params = {
      workspace_trusted: this.isWorkspaceTrusted(),
      preset_key: entry.key,
      name: entry.profileName,
      ...(baseUrl.length > 0 ? { base_url: baseUrl } : {})
    };
    // A reconnect the user already confirmed in connect() passes yes upfront; otherwise attempt
    // without it and honor a CLI-side requires_confirmation with the same modal as a fallback.
    const result = await this.bridge.profilePreset(
      options.confirmed ? { ...params, yes: true } : params
    );
    if (!requiresConfirmation(result)) {
      return true;
    }
    const choice = await vscode.window.showWarningMessage(
      `${entry.label} is already configured. Reconnect it?`,
      {
        modal: true,
        detail: `This overwrites the ${entry.profileName} profile's endpoint and model defaults. Your saved key is kept.`
      },
      "Reconnect"
    );
    if (choice !== "Reconnect") {
      return false;
    }
    await this.bridge.profilePreset({ ...params, yes: true });
    return true;
  }

  /**
   * Provider changes are intentionally single-writer operations. Webview disabled state arrives
   * asynchronously, so two fast clicks can otherwise overlap native prompts and race profile or
   * model writes. The first operation owns the busy state; later attempts are ignored, and every
   * ignored click says so — a once-per-busy-period notice left repeat clicks indistinguishable
   * from a dead button.
   */
  private runMutation(description: string, operation: () => Promise<void>): Promise<void> {
    if (this.mutationInFlight) {
      void vscode.window.showInformationMessage(
        `Alysis Code is already ${this.mutationDescription || "changing provider settings"}. Wait for it to finish before trying another change.`
      );
      return Promise.resolve();
    }

    this.mutationDescription = description;
    const mutation = Promise.resolve().then(operation);
    const tracked = mutation.finally(() => {
      if (this.mutationInFlight === tracked) {
        this.mutationInFlight = undefined;
        this.mutationDescription = "";
      }
    });
    this.mutationInFlight = tracked;
    return tracked;
  }

  /**
   * Collect a key with a native password prompt WITHOUT storing it — the caller decides when it is
   * safe to persist. Escape with a stored key means keep it (a reconnect must not silently drop
   * working credentials); Escape without one is a clean cancel.
   */
  private async promptForKey(
    profile: string,
    keyEnvVar: string,
    label: string,
    optional = false
  ): Promise<KeyPromptOutcome> {
    const existing = await this.secrets.getProfileApiKey(profile);
    const apiKey = await vscode.window.showInputBox({
      title: `API key for ${label}`,
      prompt: existing
        ? "Enter a new key to replace the saved one, or press Escape to keep it."
        : optional
          ? "This local connection can work without a key. Leave blank to continue, or enter a key if your server requires one. Escape cancels."
          : keyEnvVar
          ? `VS Code stores this securely and passes it to Alysis Code as ${keyEnvVar}. Press Escape to cancel.`
          : "VS Code stores this securely. It is never written to your project settings. Press Escape to cancel.",
      placeHolder: existing ? "New API key (Escape keeps the saved one)" : optional ? "Optional API key" : "Paste your API key",
      password: true,
      ignoreFocusOut: true,
      validateInput: (value) => (!optional && value.trim().length === 0 ? "API key is required." : undefined)
    });
    if (apiKey === undefined) {
      return existing ? { kind: "keep_existing" } : { kind: "cancelled" };
    }
    if (optional && apiKey.trim().length === 0) {
      return existing ? { kind: "keep_existing" } : { kind: "not_required" };
    }
    return { kind: "entered", value: apiKey };
  }

  /**
   * One explicit-intent live check of the active connection. Reports "unavailable" rather than
   * guessing when the CLI predates doctor.providers.live, the just-stored key cannot reach the
   * bridge process, or the check itself cannot run — a connection must never be called broken on
   * transport evidence.
   */
  private async verifyConnection(): Promise<KeyCheckOutcome> {
    const live = this.bridge.doctorProvidersLive?.bind(this.bridge);
    if (!live || !this.ensureCredentials) {
      return { status: "unavailable", message: "" };
    }
    try {
      // Credentials reach the CLI only through the bridge process environment, so the bridge must
      // be (re)started with the key that was just stored before the check can mean anything. The
      // restart is fingerprint-guarded and refuses while work is active.
      await this.ensureCredentials();
      if (!this.bridge.supportsMethod("doctor.providers.live")) {
        return { status: "unavailable", message: "" };
      }
      const result = await live({ allow_live: true });
      const message = redactForDisplay(String(result.validation?.message ?? "")).trim();
      return { status: result.ok === true ? "passed" : "failed", message };
    } catch (error) {
      return {
        status: "unavailable",
        message: redactForDisplay(error instanceof Error ? error.message : String(error))
      };
    }
  }

  /** Tell the user honestly where a connect landed, and offer the fix inline when the key failed. */
  private async reportConnectOutcome(
    label: string,
    profile: string,
    model: string,
    check: KeyCheckOutcome
  ): Promise<void> {
    const withModel = model ? ` with ${model}` : "";
    if (check.status === "passed") {
      void vscode.window.showInformationMessage(
        `${label} is connected and working${withModel}. You're ready to go.`
      );
      return;
    }
    if (check.status === "failed") {
      const choice = await vscode.window.showWarningMessage(
        `${label} is connected, but the key check failed: ${check.message || "the provider rejected the request."}`,
        "Replace key",
        "Keep anyway"
      );
      if (choice === "Replace key") {
        // Through the public wrapper: outcome reporting now runs outside the mutation lock, so a
        // late "Replace key" click must acquire it like any other provider change.
        await this.updateKey(profile);
      }
      return;
    }
    void vscode.window.showInformationMessage(`${label} is connected${withModel}.`);
  }

  /** Same honesty for a key replacement on the active connection. */
  private async reportKeyOutcome(profile: string, check: KeyCheckOutcome): Promise<void> {
    if (check.status === "passed") {
      void vscode.window.showInformationMessage(`Key for ${profile} saved and working.`);
      return;
    }
    if (check.status === "failed") {
      const choice = await vscode.window.showWarningMessage(
        `Key for ${profile} saved, but the key check failed: ${check.message || "the provider rejected the request."}`,
        "Replace key",
        "Keep anyway"
      );
      if (choice === "Replace key") {
        // Through the public wrapper: outcome reporting now runs outside the mutation lock, so a
        // late "Replace key" click must acquire it like any other provider change.
        await this.updateKey(profile);
      }
      return;
    }
    void vscode.window.showInformationMessage(`Key for ${profile} saved securely.`);
  }

  private async selectProfile(profile: string, model?: string): Promise<void> {
    if (this.activateProfile) {
      await this.activateProfile(profile, model);
      return;
    }
    await this.bridge.profileUse({ workspace_trusted: this.isWorkspaceTrusted(), name: profile });
    if (model) await this.applyModel(model);
  }

  private async applyModel(model: string): Promise<void> {
    // "model" is the CLI's settable key; it updates both the global default and the active
    // profile's default_model in one write, so the two cannot drift apart.
    await this.bridge.configSet({
      workspace_trusted: this.isWorkspaceTrusted(),
      key: "model",
      value: model
    });
  }

  private async load(): Promise<void> {
    // Refreshes are safe to overlap a mutation's final reload, but only the newest read may commit.
    // Without this fence, an older profile.list response can arrive after setModel/profileUse and
    // make the UI advertise a provider or model that the backend is no longer using.
    const generation = ++this.loadGeneration;
    try {
      // Start the bridge BEFORE asking what it supports. Capabilities come from the initialize
      // handshake, so supportsMethod() answers false for everything until a bridge is up — checking
      // first made every cold open claim the CLI was too old to list providers.
      await this.bridge.ensureStarted(this.getConfig(), { stripApiKey: true });
      // Every control on the surface reads or mutates through one of these. Only trust a negative
      // answer once the handshake actually produced capabilities; with no health yet, attempt the
      // calls and let a real method_not_found be the evidence rather than guessing at the CLI.
      const missing = PROVIDER_CATALOG_METHODS.filter((method) => !this.bridge.supportsMethod(method));
      if (missing.length > 0 && this.bridge.currentHealth?.() !== undefined) {
        if (generation !== this.loadGeneration) {
          return;
        }
        this.set({
          ...emptyModelsSurfaceState(),
          loaded: true,
          reason: `The connected Alysis Code CLI cannot list providers (missing: ${missing.join(", ")}). Upgrade with: ${CLI_UPGRADE_COMMAND}`,
          selectionStatus: "incomplete",
          selectionReason: "Update Alysis Code before sending so the active model selection can be validated."
        });
        return;
      }
      const [presets, profiles] = await Promise.all([
        this.bridge.profilePresets(),
        this.bridge.profileList()
      ]);
      const storedKeys = new Set(await this.secrets.listProfilesWithKeys());
      const configuredNames = new Set(configuredProfileNames(profiles));
      const providers = buildProviders(presets, configuredNames);
      // Connections borrow their model list from the catalog, so the catalog has to exist first.
      const connections = buildConnections(profiles, storedKeys, providers);
      const activeProfile = typeof profiles.active_profile === "string" ? profiles.active_profile : "";
      if (generation !== this.loadGeneration) {
        return;
      }
      const nextState: ModelsSurfaceState = {
        supported: true,
        reason: "",
        loaded: true,
        busy: this.state.busy,
        busyPhase: this.state.busyPhase,
        error: "",
        activeProfile,
        activeModel:
          connections.find((entry) => entry.active)?.model || this.getConfig().defaultModel.trim(),
        selectionStatus: "incomplete",
        selectionReason: "The selected provider has not been validated yet.",
        providers,
        connections
      };
      const readiness = providerSelectionReadiness(this.getConfig(), nextState);
      this.set({ ...nextState, selectionStatus: readiness.status, selectionReason: readiness.reason });
    } catch (error) {
      if (generation !== this.loadGeneration) {
        return;
      }
      this.set({
        ...emptyModelsSurfaceState(),
        loaded: true,
        reason: this.describe("load providers for", error),
        error: this.describe("load providers for", error),
        selectionStatus: "incomplete",
        selectionReason: this.describe("validate the active provider selection for", error)
      });
    }
  }

  private requireTrust(action: string): boolean {
    if (this.isWorkspaceTrusted()) {
      return true;
    }
    void vscode.window.showWarningMessage(
      `Workspace Trust is required to ${action}. Trust this folder, then try again.`
    );
    return false;
  }

  private async fail(message: string): Promise<void> {
    this.set({ ...this.state, error: message });
    await vscode.window.showErrorMessage(message);
  }

  private setBusy(busy: string, busyPhase: ModelsBusyPhase = ""): void {
    this.set({ ...this.state, busy, busyPhase: busy ? busyPhase : "" });
  }

  private set(state: ModelsSurfaceState): void {
    this.state = state;
    this.onChange();
  }

  private describe(action: string, error: unknown): string {
    const detail = redactForDisplay(error instanceof Error ? error.message : String(error));
    if (error instanceof ProtocolClientError) {
      if (error.code === "method_not_found") {
        return `The connected Alysis Code CLI cannot ${action} providers. Upgrade with: ${CLI_UPGRADE_COMMAND}`;
      }
      if (error.code === "cli_untrusted") {
        return `Cannot ${action} providers because CLI execution is blocked: ${detail}`;
      }
    }
    return `Could not ${action} providers: ${detail}`;
  }
}

function requiresConfirmation(result: unknown): boolean {
  const action = readRecord(readRecord(result)?.action);
  const kind = typeof action?.kind === "string" ? action.kind : "";
  return kind === "requires_confirmation" || kind === "confirmation_required";
}

function buildProviders(
  presets: { presets?: unknown },
  configuredNames: ReadonlySet<string>
): ProviderCatalogEntry[] {
  const rows = Array.isArray(presets.presets) ? presets.presets : [];
  const entries: ProviderCatalogEntry[] = [];
  for (const raw of rows) {
    const record = readRecord(redactDeep(raw));
    if (!record) {
      continue;
    }
    const key = stringField(record, "key");
    const label = stringField(record, "label") || key;
    if (key.length === 0) {
      continue;
    }
    const baseUrl = stringField(record, "base_url");
    const protocolKind = stringField(record, "protocol_kind");
    const profileName = isStorableProfileName(key) ? key.toLowerCase() : "";
    if (profileName.length === 0) {
      continue;
    }
    const host = stringField(record, "base_url_host") || hostOf(baseUrl);
    entries.push({
      key,
      label,
      host,
      protocol: stringField(record, "protocol"),
      protocolKind,
      protocolLabel: protocolLabel(protocolKind),
      models: stringList(record.suggested_models),
      modelDescriptions: stringMap(record.suggested_model_descriptions),
      keyEnvVar: stringField(record, "key_env_var"),
      notes: stringField(record, "notes"),
      warning: stringField(record, "setup_warning"),
      needsBaseUrl: baseUrl.trim().length === 0,
      local: isLoopbackHost(host),
      recommended: protocolKind === "native",
      profileName,
      connected: configuredNames.has(profileName)
    });
  }
  // Structural ordering only — native-protocol providers lead, then hosted endpoints, then local
  // and base-URL-required entries. Nothing here keys off provider prose, so a relabelled preset
  // cannot silently change where it sorts.
  return entries.sort((left, right) => rank(left) - rank(right) || left.label.localeCompare(right.label));
}

function rank(entry: ProviderCatalogEntry): number {
  if (entry.needsBaseUrl) {
    return 3;
  }
  if (entry.local) {
    return 2;
  }
  return entry.recommended ? 0 : 1;
}

function configuredProfileNames(profiles: { profiles?: unknown }): string[] {
  const rows = Array.isArray(profiles.profiles) ? profiles.profiles : [];
  const names: string[] = [];
  for (const raw of rows) {
    const name = stringField(readRecord(raw) ?? {}, "name");
    if (name.length > 0) {
      names.push(name);
    }
  }
  return names;
}

/**
 * Which catalog preset a configured connection came from. Profiles created through the catalog
 * keep the preset key as their name, so that match wins; anything else (a hand-made profile, a
 * renamed one, the CLI's own `default`) is matched by wire protocol and endpoint host. A profile
 * pointed at a custom endpoint matches nothing, and that is correct: its models are unknown.
 */
export function presetForConnection(
  connection: { profile: string; protocol: string; host: string },
  providers: readonly ProviderCatalogEntry[]
): ProviderCatalogEntry | undefined {
  const byName = providers.find((entry) => entry.profileName === connection.profile.toLowerCase());
  if (byName) {
    return byName;
  }
  const host = normalizeHost(connection.host);
  if (host.length === 0) {
    return undefined;
  }
  return providers.find(
    (entry) => entry.protocol === connection.protocol && entry.protocol.length > 0 && normalizeHost(entry.host) === host
  );
}

function normalizeHost(host: string): string {
  return host.trim().toLowerCase().replace(/^\[(.*)\]$/, "$1");
}

/** The configured model first (it is what runs), then the preset's suggestions, de-duplicated. */
function mergeModels(current: string, suggested: readonly string[]): string[] {
  const models: string[] = [];
  for (const candidate of [current, ...suggested]) {
    const model = candidate.trim();
    if (model.length > 0 && !models.includes(model)) {
      models.push(model);
    }
  }
  return models;
}

function buildConnections(
  profiles: { active_profile?: unknown; profiles?: unknown },
  storedKeys: ReadonlySet<string>,
  providers: readonly ProviderCatalogEntry[] = []
): ProviderConnection[] {
  const active = typeof profiles.active_profile === "string" ? profiles.active_profile : "";
  const rows = Array.isArray(profiles.profiles) ? profiles.profiles : [];
  const connections: ProviderConnection[] = [];
  for (const raw of rows) {
    const record = readRecord(redactDeep(raw));
    if (!record) {
      continue;
    }
    const profile = stringField(record, "name");
    if (profile.length === 0) {
      continue;
    }
    const apiKey = readRecord(record.api_key);
    const baseUrl = stringField(record, "base_url");
    const protocol = stringField(record, "protocol");
    const host = stringField(record, "base_url_host") || hostOf(baseUrl);
    const model = stringField(record, "default_model") || stringField(record, "model");
    const preset = presetForConnection({ profile, protocol, host }, providers);
    connections.push({
      profile,
      host,
      baseUrl,
      protocol,
      model,
      models: mergeModels(model, preset?.models ?? []),
      modelDescriptions: { ...(preset?.modelDescriptions ?? {}) },
      presetKey: preset?.key ?? "",
      protocolKind:
        stringField(record, "protocol_kind") ||
        preset?.protocolKind ||
        (protocol === "openai_compat" ? "compatibility" : "native"),
      active: profile === active,
      hasKey: apiKey?.present === true || storedKeys.has(profile),
      keySource: typeof apiKey?.source === "string" ? apiKey.source : "",
      keyEnvVar: stringField(record, "key_env_var"),
      storedInVsCode: storedKeys.has(profile),
      authProvider: stringField(record, "auth_provider"),
      reasoningEffort: stringField(record, "reasoning_effort"),
      selectionReady: record.subscription_selection_ready === true
    });
  }
  return connections.sort((left, right) =>
    Number(right.active) - Number(left.active) || left.profile.localeCompare(right.profile)
  );
}

export interface ProviderSelectionReadiness {
  status: "loading" | "ready" | "incomplete";
  reason: string;
}

/**
 * One fail-closed readiness contract shared by both composers and their host-side submit paths.
 * The backend owns subscription model/effort validation; the extension never guesses from names.
 */
export function providerSelectionReadiness(
  config: Pick<AlysisConfig, "defaultModel" | "provider" | "baseUrl">,
  state: ModelsSurfaceState
): ProviderSelectionReadiness {
  if (!state.loaded) {
    return { status: "loading", reason: "Checking the selected provider and model..." };
  }
  if (!state.supported) {
    return {
      status: "incomplete",
      reason: state.reason || "The connected Alysis Code CLI cannot validate provider selection readiness."
    };
  }
  const active = state.connections.find((connection) => connection.active);
  if (active) {
    if (!active.model.trim()) {
      return { status: "incomplete", reason: "Choose a model for the active provider before sending a task." };
    }
    if (active.authProvider && !active.selectionReady) {
      return {
        status: "incomplete",
        reason: "Choose the subscription model again so Alysis Code can select and validate its required reasoning effort."
      };
    }
    if (active.authProvider && !active.reasoningEffort.trim()) {
      return {
        status: "incomplete",
        reason: "The selected subscription model still needs a reasoning effort. Choose the model again."
      };
    }
    return { status: "ready", reason: "The active provider, model, and reasoning settings are ready." };
  }
  if (state.activeProfile.trim()) {
    return { status: "incomplete", reason: "The active provider profile could not be resolved. Refresh providers before sending." };
  }
  const fallbackModel = state.activeModel.trim() || config.defaultModel.trim();
  const fallbackProvider = config.provider.trim() || config.baseUrl.trim();
  if (fallbackModel && fallbackProvider) {
    return { status: "ready", reason: "The configured provider and model are ready." };
  }
  return { status: "incomplete", reason: "Choose a provider and model before sending a task." };
}

function protocolLabel(protocolKind: string): string {
  if (protocolKind === "native") {
    return "Native API";
  }
  if (protocolKind === "compatibility") {
    return "OpenAI-compatible";
  }
  return "";
}

function isLoopbackHost(host: string): boolean {
  const normalized = host.trim().toLowerCase()
    .replace(/^\[([^\]]+)\](?::\d+)?$/, "$1")
    .replace(/^([^:]+):\d+$/, "$1");
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "::1";
}

function hostOf(baseUrl: string): string {
  const trimmed = baseUrl.trim();
  if (trimmed.length === 0) {
    return "";
  }
  try {
    return new URL(trimmed).host;
  } catch {
    return "";
  }
}

function readRecord(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  return value as Record<string, unknown>;
}

function stringField(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "string" ? value.trim() : "";
}

/**
 * Catalog prose keyed by model slug. Bounded the same way as stringList so a hostile or corrupt
 * preset payload cannot balloon the surface state, and values are trimmed to a single display line.
 */
function stringMap(value: unknown): Record<string, string> {
  const record = readRecord(value);
  if (!record) {
    return {};
  }
  const out: Record<string, string> = {};
  for (const [key, raw] of Object.entries(record)) {
    if (Object.keys(out).length >= 40) {
      break;
    }
    if (typeof raw !== "string") {
      continue;
    }
    const text = raw.trim().replace(/\s+/g, " ");
    if (key.trim().length > 0 && text.length > 0) {
      out[key.trim()] = text.slice(0, 160);
    }
  }
  return out;
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const seen = new Set<string>();
  for (const entry of value) {
    if (typeof entry === "string" && entry.trim().length > 0 && seen.size < 40) {
      seen.add(entry.trim());
    }
  }
  return [...seen];
}
