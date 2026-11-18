import {
  ManagedBrowserArtifactReadParams,
  ManagedBrowserArtifactReadResult,
  ManagedBrowserClickParams,
  ManagedBrowserClickResult,
  ManagedBrowserCloseParams,
  ManagedBrowserCloseResult,
  ManagedBrowserDiagnosticsParams,
  ManagedBrowserDiagnosticsResult,
  ManagedBrowserListResult,
  ManagedBrowserNavigateParams,
  ManagedBrowserNavigateResult,
  ManagedBrowserNetworkScope,
  ManagedBrowserScreenshotParams,
  ManagedBrowserScreenshotResult,
  ManagedBrowserSessionParams,
  ManagedBrowserSessionStatus,
  ManagedBrowserSnapshotKind,
  ManagedBrowserSnapshotParams,
  ManagedBrowserSnapshotResult,
  ManagedBrowserStartParams,
  ManagedBrowserStatusResult,
  ManagedBrowserTypeParams,
  ManagedBrowserTypeResult
} from "../client/AlysisProtocol";
import { isUnsafeHostname } from "../runtime/ManagedCliRuntime";
import { BrowserPreview, BrowserPreviewStore } from "./BrowserPreviewStore";

const BROWSER_ID_RE = /^[A-Za-z0-9_-]{20,64}$/;
const URL_MAX_CHARS = 8_192;
const SELECTOR_MAX_CHARS = 2_000;
const TYPE_MAX_CHARS = 100_000;
const SCREENSHOT_CHUNK_BYTES = 1024 * 1024;
const SCREENSHOT_MAX_BYTES = 10 * 1024 * 1024;
const SNAPSHOT_PREVIEW_BYTES = 256 * 1024;
const DIAGNOSTIC_PREVIEW_BYTES = 4 * 1024;
const DIAGNOSTIC_MAX_EVENTS = 100;

export type BrowserCockpitOperation =
  | "refresh"
  | "start"
  | "navigate"
  | "snapshot"
  | "screenshot"
  | "diagnostics"
  | "click"
  | "type"
  | "close"
  | "save";

export interface BrowserSessionView {
  browserSessionId: string;
  product: string;
  state: "running" | "crashed";
  createdAt: number;
  networkScope: ManagedBrowserNetworkScope;
  activeUrl: string | null;
  artifactCount: number;
}

export interface BrowserSnapshotView {
  kind: ManagedBrowserSnapshotKind;
  preview: string;
  truncated: boolean;
  sizeBytes: number;
}

export interface BrowserScreenshotView {
  artifactId: string;
  previewKey: string;
  sizeBytes: number;
  sha256: string;
}

export interface BrowserDiagnosticView {
  category: "console" | "network";
  method: string;
  preview: string;
  truncated: boolean;
  sizeBytes: number;
}

export interface BrowserCockpitState {
  supported: boolean;
  reason: string | null;
  localTestingSupported: boolean;
  localTestingReason: string | null;
  ownerSessionId: string | null;
  phase: "idle" | "loading" | "ready" | "disconnected" | "error";
  sessions: BrowserSessionView[];
  selectedBrowserId: string | null;
  busy: BrowserCockpitOperation | null;
  notice: string | null;
  error: string | null;
  snapshot: BrowserSnapshotView | null;
  screenshot: BrowserScreenshotView | null;
  diagnostics: BrowserDiagnosticView[];
  diagnosticsTruncated: boolean;
}

/** Compact projection safe to publish with high-frequency chat deltas. */
export interface BrowserCockpitSummaryState {
  supported: boolean;
  reason: string | null;
  localTestingSupported: boolean;
  localTestingReason: string | null;
  ownerSessionId: string | null;
  phase: BrowserCockpitState["phase"];
  sessions: BrowserSessionView[];
  selectedBrowserId: string | null;
  busy: BrowserCockpitOperation | null;
  notice: string | null;
  error: string | null;
  hasSnapshot: boolean;
  hasScreenshot: boolean;
  diagnosticCount: number;
  diagnosticsTruncated: boolean;
}

export interface ManagedBrowserBridge {
  browserStart(params: ManagedBrowserStartParams): Promise<ManagedBrowserStatusResult>;
  browserNavigate(params: ManagedBrowserNavigateParams): Promise<ManagedBrowserNavigateResult>;
  browserSnapshot(params: ManagedBrowserSnapshotParams): Promise<ManagedBrowserSnapshotResult>;
  browserScreenshot(params: ManagedBrowserScreenshotParams): Promise<ManagedBrowserScreenshotResult>;
  browserArtifactRead(params: ManagedBrowserArtifactReadParams): Promise<ManagedBrowserArtifactReadResult>;
  browserDiagnostics(params: ManagedBrowserDiagnosticsParams): Promise<ManagedBrowserDiagnosticsResult>;
  browserClick(params: ManagedBrowserClickParams): Promise<ManagedBrowserClickResult>;
  browserType(params: ManagedBrowserTypeParams): Promise<ManagedBrowserTypeResult>;
  browserStatus(params: ManagedBrowserSessionParams): Promise<ManagedBrowserStatusResult>;
  browserList(sessionId: string): Promise<ManagedBrowserListResult>;
  browserClose(params: ManagedBrowserCloseParams): Promise<ManagedBrowserCloseResult>;
}

export interface BrowserCockpitDependencies {
  bridge: ManagedBrowserBridge;
  previewStore: BrowserPreviewStore;
  ensureOwnerSession(): Promise<string>;
  isWorkspaceTrusted(): boolean;
  compatibility(): BrowserCockpitCompatibility;
  confirmLocalStart(): Promise<boolean>;
  confirmClose(browser: BrowserSessionView): Promise<boolean>;
  onChange(state: BrowserCockpitState): void;
  report?(message: string): void;
}

export interface BrowserCockpitCompatibility {
  supported: boolean;
  reason: string | null;
  localTestingSupported: boolean;
  localTestingReason: string | null;
}

/**
 * Host-side managed-browser lifecycle. Normal starts are public and agent-shared.
 * The only local-capable start is the explicit, confirmed direct-user loopback
 * action; callers cannot pass an executable, destination class, or legacy local flag.
 */
export class BrowserCockpitController {
  private stateValue: BrowserCockpitState;
  private operationToken = 0;
  private currentPreviewValue: BrowserPreview | null = null;
  private disposed = false;

  public constructor(private readonly deps: BrowserCockpitDependencies) {
    const compatibility = deps.compatibility();
    this.stateValue = emptyBrowserCockpitState(compatibility);
  }

  public state(): BrowserCockpitState {
    return cloneState(this.stateValue);
  }

  public currentPreview(): BrowserPreview | null {
    return this.currentPreviewValue ? { ...this.currentPreviewValue } : null;
  }

  public async setOwnerSession(sessionId: string | undefined): Promise<void> {
    const owner = normalizedOwner(sessionId);
    if (owner === this.stateValue.ownerSessionId) {
      return;
    }
    this.operationToken += 1;
    const preview = this.detachCurrentPreview();
    const compatibility = this.deps.compatibility();
    this.stateValue = {
      ...emptyBrowserCockpitState(compatibility),
      ownerSessionId: owner,
      phase: owner ? "idle" : "idle"
    };
    this.publish();
    await this.cleanupPreview(preview);
  }

  public async disconnect(reason = "Managed browsers ended when the Alysis Code bridge stopped."): Promise<void> {
    this.operationToken += 1;
    const preview = this.detachCurrentPreview();
    const compatibility = this.deps.compatibility();
    this.stateValue = {
      ...emptyBrowserCockpitState(compatibility),
      phase: "disconnected",
      notice: boundedNotice(reason)
    };
    this.publish();
    await this.cleanupPreview(preview);
  }

  /** Re-project the live fail-closed capability gate without touching a browser session. */
  public syncCompatibility(): void {
    this.applyCompatibility();
    this.publish();
  }

  public async refresh(): Promise<void> {
    const owner = this.stateValue.ownerSessionId;
    if (!owner) {
      this.applyCompatibility();
      this.stateValue.phase = "idle";
      this.stateValue.sessions = [];
      this.stateValue.selectedBrowserId = null;
      this.publish();
      return;
    }
    await this.run("refresh", async (token) => {
      this.assertSupported();
      const result = await this.deps.bridge.browserList(owner);
      this.commit(token, () => {
        this.applyList(result);
        this.stateValue.notice = null;
      });
    });
  }

  public async start(): Promise<void> {
    await this.startWithScope("public");
  }

  public async startLocal(): Promise<void> {
    this.requireWorkspaceTrust("Starting a local-testing browser");
    this.assertLocalTestingSupported();
    if (!(await this.deps.confirmLocalStart())) {
      return;
    }
    await this.startWithScope("public_loopback");
  }

  private async startWithScope(
    networkScope: Exclude<ManagedBrowserNetworkScope, "local_network">
  ): Promise<void> {
    this.requireWorkspaceTrust("Starting a managed browser");
    const owner = normalizedOwner(await this.deps.ensureOwnerSession());
    if (!owner) {
      throw new BrowserCockpitInputError("Alysis Code did not create a browser owner session.");
    }
    if (owner !== this.stateValue.ownerSessionId) {
      await this.setOwnerSession(owner);
    }
    await this.run("start", async (token) => {
      // ensureOwnerSession may have started the bridge and populated its live
      // capability snapshot, so enforce the fail-closed gate after that probe.
      this.assertSupported();
      if (networkScope === "public_loopback") {
        this.assertLocalTestingSupported();
      }
      const result = await this.deps.bridge.browserStart({
        session_id: owner,
        workspace_trusted: true,
        network_scope: networkScope,
        ...(networkScope === "public_loopback" ? { yes: true, confirm: true } : {})
      });
      assertResponseScope(result, owner);
      if (result.network_scope !== networkScope) {
        throw new BrowserCockpitInputError("Managed browser returned an unexpected network scope.");
      }
      this.commit(token, () => {
        this.stateValue.ownerSessionId = owner;
        this.upsertSession(result);
        this.stateValue.selectedBrowserId = result.browser_session_id;
        this.stateValue.phase = "ready";
        this.stateValue.notice = networkScope === "public_loopback"
          ? "Local-testing browser started for Direct IDE use. The agent cannot access it."
          : "Managed browser started with public-web-only, agent-shared access.";
      });
    });
  }

  public select(browserSessionId: string): void {
    const browserId = validatedBrowserId(browserSessionId);
    if (!this.stateValue.sessions.some((item) => item.browserSessionId === browserId)) {
      throw new BrowserCockpitInputError("Managed browser session is not available.");
    }
    this.stateValue.selectedBrowserId = browserId;
    this.stateValue.snapshot = null;
    this.stateValue.diagnostics = [];
    this.stateValue.diagnosticsTruncated = false;
    this.publish();
  }

  public async navigate(url: string): Promise<void> {
    this.requireWorkspaceTrust("Browser navigation");
    const target = validatedNavigationUrl(url, this.selectedSession()?.networkScope);
    await this.withSelected("navigate", async (token, owner, browserId) => {
      const result = await this.deps.bridge.browserNavigate({
        session_id: owner,
        browser_session_id: browserId,
        workspace_trusted: true,
        url: target
      });
      assertResponseScope(result, owner, browserId);
      this.commit(token, () => {
        this.updateSession(browserId, { activeUrl: publicDisplayUrl(result.url) });
        this.stateValue.notice = "Navigation completed.";
      });
    });
  }

  public async snapshot(kind: ManagedBrowserSnapshotKind = "text"): Promise<void> {
    const snapshotKind = validatedSnapshotKind(kind);
    await this.withSelected("snapshot", async (token, owner, browserId) => {
      const result = await this.deps.bridge.browserSnapshot({
        session_id: owner,
        browser_session_id: browserId,
        kind: snapshotKind
      });
      assertResponseScope(result, owner, browserId);
      const rendered = result.kind === "text" ? result.text : safeJson(result.data);
      const preview = boundedUtf8(rendered, SNAPSHOT_PREVIEW_BYTES);
      this.commit(token, () => {
        this.stateValue.snapshot = {
          kind: result.kind,
          preview: preview.text,
          truncated: result.truncated || preview.truncated,
          sizeBytes: result.size_bytes
        };
        this.stateValue.notice = "Page snapshot refreshed.";
      });
    });
  }

  public async screenshot(fullPage = false): Promise<void> {
    await this.withSelected("screenshot", async (token, owner, browserId) => {
      const artifact = await this.deps.bridge.browserScreenshot({
        session_id: owner,
        browser_session_id: browserId,
        full_page: fullPage === true
      });
      assertResponseScope(artifact, owner, browserId);
      const bytes = await this.readScreenshot(token, owner, browserId, artifact);
      if (!this.isCurrent(token)) {
        return;
      }
      const preview = await this.deps.previewStore.store(bytes, {
        sizeBytes: artifact.size_bytes,
        sha256: artifact.sha256
      });
      if (!this.isCurrent(token)) {
        await this.deps.previewStore.remove(preview);
        return;
      }
      const previous = this.currentPreviewValue;
      this.currentPreviewValue = preview;
      if (previous && previous.key !== preview.key) {
        await this.deps.previewStore.remove(previous);
      }
      this.commit(token, () => {
        this.stateValue.screenshot = {
          artifactId: artifact.artifact_id,
          previewKey: preview.key,
          sizeBytes: artifact.size_bytes,
          sha256: artifact.sha256
        };
        this.stateValue.notice = "Screenshot captured and verified.";
        this.updateSession(browserId, {
          artifactCount: (this.session(browserId)?.artifactCount ?? 0) + 1
        });
      });
    });
  }

  public async diagnostics(): Promise<void> {
    await this.withSelected("diagnostics", async (token, owner, browserId) => {
      const result = await this.deps.bridge.browserDiagnostics({
        session_id: owner,
        browser_session_id: browserId,
        max_events: DIAGNOSTIC_MAX_EVENTS
      });
      assertResponseScope(result, owner, browserId);
      const events = result.events.slice(0, DIAGNOSTIC_MAX_EVENTS).map((event) => {
        const projected = projectDiagnostic(event.category, event.method, event.params.data);
        const preview = boundedUtf8(projected.preview, DIAGNOSTIC_PREVIEW_BYTES);
        return {
          category: event.category,
          method: projected.label,
          preview: preview.text,
          truncated: event.params.truncated || preview.truncated,
          sizeBytes: event.params.size_bytes
        };
      });
      this.commit(token, () => {
        this.stateValue.diagnostics = events;
        this.stateValue.diagnosticsTruncated = result.truncated || result.events.length > events.length;
        this.stateValue.notice = "Browser diagnostics refreshed.";
      });
    });
  }

  public async click(selector: string): Promise<void> {
    this.requireWorkspaceTrust("Browser interaction");
    const target = validatedSelector(selector);
    await this.withSelected("click", async (token, owner, browserId) => {
      const result = await this.deps.bridge.browserClick({
        session_id: owner,
        browser_session_id: browserId,
        workspace_trusted: true,
        selector: target
      });
      assertResponseScope(result, owner, browserId);
      this.commit(token, () => {
        this.stateValue.notice = "Element clicked.";
      });
    });
  }

  public async type(selector: string, text: string, replace = true): Promise<void> {
    this.requireWorkspaceTrust("Browser text input");
    const target = validatedSelector(selector);
    const input = validatedTypedText(text);
    await this.withSelected("type", async (token, owner, browserId) => {
      const result = await this.deps.bridge.browserType({
        session_id: owner,
        browser_session_id: browserId,
        workspace_trusted: true,
        selector: target,
        text: input,
        replace: replace === true
      });
      assertResponseScope(result, owner, browserId);
      this.commit(token, () => {
        this.stateValue.notice = `Typed ${result.character_count} characters.`;
      });
    });
  }

  public async close(): Promise<void> {
    const selected = this.selectedSession();
    if (!(await this.deps.confirmClose(selected))) {
      return;
    }
    // Act on the exact session the modal named. A select() arriving while the modal was open moves
    // the selection, and close() deletes artifacts — it must never land on a different browser.
    await this.withSelectedId("close", selected.browserSessionId, async (token, owner, browserId) => {
      // Exact owner-scoped cleanup is deliberately available after Workspace
      // Trust is revoked. `confirm` records the explicit direct user decision.
      const result = await this.deps.bridge.browserClose({
        session_id: owner,
        browser_session_id: browserId,
        delete_artifacts: true,
        confirm: true
      });
      assertResponseScope(result, owner, browserId);
      if (result.status !== "closed" && result.status !== "not_found") {
        throw new BrowserCockpitInputError("Managed browser did not confirm cleanup.");
      }
      this.commit(token, () => {
        this.stateValue.sessions = this.stateValue.sessions.filter(
          (item) => item.browserSessionId !== browserId
        );
        const selected = this.stateValue.selectedBrowserId;
        this.stateValue.selectedBrowserId = selected && selected !== browserId
          && this.stateValue.sessions.some((item) => item.browserSessionId === selected)
          ? selected
          : this.stateValue.sessions[0]?.browserSessionId ?? null;
        this.stateValue.snapshot = null;
        this.stateValue.screenshot = null;
        this.stateValue.diagnostics = [];
        this.stateValue.diagnosticsTruncated = false;
        this.stateValue.phase = this.stateValue.sessions.length > 0 ? "ready" : "idle";
        this.stateValue.notice = "Managed browser and ephemeral screenshots were deleted.";
      });
      await this.removeCurrentPreview();
    });
  }

  public async saveScreenshot(destination: string, overwrite = false): Promise<void> {
    const preview = this.currentPreviewValue;
    if (!preview || !this.stateValue.screenshot) {
      throw new BrowserCockpitInputError("Capture a screenshot before saving it.");
    }
    await this.run("save", async (token) => {
      await this.deps.previewStore.save(preview, destination, overwrite);
      this.commit(token, () => {
        this.stateValue.notice = "Verified screenshot saved.";
      });
    });
  }

  public async dispose(): Promise<void> {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    this.operationToken += 1;
    await this.cleanupPreview(this.detachCurrentPreview());
  }

  private async withSelected(
    operation: BrowserCockpitOperation,
    action: (token: number, owner: string, browserId: string) => Promise<void>
  ): Promise<void> {
    await this.withSelectedId(operation, this.selectedSession().browserSessionId, action);
  }

  private async withSelectedId(
    operation: BrowserCockpitOperation,
    browserSessionId: string,
    action: (token: number, owner: string, browserId: string) => Promise<void>
  ): Promise<void> {
    const owner = requiredOwner(this.stateValue.ownerSessionId);
    const browserId = validatedBrowserId(browserSessionId);
    if (!this.session(browserId)) {
      throw new BrowserCockpitInputError("Managed browser session is not available.");
    }
    await this.run(operation, (token) => action(token, owner, browserId));
  }

  private async run(
    operation: BrowserCockpitOperation,
    action: (token: number) => Promise<void>
  ): Promise<void> {
    if (this.disposed) {
      throw new BrowserCockpitInputError("Managed Browser is closed.");
    }
    if (this.stateValue.busy) {
      throw new BrowserCockpitInputError("Another managed browser operation is already running.");
    }
    const token = ++this.operationToken;
    this.applyCompatibility();
    this.stateValue.busy = operation;
    this.stateValue.error = null;
    this.stateValue.phase = "loading";
    this.publish();
    try {
      await action(token);
      this.commit(token, () => {
        this.stateValue.phase = this.stateValue.sessions.length > 0 ? "ready" : "idle";
      });
    } catch (error) {
      this.commit(token, () => {
        this.stateValue.phase = "error";
        this.stateValue.error = safeOperationError(error);
      });
      this.deps.report?.(`Managed browser ${operation} failed safely.`);
    } finally {
      this.commit(token, () => {
        this.stateValue.busy = null;
      });
    }
  }

  private async readScreenshot(
    token: number,
    owner: string,
    browserId: string,
    artifact: ManagedBrowserScreenshotResult
  ): Promise<Uint8Array> {
    if (!Number.isInteger(artifact.size_bytes) || artifact.size_bytes < 8 || artifact.size_bytes > SCREENSHOT_MAX_BYTES) {
      throw new BrowserCockpitInputError("Browser screenshot exceeded the host preview limit.");
    }
    const chunks: Buffer[] = [];
    let offset = 0;
    while (offset < artifact.size_bytes) {
      if (!this.isCurrent(token)) {
        return new Uint8Array();
      }
      const result = await this.deps.bridge.browserArtifactRead({
        session_id: owner,
        browser_session_id: browserId,
        artifact_id: artifact.artifact_id,
        offset,
        max_bytes: Math.min(SCREENSHOT_CHUNK_BYTES, artifact.size_bytes - offset)
      });
      const chunk = strictBase64(result.content);
      assertArtifactChunk(result, artifact, owner, browserId, offset, chunk.length);
      chunks.push(chunk);
      offset = result.next_offset;
      if (!result.truncated && offset !== artifact.size_bytes) {
        throw new BrowserCockpitInputError("Browser screenshot ended before its declared size.");
      }
    }
    return Buffer.concat(chunks, artifact.size_bytes);
  }

  private applyList(result: ManagedBrowserListResult): void {
    if (result.session_id !== this.stateValue.ownerSessionId) {
      throw new BrowserCockpitInputError("Managed browser owner changed during refresh.");
    }
    this.stateValue.sessions = result.browsers.map(sessionView);
    const selected = this.stateValue.selectedBrowserId;
    this.stateValue.selectedBrowserId = selected && this.stateValue.sessions.some(
      (item) => item.browserSessionId === selected
    )
      ? selected
      : this.stateValue.sessions[0]?.browserSessionId ?? null;
  }

  private upsertSession(status: ManagedBrowserSessionStatus): void {
    const item = sessionView(status);
    const index = this.stateValue.sessions.findIndex(
      (candidate) => candidate.browserSessionId === item.browserSessionId
    );
    if (index >= 0) {
      this.stateValue.sessions[index] = item;
    } else {
      this.stateValue.sessions.push(item);
    }
  }

  private updateSession(browserId: string, patch: Partial<BrowserSessionView>): void {
    this.stateValue.sessions = this.stateValue.sessions.map((item) =>
      item.browserSessionId === browserId ? { ...item, ...patch } : item
    );
  }

  private session(browserId: string): BrowserSessionView | undefined {
    return this.stateValue.sessions.find((item) => item.browserSessionId === browserId);
  }

  private selectedSession(): BrowserSessionView {
    const selected = this.stateValue.selectedBrowserId;
    const session = selected ? this.session(selected) : undefined;
    if (!session) {
      throw new BrowserCockpitInputError("Start or select a managed browser first.");
    }
    return session;
  }

  private applyCompatibility(): void {
    const compatibility = this.deps.compatibility();
    this.stateValue.supported = compatibility.supported;
    this.stateValue.reason = compatibility.reason ? boundedNotice(compatibility.reason) : null;
    this.stateValue.localTestingSupported = compatibility.localTestingSupported;
    this.stateValue.localTestingReason = compatibility.localTestingReason
      ? boundedNotice(compatibility.localTestingReason)
      : null;
  }

  private assertSupported(): void {
    const compatibility = this.deps.compatibility();
    this.stateValue.supported = compatibility.supported;
    this.stateValue.reason = compatibility.reason ? boundedNotice(compatibility.reason) : null;
    this.stateValue.localTestingSupported = compatibility.localTestingSupported;
    this.stateValue.localTestingReason = compatibility.localTestingReason
      ? boundedNotice(compatibility.localTestingReason)
      : null;
    if (!compatibility.supported) {
      throw new BrowserCockpitInputError(
        compatibility.reason || "Managed Browser is not supported by the connected Alysis Code CLI."
      );
    }
  }

  private assertLocalTestingSupported(): void {
    const compatibility = this.deps.compatibility();
    this.stateValue.localTestingSupported = compatibility.localTestingSupported;
    this.stateValue.localTestingReason = compatibility.localTestingReason
      ? boundedNotice(compatibility.localTestingReason)
      : null;
    if (!compatibility.localTestingSupported) {
      throw new BrowserCockpitInputError(
        compatibility.localTestingReason
        || "Direct IDE loopback browsing is not supported by the connected Alysis Code CLI."
      );
    }
  }

  private requireWorkspaceTrust(action: string): void {
    if (!this.deps.isWorkspaceTrusted()) {
      throw new BrowserCockpitInputError(`${action} requires Workspace Trust.`);
    }
  }

  private commit(token: number, mutate: () => void): void {
    if (!this.isCurrent(token)) {
      return;
    }
    mutate();
    this.publish();
  }

  private isCurrent(token: number): boolean {
    return !this.disposed && token === this.operationToken;
  }

  private detachCurrentPreview(): BrowserPreview | null {
    const preview = this.currentPreviewValue;
    this.currentPreviewValue = null;
    return preview;
  }

  private async removeCurrentPreview(): Promise<void> {
    await this.cleanupPreview(this.detachCurrentPreview());
  }

  private async cleanupPreview(preview: BrowserPreview | null): Promise<void> {
    if (!preview) {
      return;
    }
    try {
      await this.deps.previewStore.remove(preview);
    } catch {
      // State is already fenced and the path is no longer published. Cache cleanup
      // is best-effort here so a local filesystem failure cannot resurrect a stale
      // owner, browser id, or screenshot URI after a session/bridge transition.
      this.deps.report?.("Managed browser preview cleanup will be retried on extension cleanup.");
    }
  }

  private publish(): void {
    if (!this.disposed) {
      this.deps.onChange(this.state());
    }
  }
}

export class BrowserCockpitInputError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "BrowserCockpitInputError";
  }
}

export function emptyBrowserCockpitState(
  compatibility: BrowserCockpitCompatibility = {
    supported: false,
    reason: "Alysis Code CLI compatibility has not been checked.",
    localTestingSupported: false,
    localTestingReason: "Alysis Code CLI compatibility has not been checked."
  }
): BrowserCockpitState {
  return {
    supported: compatibility.supported,
    reason: compatibility.reason ? boundedNotice(compatibility.reason) : null,
    localTestingSupported: compatibility.localTestingSupported,
    localTestingReason: compatibility.localTestingReason
      ? boundedNotice(compatibility.localTestingReason)
      : null,
    ownerSessionId: null,
    phase: "idle",
    sessions: [],
    selectedBrowserId: null,
    busy: null,
    notice: null,
    error: null,
    snapshot: null,
    screenshot: null,
    diagnostics: [],
    diagnosticsTruncated: false
  };
}

export function browserCockpitSummary(state: BrowserCockpitState): BrowserCockpitSummaryState {
  return {
    supported: state.supported,
    reason: state.reason,
    localTestingSupported: state.localTestingSupported,
    localTestingReason: state.localTestingReason,
    ownerSessionId: state.ownerSessionId,
    phase: state.phase,
    sessions: state.sessions.map((item) => ({ ...item })),
    selectedBrowserId: state.selectedBrowserId,
    busy: state.busy,
    notice: state.notice,
    error: state.error,
    hasSnapshot: state.snapshot !== null,
    hasScreenshot: state.screenshot !== null,
    diagnosticCount: state.diagnostics.length,
    diagnosticsTruncated: state.diagnosticsTruncated
  };
}

function sessionView(status: ManagedBrowserSessionStatus): BrowserSessionView {
  return {
    browserSessionId: validatedBrowserId(status.browser_session_id),
    product: boundedLabel(status.product, 80),
    state: status.state,
    createdAt: status.created_at,
    networkScope: status.network_scope,
    activeUrl: publicDisplayUrl(status.active_url),
    artifactCount: status.artifact_count
  };
}

function cloneState(state: BrowserCockpitState): BrowserCockpitState {
  return {
    ...state,
    sessions: state.sessions.map((item) => ({ ...item })),
    snapshot: state.snapshot ? { ...state.snapshot } : null,
    screenshot: state.screenshot ? { ...state.screenshot } : null,
    diagnostics: state.diagnostics.map((item) => ({ ...item }))
  };
}

function normalizedOwner(value: string | undefined | null): string | null {
  const owner = String(value || "").trim();
  if (!owner) {
    return null;
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$/.test(owner)) {
    throw new BrowserCockpitInputError("Managed browser owner session id is invalid.");
  }
  return owner;
}

function requiredOwner(value: string | null): string {
  const owner = normalizedOwner(value);
  if (!owner) {
    throw new BrowserCockpitInputError("Start an Alysis Code task before using Managed Browser.");
  }
  return owner;
}

function validatedBrowserId(value: string): string {
  const browserId = String(value || "").trim();
  if (!BROWSER_ID_RE.test(browserId)) {
    throw new BrowserCockpitInputError("Managed browser session id is invalid.");
  }
  return browserId;
}

/**
 * Syntax-only validation: scheme, length, control characters and embedded credentials. It does NOT
 * classify the host as public — loopback, RFC1918 and link-link addresses all parse here, and the
 * managed browser backend is what enforces each session's network scope (public web only, or
 * public + loopback for a Direct IDE local-testing browser).
 */
function validatedNavigationUrl(value: string, networkScope?: string): string {
  const text = String(value || "").trim();
  if (!text || text.length > URL_MAX_CHARS || [...text].some((character) => character < " ")) {
    throw new BrowserCockpitInputError("Browser URL is invalid or too long.");
  }
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch {
    throw new BrowserCockpitInputError("Enter a complete HTTP or HTTPS URL.");
  }
  if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || parsed.username || parsed.password) {
    throw new BrowserCockpitInputError("Managed Browser accepts HTTP(S) URLs without user information.");
  }
  // The CLI owns network-scope policy, but an agent-shared public session must not be able to reach
  // loopback, RFC1918, or the cloud metadata endpoint even for the round trip it takes the backend
  // to refuse. A Direct IDE local-testing browser is explicitly scoped for loopback and keeps it.
  if (networkScope === "public" && isUnsafeHostname(parsed.hostname)) {
    throw new BrowserCockpitInputError(
      "This browser session reaches the public web only. Start a local-testing browser to open an address on this machine or network."
    );
  }
  return text;
}

function publicDisplayUrl(value: string | null): string | null {
  if (!value) {
    return null;
  }
  try {
    const parsed = new URL(value);
    if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.hostname) {
      return null;
    }
    const host = parsed.hostname.includes(":") ? `[${parsed.hostname}]` : parsed.hostname;
    const authority = parsed.port ? `${host}:${parsed.port}` : host;
    return `${parsed.protocol}//${authority}${parsed.pathname || "/"}`;
  } catch {
    return null;
  }
}

function validatedSelector(value: string): string {
  if (typeof value !== "string" || !value || value.length > SELECTOR_MAX_CHARS || value.includes("\0")) {
    throw new BrowserCockpitInputError("Browser selector is invalid or too long.");
  }
  return value;
}

function validatedTypedText(value: string): string {
  if (typeof value !== "string" || !value || value.length > TYPE_MAX_CHARS) {
    throw new BrowserCockpitInputError("Browser input text is invalid or too long.");
  }
  return value;
}

function validatedSnapshotKind(value: string): ManagedBrowserSnapshotKind {
  if (value !== "semantic" && value !== "accessibility" && value !== "dom" && value !== "text") {
    throw new BrowserCockpitInputError("Browser snapshot kind is invalid.");
  }
  return value;
}

function strictBase64(value: string): Buffer {
  if (typeof value !== "string" || value.length === 0 || value.length % 4 !== 0) {
    throw new BrowserCockpitInputError("Browser screenshot chunk encoding is invalid.");
  }
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) {
    throw new BrowserCockpitInputError("Browser screenshot chunk encoding is invalid.");
  }
  return Buffer.from(value, "base64");
}

function assertArtifactChunk(
  result: ManagedBrowserArtifactReadResult,
  artifact: ManagedBrowserScreenshotResult,
  owner: string,
  browserId: string,
  offset: number,
  decodedBytes: number
): void {
  if (
    result.session_id !== owner
    || result.browser_session_id !== browserId
    || result.artifact_id !== artifact.artifact_id
    || result.media_type !== "image/png"
    || result.encoding !== "base64"
    || result.offset !== offset
    || result.size_bytes !== artifact.size_bytes
    || result.next_offset !== offset + decodedBytes
    || result.next_offset <= offset
    || result.next_offset > artifact.size_bytes
    || result.truncated !== (result.next_offset < artifact.size_bytes)
  ) {
    throw new BrowserCockpitInputError("Browser screenshot chunk verification failed.");
  }
}

function assertResponseScope(
  result: { session_id: string; browser_session_id: string },
  owner: string,
  browserId?: string
): void {
  if (result.session_id !== owner || (browserId !== undefined && result.browser_session_id !== browserId)) {
    throw new BrowserCockpitInputError("Managed browser response scope verification failed.");
  }
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return "[Browser payload could not be rendered safely.]";
  }
}

function projectDiagnostic(
  category: "console" | "network",
  method: string,
  data: unknown
): { label: string; preview: string } {
  const rawRecord = isPlainRecord(data) ? data : {};
  const record = category === "console" && isPlainRecord(rawRecord.entry)
    ? rawRecord.entry
    : rawRecord;
  if (category === "console") {
    const level = safeConsoleLevel(record);
    return {
      label: level ? `Console ${level}` : "Console message",
      // Never project free-form console text or argument values. A page can
      // echo arbitrary content entered through browser.type into these fields.
      preview: "Console content hidden because it can contain browser input."
    };
  }
  const response = isPlainRecord(record.response) ? record.response : {};
  const request = isPlainRecord(record.request) ? record.request : {};
  const rawUrl = diagnosticString(response, ["url"], URL_MAX_CHARS)
    || diagnosticString(request, ["url"], URL_MAX_CHARS)
    || diagnosticString(record, ["url"], URL_MAX_CHARS);
  const url = publicDisplayUrl(rawUrl) || "Destination";
  const statusValue = typeof response.status === "number"
    ? response.status
    : typeof record.status === "number" ? record.status : undefined;
  const failure = diagnosticString(record, ["errorText", "blockedReason"], 500);
  const resourceType = diagnosticString(record, ["type", "resourceType"], 80);
  const label = /fail|error/i.test(method)
    ? "Network failure"
    : /response/i.test(method)
      ? "Network response"
      : "Network request";
  return {
    label,
    preview: [url, statusValue !== undefined ? `Status ${statusValue}` : "", resourceType, failure]
      .filter(Boolean)
       .join(" | ") || "A network event was captured."
  };
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function diagnosticString(
  record: Record<string, unknown>,
  keys: readonly string[],
  max: number
): string {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) {
      return boundedLabel(value.trim(), max);
    }
  }
  return "";
}

function safeConsoleLevel(record: Record<string, unknown>): string {
  const value = diagnosticString(record, ["level", "type"], 40).toLowerCase();
  switch (value) {
    case "debug":
    case "error":
    case "info":
    case "log":
    case "trace":
    case "warning":
      return value;
    case "warn":
      return "warning";
    default:
      return "";
  }
}

function boundedUtf8(value: string, maxBytes: number): { text: string; truncated: boolean } {
  const encoded = Buffer.from(String(value || ""), "utf8");
  if (encoded.length <= maxBytes) {
    return { text: encoded.toString("utf8"), truncated: false };
  }
  return { text: encoded.subarray(0, maxBytes).toString("utf8"), truncated: true };
}

function boundedLabel(value: string, max: number): string {
  return String(value || "").slice(0, max);
}

function boundedNotice(value: string): string {
  return boundedLabel(value, 1_000);
}

function safeOperationError(error: unknown): string {
  if (error instanceof BrowserCockpitInputError) {
    return boundedNotice(error.message);
  }
  if (error instanceof Error && "code" in error && typeof (error as { code?: unknown }).code === "string") {
    return `Managed browser request failed (${boundedLabel(String((error as { code: string }).code), 80)}).`;
  }
  return "Managed browser operation failed safely.";
}
